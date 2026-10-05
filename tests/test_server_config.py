"""
tests/test_server_config.py — tests for config loading and token guard.

Covers:
  - load_config reads config.json when present
  - load_config falls back to config.example.json when config.json is absent
  - LOCALCLIP_TOKEN env var overrides the file token
  - check_token raises SystemExit for the default "CHANGE_ME" token
  - check_token allows "CHANGE_ME" when insecure_dev=True
  - check_token accepts a non-default token without raising
  - Server returns 401 for a wrong token (integration)
  - Server returns 401 for a missing token (integration)
  - Server returns 200 with the correct token (integration)
"""

from __future__ import annotations

import importlib
import json
import threading
import urllib.request
import urllib.error
from pathlib import Path

import pytest

import server.server as srv


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_config(path: Path, token: str = "test-token-abc") -> None:
    cfg = {
        "port": 8787,
        "token": token,
        "model": "llama3.2",
        "num_ctx": 8192,
        "max_chars": 6000,
        "blocklist": [],
        "idle_ms": 5000,
    }
    path.write_text(json.dumps(cfg), encoding="utf-8")


def _reset_config_cache() -> None:
    """Clear the module-level cache so each test starts fresh."""
    srv._config_cache = None


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_reads_config_json_when_present(self, tmp_path, monkeypatch):
        _reset_config_cache()
        cfg = tmp_path / "config.json"
        _write_config(cfg, token="my-real-token")
        monkeypatch.setattr(srv, "_CONFIG_PATH", cfg)
        monkeypatch.setattr(srv, "_CONFIG_EXAMPLE_PATH", tmp_path / "config.example.json")
        result = srv.load_config()
        assert result["token"] == "my-real-token"

    def test_falls_back_to_example_when_config_json_absent(self, tmp_path, monkeypatch):
        _reset_config_cache()
        example = tmp_path / "config.example.json"
        _write_config(example, token="example-token")
        monkeypatch.setattr(srv, "_CONFIG_PATH", tmp_path / "config.json")  # does not exist
        monkeypatch.setattr(srv, "_CONFIG_EXAMPLE_PATH", example)
        result = srv.load_config()
        assert result["token"] == "example-token"

    def test_raises_when_neither_file_exists(self, tmp_path, monkeypatch):
        _reset_config_cache()
        monkeypatch.setattr(srv, "_CONFIG_PATH",         tmp_path / "config.json")
        monkeypatch.setattr(srv, "_CONFIG_EXAMPLE_PATH", tmp_path / "config.example.json")
        with pytest.raises(FileNotFoundError, match="config.example.json"):
            srv.load_config()

    def test_env_var_overrides_file_token(self, tmp_path, monkeypatch):
        _reset_config_cache()
        cfg = tmp_path / "config.json"
        _write_config(cfg, token="file-token")
        monkeypatch.setattr(srv, "_CONFIG_PATH", cfg)
        monkeypatch.setattr(srv, "_CONFIG_EXAMPLE_PATH", tmp_path / "config.example.json")
        monkeypatch.setenv("LOCALCLIP_TOKEN", "env-override-token")
        result = srv.load_config()
        assert result["token"] == "env-override-token"

    def test_env_var_absent_uses_file_token(self, tmp_path, monkeypatch):
        _reset_config_cache()
        cfg = tmp_path / "config.json"
        _write_config(cfg, token="file-token")
        monkeypatch.setattr(srv, "_CONFIG_PATH", cfg)
        monkeypatch.setattr(srv, "_CONFIG_EXAMPLE_PATH", tmp_path / "config.example.json")
        monkeypatch.delenv("LOCALCLIP_TOKEN", raising=False)
        result = srv.load_config()
        assert result["token"] == "file-token"

    def test_result_is_cached(self, tmp_path, monkeypatch):
        _reset_config_cache()
        cfg = tmp_path / "config.json"
        _write_config(cfg, token="cached-token")
        monkeypatch.setattr(srv, "_CONFIG_PATH", cfg)
        monkeypatch.setattr(srv, "_CONFIG_EXAMPLE_PATH", tmp_path / "config.example.json")
        r1 = srv.load_config()
        r2 = srv.load_config()
        assert r1 is r2


# ---------------------------------------------------------------------------
# check_token
# ---------------------------------------------------------------------------

class TestCheckToken:
    def test_refuses_default_token(self):
        """Server must exit when token is CHANGE_ME and insecure_dev=False."""
        config = {"token": "CHANGE_ME"}
        with pytest.raises(SystemExit) as exc_info:
            srv.check_token(config, insecure_dev=False)
        assert exc_info.value.code == 1

    def test_allows_default_token_with_insecure_dev(self):
        """--insecure-dev must bypass the check."""
        config = {"token": "CHANGE_ME"}
        # Must not raise
        srv.check_token(config, insecure_dev=True)

    def test_accepts_non_default_token(self):
        """A real token must pass without raising."""
        config = {"token": "abc123def456abc123def456abc12345"}
        srv.check_token(config, insecure_dev=False)

    def test_error_message_mentions_fix_steps(self, capsys):
        """The error output must tell the user what to do."""
        config = {"token": "CHANGE_ME"}
        with pytest.raises(SystemExit):
            srv.check_token(config, insecure_dev=False)
        captured = capsys.readouterr()
        assert "config.example.json" in captured.err
        assert "openssl rand" in captured.err
        assert "--insecure-dev" in captured.err


# ---------------------------------------------------------------------------
# HTTP token enforcement (integration — spins up a real server on a free port)
# ---------------------------------------------------------------------------

def _start_test_server(token: str) -> tuple[srv._ThreadingServer, int]:
    """Start a real LocalClipHandler on an OS-assigned free port."""
    _reset_config_cache()
    # Inject a minimal config directly so we don't need a file
    srv._config_cache = {
        "token": token,
        "port": 0,
        "model": "stub",
        "num_ctx": 8192,
        "max_chars": 6000,
        "blocklist": [],
        "idle_ms": 5000,
    }
    server = srv._ThreadingServer(("127.0.0.1", 0), srv.LocalClipHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _post(port: int, token: str | None, body: bytes = b"{}") -> int:
    """POST to /clip; return the HTTP status code."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-LocalClip-Token"] = token
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/clip",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


class TestTokenEnforcement:
    def setup_method(self):
        self.server, self.port = _start_test_server(token="correct-token-xyz")

    def teardown_method(self):
        self.server.shutdown()
        _reset_config_cache()

    def test_correct_token_returns_not_401(self):
        """A valid token must not produce a 401."""
        status = _post(self.port, token="correct-token-xyz")
        assert status != 401

    def test_wrong_token_returns_401(self):
        status = _post(self.port, token="wrong-token")
        assert status == 401

    def test_missing_token_returns_401(self):
        status = _post(self.port, token=None)
        assert status == 401

    def test_empty_token_returns_401(self):
        status = _post(self.port, token="")
        assert status == 401
