"""
tests/test_llm.py — mocked unit tests for server/llm.py

Covers:
  - valid response parsed and returned
  - invalid JSON on first attempt → retry → success
  - invalid JSON both attempts → fallback node (chain never breaks)
  - Ollama unreachable → OllamaUnavailableError raised (not swallowed)
  - Model not found → OllamaModelError raised
  - Schema format rejected (400) → falls back to format:"json" automatically
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch, call as mock_call

import pytest
import requests

import server.llm as llm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_config(model: str = "llama3.2") -> dict:
    return {
        "model":     model,
        "num_ctx":   8192,
        "max_chars": 6000,
    }


def _make_response(content: dict | str, status_code: int = 200) -> MagicMock:
    """Build a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    if isinstance(content, dict):
        resp.json.return_value = {"message": {"content": json.dumps(content)}}
        resp.text = json.dumps(content)
    else:
        # raw string content (used to simulate bad JSON)
        resp.json.return_value = {"message": {"content": content}}
        resp.text = content
    resp.raise_for_status = MagicMock()
    return resp


_VALID_RESULT = {
    "skip":    False,
    "title":   "How Python Generators Work",
    "topic":   "Python Generators",
    "bullets": ["Generators yield values lazily.", "Use yield to define one."],
}

_Q = "What is a Python generator?"
_A = "A generator is a function that uses yield."


# ---------------------------------------------------------------------------
# Valid response
# ---------------------------------------------------------------------------

class TestValidResponse:
    def test_returns_parsed_dict(self, monkeypatch):
        llm._USE_SCHEMA = True
        resp = _make_response(_VALID_RESULT)
        with patch("requests.post", return_value=resp) as mock_post:
            result = llm.call_ollama(_Q, _A, [], [], _mock_config())
        assert result["skip"]  is False
        assert result["title"] == "How Python Generators Work"
        assert result["topic"] == "Python Generators"
        assert len(result["bullets"]) == 2
        assert mock_post.call_count == 1

    def test_bullets_capped_at_6(self, monkeypatch):
        llm._USE_SCHEMA = True
        payload = dict(_VALID_RESULT, bullets=["b1","b2","b3","b4","b5","b6","b7"])
        resp = _make_response(payload)
        with patch("requests.post", return_value=resp):
            result = llm.call_ollama(_Q, _A, [], [], _mock_config())
        assert len(result["bullets"]) == 6

    def test_skip_true_returned(self):
        """llm.call_ollama returns skip=True as-is from the model.
        The skip gate (honor vs override) is enforced in server.handle_clip,
        not here.  See tests/test_skip_gate.py.
        """
        llm._USE_SCHEMA = True
        payload = {"skip": True, "title": "T", "topic": "T", "bullets": []}
        resp = _make_response(payload)
        with patch("requests.post", return_value=resp):
            result = llm.call_ollama(_Q, _A, [], [], _mock_config())
        assert result["skip"] is True


# ---------------------------------------------------------------------------
# Retry on invalid JSON
# ---------------------------------------------------------------------------

class TestRetry:
    def test_bad_json_first_attempt_retries(self):
        llm._USE_SCHEMA = True
        bad_resp  = _make_response("NOT JSON AT ALL")
        good_resp = _make_response(_VALID_RESULT)
        with patch("requests.post", side_effect=[bad_resp, good_resp]) as mock_post:
            result = llm.call_ollama(_Q, _A, [], [], _mock_config())
        assert mock_post.call_count == 2
        assert result["title"] == "How Python Generators Work"

    def test_bad_json_both_attempts_returns_fallback(self):
        llm._USE_SCHEMA = True
        bad_resp = _make_response("NOT JSON")
        with patch("requests.post", return_value=bad_resp) as mock_post:
            result = llm.call_ollama(_Q, _A, [], [], _mock_config())
        assert mock_post.call_count == 2
        assert result["skip"]    is False
        assert result["_llm_error"] == "llm_failed"
        assert result["bullets"] == []
        # Title should be first 8 words of question
        assert "What" in result["title"]

    def test_fallback_title_is_first_8_words(self):
        llm._USE_SCHEMA = True
        question = "one two three four five six seven eight nine ten"
        bad_resp = _make_response("BAD")
        with patch("requests.post", return_value=bad_resp):
            result = llm.call_ollama(question, _A, [], [], _mock_config())
        assert result["title"] == "one two three four five six seven eight"

    def test_missing_required_field_retries(self):
        """A response missing 'topic' is invalid → retry."""
        llm._USE_SCHEMA = True
        incomplete = {"skip": False, "title": "T", "bullets": []}  # no "topic"
        good       = dict(_VALID_RESULT)
        resp1 = _make_response(incomplete)
        resp2 = _make_response(good)
        with patch("requests.post", side_effect=[resp1, resp2]) as mock_post:
            result = llm.call_ollama(_Q, _A, [], [], _mock_config())
        assert mock_post.call_count == 2
        assert result["topic"] == "Python Generators"


# ---------------------------------------------------------------------------
# Ollama unreachable
# ---------------------------------------------------------------------------

class TestOllamaDown:
    def test_connection_error_raises_unavailable(self):
        llm._USE_SCHEMA = True
        with patch(
            "requests.post",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ):
            with pytest.raises(llm.OllamaUnavailableError, match="not running"):
                llm.call_ollama(_Q, _A, [], [], _mock_config())

    def test_timeout_raises_unavailable(self):
        llm._USE_SCHEMA = True
        with patch(
            "requests.post",
            side_effect=requests.exceptions.Timeout("timed out"),
        ):
            with pytest.raises(llm.OllamaUnavailableError, match="timed out"):
                llm.call_ollama(_Q, _A, [], [], _mock_config())

    def test_unavailable_not_retried(self):
        """OllamaUnavailableError must propagate immediately, not trigger retry."""
        llm._USE_SCHEMA = True
        with patch(
            "requests.post",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ) as mock_post:
            with pytest.raises(llm.OllamaUnavailableError):
                llm.call_ollama(_Q, _A, [], [], _mock_config())
        assert mock_post.call_count == 1


# ---------------------------------------------------------------------------
# Model not found
# ---------------------------------------------------------------------------

class TestModelNotFound:
    def test_404_raises_model_error(self):
        llm._USE_SCHEMA = True
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 404
        resp.raise_for_status = MagicMock()
        with patch("requests.post", return_value=resp):
            with pytest.raises(llm.OllamaModelError, match="not found"):
                llm.call_ollama(_Q, _A, [], [], _mock_config(model="no-such-model"))

    def test_200_with_error_field_raises_model_error(self):
        """Some Ollama versions return 200 with an error field instead of 404."""
        llm._USE_SCHEMA = True
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        resp.json.return_value = {"error": "model 'ghost' not found, try pulling it"}
        resp.text = '{"error":"model not found"}'
        resp.raise_for_status = MagicMock()
        with patch("requests.post", return_value=resp):
            with pytest.raises(llm.OllamaModelError):
                llm.call_ollama(_Q, _A, [], [], _mock_config(model="ghost"))


# ---------------------------------------------------------------------------
# Schema format fallback
# ---------------------------------------------------------------------------

class TestSchemaFallback:
    def test_400_resets_to_json_format_and_retries(self):
        """First call with schema gets 400 → second call uses format='json'."""
        llm._USE_SCHEMA = True

        schema_rejected = MagicMock(spec=requests.Response)
        schema_rejected.status_code = 400
        schema_rejected.raise_for_status = MagicMock()

        good_resp = _make_response(_VALID_RESULT)

        with patch("requests.post", side_effect=[schema_rejected, good_resp]) as mock_post:
            result = llm.call_ollama(_Q, _A, [], [], _mock_config())

        assert mock_post.call_count == 2
        # The second call must use format:"json" (a string, not a dict)
        second_call_payload = mock_post.call_args_list[1][1]["json"]
        assert second_call_payload["format"] == "json"
        assert result["title"] == "How Python Generators Work"
        # Flag must be flipped
        assert llm._USE_SCHEMA is False

    def teardown_method(self, _method):
        # Reset module flag after each test
        llm._USE_SCHEMA = True


# ---------------------------------------------------------------------------
# Prompt content (never logs chat text, includes context)
# ---------------------------------------------------------------------------

class TestPromptBuilding:
    def test_existing_topics_in_user_message(self):
        llm._USE_SCHEMA = True
        captured = {}

        def _capture(url, json=None, timeout=None):
            captured["payload"] = json
            return _make_response(_VALID_RESULT)

        with patch("requests.post", side_effect=_capture):
            llm.call_ollama(_Q, _A, ["Topic A", "Topic B"], [], _mock_config())

        user_content = captured["payload"]["messages"][1]["content"]
        assert "Topic A" in user_content
        assert "Topic B" in user_content

    def test_answer_truncated_to_max_chars(self):
        llm._USE_SCHEMA = True
        long_answer = "x" * 10000
        captured = {}

        def _capture(url, json=None, timeout=None):
            captured["payload"] = json
            return _make_response(_VALID_RESULT)

        with patch("requests.post", side_effect=_capture):
            llm.call_ollama(_Q, long_answer, [], [], _mock_config())

        user_content = captured["payload"]["messages"][1]["content"]
        # Answer section must be truncated
        assert "truncated" in user_content
        # Total content must be less than 10000 + overhead
        assert len(user_content) < 8000

    def test_token_never_in_payload(self):
        """The token is a server-side concern; llm.py must never include it."""
        llm._USE_SCHEMA = True
        captured = {}

        def _capture(url, json=None, timeout=None):
            captured["payload"] = json
            return _make_response(_VALID_RESULT)

        config = dict(_mock_config(), token="super-secret-token")
        with patch("requests.post", side_effect=_capture):
            llm.call_ollama(_Q, _A, [], [], config)

        payload_str = json.dumps(captured["payload"])
        assert "super-secret-token" not in payload_str
