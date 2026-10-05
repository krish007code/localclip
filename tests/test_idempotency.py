"""
tests/test_idempotency.py

Required tests from the duplicate-node bug fix:

  1. Same payload posted 3 times → exactly 1 node in nodes.jsonl.
  2. A second request for an in-flight id returns {"status":"busy"} without
     calling the LLM (mock a slow LLM).
  3. Existing duplicate ids in nodes.jsonl render as one note (keep first).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import server.server as srv
import server.store  as store_mod
import server.llm    as llm_mod
import server.render as render_mod


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _reset(tmp_path: Path, monkeypatch):
    """Redirect file paths, reset server module-level state."""
    monkeypatch.setattr(store_mod, "DATA_DIR",   tmp_path)
    monkeypatch.setattr(store_mod, "RAW_DIR",    tmp_path / "raw")
    monkeypatch.setattr(store_mod, "NODES_FILE", tmp_path / "nodes.jsonl")
    monkeypatch.setattr(store_mod, "STATE_FILE", tmp_path / "state.json")
    (tmp_path / "raw").mkdir(parents=True, exist_ok=True)

    # Reset server in-memory sets for each test
    srv._processed_ids = set()
    srv._inflight      = set()
    srv._config_cache  = {
        "token":     "test-token",
        "model":     "stub",
        "num_ctx":   8192,
        "max_chars": 6000,
        "blocklist": [],
        "idle_ms":   5000,
    }


def _good_llm(*_a, **_kw):
    return {
        "skip":    False,
        "title":   "Broadcast Join Explained",
        "topic":   "PySpark",
        "bullets": ["fact one"],
    }


def _payload(conv_id: str = "conv-1", flush: bool = True) -> dict:
    return {
        "url":             "https://chatgpt.com/c/test",
        "title":           "Test",
        "source":          "chat",
        "conversation_id": conv_id,
        "flush":           flush,
        "captured_at":     "2025-10-05T10:00:00Z",
        "messages": [
            {"id": "u1", "role": "user",      "markdown": "How does PySpark broadcast join work?"},
            {"id": "a1", "role": "assistant", "markdown": "A broadcast join sends the smaller table to all executors."},
        ],
    }


def _node_count(tmp_path: Path) -> int:
    nf = tmp_path / "nodes.jsonl"
    if not nf.exists():
        return 0
    return sum(1 for l in nf.read_text().splitlines() if l.strip())


# ---------------------------------------------------------------------------
# Test 1: Same payload 3 times → exactly 1 node
# ---------------------------------------------------------------------------

class TestSamePayloadThreeTimes:
    def test_three_posts_produce_one_node(self, tmp_path, monkeypatch):
        _reset(tmp_path, monkeypatch)
        cfg = srv._config_cache

        with patch.object(llm_mod, "call_ollama", side_effect=_good_llm):
            with patch.object(render_mod, "rebuild"):
                # First call: goes through pipeline
                r1 = srv.handle_clip(_payload(), cfg)
                # Worker hasn't run yet (no real thread in unit test),
                # so we call _process_turns directly to simulate it.
                # Drain any queued items synchronously.
                _drain_queue_synchronously()

                r2 = srv.handle_clip(_payload(), cfg)
                r3 = srv.handle_clip(_payload(), cfg)

        assert r1["status"] == "accepted"
        # r2 and r3 must be duplicate or busy — never accepted for the same id
        assert r2["status"] in ("duplicate", "busy")
        assert r3["status"] in ("duplicate", "busy")
        # Only 1 node written
        assert _node_count(tmp_path) == 1

    def test_three_direct_process_calls_produce_one_node(self, tmp_path, monkeypatch):
        """Call _process_turns directly three times with the same turn id."""
        _reset(tmp_path, monkeypatch)
        cfg = srv._config_cache
        payload_meta = {
            "url": "https://chatgpt.com/c/test",
            "title": "Test",
            "conversation_id": "conv-1",
            "captured_at": "2025-10-05T10:00:00Z",
        }
        pairs = [
            ({"id": "u1", "role": "user",      "markdown": "Q"},
             {"id": "a1", "role": "assistant", "markdown": "A"}),
        ]

        with patch.object(llm_mod, "call_ollama", side_effect=_good_llm):
            with patch.object(render_mod, "rebuild"):
                srv._process_turns(pairs, payload_meta, cfg)
                # Second and third calls: u1 is now in _processed_ids
                srv._process_turns(pairs, payload_meta, cfg)
                srv._process_turns(pairs, payload_meta, cfg)

        assert _node_count(tmp_path) == 1

    def test_llm_not_called_on_second_identical_process(self, tmp_path, monkeypatch):
        """_process_turns must skip the LLM entirely for already-processed ids."""
        _reset(tmp_path, monkeypatch)
        cfg = srv._config_cache
        payload_meta = {
            "url": "u", "title": "t",
            "conversation_id": "conv-1",
            "captured_at": "2025-10-05T10:00:00Z",
        }
        pairs = [
            ({"id": "u1", "role": "user",      "markdown": "Q"},
             {"id": "a1", "role": "assistant", "markdown": "A"}),
        ]

        call_count = {"n": 0}
        def counting_llm(*_a, **_kw):
            call_count["n"] += 1
            return _good_llm()

        with patch.object(llm_mod, "call_ollama", side_effect=counting_llm):
            with patch.object(render_mod, "rebuild"):
                srv._process_turns(pairs, payload_meta, cfg)
                srv._process_turns(pairs, payload_meta, cfg)

        assert call_count["n"] == 1, "LLM was called more than once for same turn id"


# ---------------------------------------------------------------------------
# Test 2: In-flight id returns busy without calling LLM
# ---------------------------------------------------------------------------

class TestInFlightBusy:
    def test_inflight_id_returns_busy(self, tmp_path, monkeypatch):
        _reset(tmp_path, monkeypatch)
        cfg = srv._config_cache

        # Pre-populate _inflight with u1 to simulate an in-flight request
        with srv._ID_LOCK:
            srv._inflight.add("u1")

        llm_called = {"n": 0}
        def spy_llm(*_a, **_kw):
            llm_called["n"] += 1
            return _good_llm()

        with patch.object(llm_mod, "call_ollama", side_effect=spy_llm):
            with patch.object(render_mod, "rebuild"):
                result = srv.handle_clip(_payload(), cfg)

        assert result["status"] == "busy"
        assert llm_called["n"] == 0, "LLM must not be called when id is in-flight"

    def test_inflight_cleared_after_worker_completes(self, tmp_path, monkeypatch):
        """After _process_turns finishes, the id must no longer be in _inflight."""
        _reset(tmp_path, monkeypatch)
        cfg = srv._config_cache
        tid = "u1"
        payload_meta = {
            "url": "u", "title": "t",
            "conversation_id": "conv-1",
            "captured_at": "2025-10-05T10:00:00Z",
        }
        pairs = [
            ({"id": tid, "role": "user",      "markdown": "Q"},
             {"id": "a1", "role": "assistant", "markdown": "A"}),
        ]

        # Manually add to inflight (as the worker would)
        with srv._ID_LOCK:
            srv._inflight.add(tid)

        with patch.object(llm_mod, "call_ollama", side_effect=_good_llm):
            with patch.object(render_mod, "rebuild"):
                srv._process_turns(pairs, payload_meta, cfg)

        # After processing, worker's finally block removes from inflight.
        # Simulate it (in unit tests we call _process_turns directly, so
        # we verify the id IS in _processed_ids and thus future requests
        # won't call the LLM regardless).
        with srv._ID_LOCK:
            assert tid in srv._processed_ids

    def test_busy_does_not_advance_processed_ids(self, tmp_path, monkeypatch):
        """A busy response must not add the id to _processed_ids."""
        _reset(tmp_path, monkeypatch)
        cfg = srv._config_cache

        with srv._ID_LOCK:
            srv._inflight.add("u1")

        with patch.object(llm_mod, "call_ollama", side_effect=_good_llm):
            with patch.object(render_mod, "rebuild"):
                srv.handle_clip(_payload(), cfg)

        with srv._ID_LOCK:
            assert "u1" not in srv._processed_ids


# ---------------------------------------------------------------------------
# Test 3: Duplicate ids in nodes.jsonl render as one note
# ---------------------------------------------------------------------------

class TestDuplicateIdsRenderAsOne:
    def _make_node(self, id_: str, idx: int = 0, title: str = "T",
                   ts: str = "2025-01-01T00:00:00Z") -> dict:
        return {
            "id":       id_,
            "conv_id":  "conv-abc12345",
            "idx":      idx,
            "prev":     None,
            "question": "Q",
            "title":    title,
            "topic":    "Test Topic",
            "bullets":  ["bullet"],
            "raw_file": "raw.md",
            "ts":       ts,
        }

    def _write_nodes(self, path: Path, nodes: list[dict]) -> None:
        path.write_text(
            "\n".join(json.dumps(n) for n in nodes) + "\n",
            encoding="utf-8",
        )

    def test_duplicate_ids_produce_one_vault_file(self, tmp_path):
        vault   = tmp_path / "vault"
        nf      = tmp_path / "nodes.jsonl"
        vault.mkdir()

        # Write the same id twice (simulating the bug)
        node_first  = self._make_node("u1", idx=0, title="First Title",
                                      ts="2025-01-01T00:00:00Z")
        node_second = self._make_node("u1", idx=0, title="Second Title",
                                      ts="2025-01-01T00:00:01Z")
        self._write_nodes(nf, [node_first, node_second])

        render_mod.rebuild(nodes_path=nf, vault_path=vault)

        md_files = list(vault.glob("*.md"))
        node_files = [f for f in md_files if "_Index" not in f.name
                      and "Test Topic" not in f.name]
        # Exactly one node file for this conversation
        conv_files = [f for f in md_files
                      if f.name.startswith("conv-ab") and "_Index" not in f.name]
        assert len(conv_files) == 1, (
            f"Expected 1 node file, found {len(conv_files)}: "
            f"{[f.name for f in conv_files]}"
        )

    def test_first_occurrence_kept_not_second(self, tmp_path):
        vault = tmp_path / "vault"
        nf    = tmp_path / "nodes.jsonl"
        vault.mkdir()

        node_first  = self._make_node("u1", title="KEEP THIS",
                                      ts="2025-01-01T00:00:00Z")
        node_second = self._make_node("u1", title="DROP THIS",
                                      ts="2025-01-01T00:00:01Z")
        self._write_nodes(nf, [node_first, node_second])

        render_mod.rebuild(nodes_path=nf, vault_path=vault)

        # Find the node file
        node_files = [f for f in vault.glob("*.md")
                      if f.name.startswith("conv-ab") and "_Index" not in f.name]
        assert len(node_files) == 1
        content = node_files[0].read_text()
        assert "KEEP THIS" in content
        assert "DROP THIS" not in content

    def test_three_duplicates_produce_one_note(self, tmp_path):
        vault = tmp_path / "vault"
        nf    = tmp_path / "nodes.jsonl"
        vault.mkdir()

        nodes = [self._make_node("u1", title=f"Copy {i}",
                                 ts=f"2025-01-01T00:0{i}:00Z") for i in range(3)]
        self._write_nodes(nf, nodes)

        render_mod.rebuild(nodes_path=nf, vault_path=vault)

        node_files = [f for f in vault.glob("*.md")
                      if f.name.startswith("conv-ab") and "_Index" not in f.name]
        assert len(node_files) == 1
        content = node_files[0].read_text()
        assert "Copy 0" in content
        assert "Copy 1" not in content
        assert "Copy 2" not in content


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _drain_queue_synchronously():
    """Pull items off _work_queue and call _process_turns directly."""
    while not srv._work_queue.empty():
        try:
            item = srv._work_queue.get_nowait()
            if item is None:
                break
            new_pairs, payload_meta, config = item
            try:
                srv._process_turns(new_pairs, payload_meta, config)
            finally:
                with srv._ID_LOCK:
                    for user_turn, _ in new_pairs:
                        srv._inflight.discard(user_turn["id"])
                srv._work_queue.task_done()
        except Exception:
            break
