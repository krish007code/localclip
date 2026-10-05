"""
llm.py — Ollama integration for LocalClip.

One call per closed turn (question + answer).
Schema-constrained format (Ollama ≥ 0.3) with automatic fallback to
format:"json" + manual validation for older versions.

Failure contract:
  - Invalid JSON → retry once.
  - Second failure → return a fallback node (title = first 8 words of question,
    empty bullets) so the conversation chain never breaks.
  - Ollama unreachable → raise OllamaUnavailableError.
  - Model missing on server → raise OllamaModelError.
"""

from __future__ import annotations

import json
import re
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class OllamaUnavailableError(Exception):
    """Raised when Ollama is not reachable at all."""

class OllamaModelError(Exception):
    """Raised when the requested model is not available on the Ollama server."""

# ---------------------------------------------------------------------------
# Module-level flag: set False if Ollama rejects the schema dict
# ---------------------------------------------------------------------------

_USE_SCHEMA: bool = True   # flipped to False on first 400/422 from Ollama

# ---------------------------------------------------------------------------
# JSON schema for the LLM output
# ---------------------------------------------------------------------------

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "skip":    {"type": "boolean"},
        "title":   {"type": "string"},
        "topic":   {"type": "string"},
        "bullets": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 6,
        },
    },
    "required": ["skip", "title", "topic", "bullets"],
}

# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You extract durable knowledge from a single question-and-answer exchange. "
    "Return only facts, decisions, definitions, commands and code snippets worth "
    "remembering, copied or closely quoted — not paraphrased into vagueness. "
    "Ignore greetings, filler, and repeated content. "
    "Provide a short descriptive title (max 8 words) for this specific exchange. "
    "If everything is already covered by the recent notes provided, set skip=true. "
    "Choose topic from the existing topics list when one fits; otherwise propose a "
    "short new noun-phrase topic (max 4 words). Never invent facts."
)


def _build_user_message(
    question: str,
    answer: str,
    existing_topics: list[str],
    recent_bullets: list[str],
    max_chars: int,
) -> str:
    parts: list[str] = []

    if existing_topics:
        parts.append("## Existing topics (pick one or propose a new short noun phrase)")
        parts.append(", ".join(existing_topics[:80]))
        parts.append("")

    if recent_bullets:
        parts.append("## Recent bullets (set skip=true if already covered)")
        for b in recent_bullets[:8]:
            parts.append(f"- {b}")
        parts.append("")

    parts.append("## Question")
    parts.append(question)
    parts.append("")
    parts.append("## Answer")
    # Truncate answer to max_chars to stay within context window
    if len(answer) > max_chars:
        answer = answer[:max_chars] + "\n…[truncated]"
    parts.append(answer)

    return "\n".join(parts)

# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_response(raw: str) -> dict[str, Any]:
    """Parse and validate the LLM JSON response.

    Raises ValueError on missing/wrong-type fields.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    if not isinstance(data.get("skip"), bool):
        raise ValueError("missing or non-bool 'skip'")
    if not isinstance(data.get("title"), str) or not data["title"].strip():
        raise ValueError("missing or empty 'title'")
    if not isinstance(data.get("topic"), str) or not data["topic"].strip():
        raise ValueError("missing or empty 'topic'")
    if not isinstance(data.get("bullets"), list):
        raise ValueError("missing or non-list 'bullets'")
    # Coerce any non-string bullets to strings
    data["bullets"] = [str(b) for b in data["bullets"]][:6]
    return data

# ---------------------------------------------------------------------------
# Fallback result (chain never breaks)
# ---------------------------------------------------------------------------

def _fallback_result(question: str) -> dict[str, Any]:
    """Return a minimal valid result when the LLM fails twice."""
    words = question.strip().split()
    title = " ".join(words[:8]) or "Untitled"
    return {
        "skip":    False,
        "title":   title,
        "topic":   "Uncategorized",
        "bullets": [],
        "_llm_error": "llm_failed",
    }

# ---------------------------------------------------------------------------
# Raw HTTP call
# ---------------------------------------------------------------------------

def _post_ollama(messages: list[dict], config: dict[str, Any]) -> str:
    """POST to Ollama /api/chat and return the assistant message content string.

    Raises:
        OllamaUnavailableError  — connection refused / timeout
        OllamaModelError        — model not found on this Ollama instance
        requests.HTTPError      — any other HTTP error from Ollama
    """
    global _USE_SCHEMA

    ollama_url = "http://127.0.0.1:11434/api/chat"
    payload: dict[str, Any] = {
        "model":   config["model"],
        "messages": messages,
        "stream":  False,
        "options": {
            "temperature": 0,
            "num_ctx":     int(config.get("num_ctx", 8192)),
        },
    }

    if _USE_SCHEMA:
        payload["format"] = _SCHEMA
    else:
        payload["format"] = "json"

    try:
        resp = requests.post(ollama_url, json=payload, timeout=120)
    except requests.exceptions.ConnectionError as exc:
        raise OllamaUnavailableError("Ollama not running") from exc
    except requests.exceptions.Timeout as exc:
        raise OllamaUnavailableError("Ollama request timed out") from exc

    # Schema format not supported by this Ollama version → fall back to "json"
    if resp.status_code in (400, 422) and _USE_SCHEMA:
        _USE_SCHEMA = False
        payload["format"] = "json"
        resp = requests.post(ollama_url, json=payload, timeout=120)

    # Model not found
    if resp.status_code == 404:
        raise OllamaModelError(f"model not found: {config['model']}")

    # Detect model-not-found in 200 responses (some Ollama versions return 200
    # with an error field instead of a 404)
    if resp.status_code == 200:
        try:
            body = resp.json()
            err = body.get("error", "")
            if err and ("not found" in err.lower() or "pull" in err.lower()):
                raise OllamaModelError(f"model not found: {config['model']}")
            # Extract the assistant message content
            return body["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"unexpected Ollama response shape: {resp.text[:200]}") from exc

    resp.raise_for_status()
    # Should not reach here
    raise ValueError(f"unexpected Ollama status {resp.status_code}")

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def call_ollama(
    question: str,
    answer: str,
    existing_topics: list[str],
    recent_bullets: list[str],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Call Ollama for one turn and return a validated result dict.

    Returns a dict with keys: skip, title, topic, bullets.
    May also contain _llm_error="llm_failed" if both attempts failed
    (caller should still write the node with empty bullets).

    Raises OllamaUnavailableError or OllamaModelError — caller handles these
    to avoid marking the turn processed.
    """
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user",   "content": _build_user_message(
            question, answer, existing_topics, recent_bullets,
            int(config.get("max_chars", 6000)),
        )},
    ]

    # First attempt
    try:
        raw = _post_ollama(messages, config)
        return _parse_response(raw)
    except (OllamaUnavailableError, OllamaModelError):
        raise   # propagate immediately — don't retry infra errors
    except (ValueError, Exception):
        pass    # fall through to retry

    # Retry once
    try:
        raw = _post_ollama(messages, config)
        return _parse_response(raw)
    except (OllamaUnavailableError, OllamaModelError):
        raise
    except (ValueError, Exception):
        pass

    # Both attempts failed — return fallback so the chain doesn't break
    return _fallback_result(question)
