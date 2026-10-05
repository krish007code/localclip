"""
server.py — HTTP server and pipeline orchestration for LocalClip.

Binds to 127.0.0.1 only.  Checkpoint-1 uses a deterministic stub LLM;
the real Ollama call is wired in Checkpoint 3.

Endpoints:
  GET  /health  → {"status": "ok"}
  POST /clip    → pipeline
  OPTIONS *     → 200
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Sibling imports (server/ is a package)
# ---------------------------------------------------------------------------

from server import store, linker, render as renderer

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _REPO_ROOT / "config.json"
_config_cache: dict[str, Any] | None = None


def load_config() -> dict[str, Any]:
    """Read config.json and cache the result for the lifetime of the process."""
    global _config_cache
    if _config_cache is None:
        _config_cache = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    return _config_cache


# ---------------------------------------------------------------------------
# Stub LLM (Checkpoint 1)
# Replaced in Checkpoint 3 by llm.call_ollama.
# ---------------------------------------------------------------------------

_STUB_COUNTER: dict[str, int] = {}  # deterministic per-topic counter


def stub_llm(
    question: str,
    answer: str,
    existing_topics: list[str],
    _recent_bullets: list[str],
    _config: dict[str, Any],
) -> dict[str, Any]:
    """Deterministic fake LLM output for testing.

    Topic cycles through three values so consecutive turns get different topics,
    exercising both the topic-hub and the _Index renderers.
    """
    topics = ["Python Basics", "Machine Learning", "Data Structures"]
    # Use a simple hash of the question to pick a deterministic topic
    idx = hash(question) % len(topics)
    # But if an existing topic matches, prefer it
    raw_topic = topics[idx]
    if existing_topics:
        raw_topic = existing_topics[hash(question) % len(existing_topics)]

    # Short deterministic title
    words = question.strip().split()
    title = " ".join(words[:6]) or "Untitled"

    return {
        "skip": False,
        "title": title,
        "topic": raw_topic,
        "bullets": [
            f"Key point from: {question[:60]}",
            "Stub bullet — replace with real LLM in Checkpoint 3.",
        ],
    }


# ---------------------------------------------------------------------------
# Turn logic
# ---------------------------------------------------------------------------

def close_turns(
    messages: list[dict[str, Any]], flush: bool
) -> list[tuple[dict, dict]]:
    """Delegate to store.close_turns (kept here for import convenience)."""
    return store.close_turns(messages, flush)


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

def handle_clip(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Run the full ingestion pipeline for one POST /clip request.

    Returns a dict that becomes the JSON response body.
    """
    t0 = time.monotonic()

    # --- Validate required fields ---
    for field in ("url", "source", "conversation_id", "messages", "captured_at"):
        if field not in payload:
            return {"status": "error", "message": f"missing field: {field}"}

    conv_id: str = payload["conversation_id"]
    messages: list[dict] = payload["messages"]
    flush: bool = bool(payload.get("flush", False))

    # --- Determine closed turns ---
    closed_pairs = close_turns(messages, flush)

    if not closed_pairs:
        # Nothing to process yet (all pending, or empty payload)
        return {"status": "duplicate"}

    # --- Deduplicate against state ---
    state = store.load_state()
    new_pairs = store.dedupe_turns(conv_id, closed_pairs, state)

    if not new_pairs:
        return {"status": "duplicate"}

    # --- Load existing nodes for context ---
    all_nodes = store.load_nodes()
    existing_topics = sorted({n.get("topic", "") for n in all_nodes if n.get("topic")})
    recent_bullets: list[str] = []
    for n in reversed(all_nodes):
        recent_bullets.extend(n.get("bullets", []))
        if len(recent_bullets) >= 8:
            break
    recent_bullets = recent_bullets[:8]

    # Determine the prev id: last processed node for this conversation
    conv_nodes = [n for n in all_nodes if n.get("conv_id") == conv_id]
    conv_nodes.sort(key=lambda n: n.get("idx", 0))
    prev_id: str | None = conv_nodes[-1]["id"] if conv_nodes else None
    next_idx = len(conv_nodes)

    saved = 0
    last_id_processed: str | None = None

    for user_turn, asst_turn in new_pairs:
        # Save raw archive
        raw_file = store.save_raw(payload, user_turn, asst_turn)

        # LLM call (stub in Checkpoint 1)
        llm_raw = stub_llm(
            question=user_turn.get("markdown", ""),
            answer=asst_turn.get("markdown", ""),
            existing_topics=existing_topics,
            _recent_bullets=recent_bullets,
            _config=config,
        )

        # Skip handling
        if llm_raw.get("skip"):
            # Advance cursor but don't write a node
            last_id_processed = user_turn["id"]
            prev_id = user_turn["id"]
            continue

        # Post-process topic (normalize + fuzzy-match)
        llm_result = linker.process(llm_raw, existing_topics)

        # Build node record
        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        node: dict[str, Any] = {
            "id":       user_turn["id"],
            "conv_id":  conv_id,
            "idx":      next_idx,
            "prev":     prev_id,
            "question": user_turn.get("markdown", ""),
            "title":    llm_result.get("title", "Untitled"),
            "topic":    llm_result.get("topic", "Uncategorized"),
            "bullets":  llm_result.get("bullets", []),
            "raw_file": raw_file,
            "ts":       ts,
        }

        store.append_node(node)

        # Update context for next iteration
        existing_topics = sorted(set(existing_topics) | {node["topic"]})
        recent_bullets = (node.get("bullets", []) + recent_bullets)[:8]
        prev_id = node["id"]
        next_idx += 1
        last_id_processed = user_turn["id"]
        saved += 1

    if last_id_processed is None:
        # All turns were skipped by LLM
        return {"status": "skipped"}

    # Advance state cursor to last processed user-turn id
    store.update_state(conv_id, last_id_processed, state)

    # Re-render vault
    renderer.rebuild()

    duration_ms = int((time.monotonic() - t0) * 1000)
    last_topic = node.get("topic", "?") if saved else "?"  # type: ignore[possibly-undefined]
    print(f"[saved] {last_topic} | {saved} turn(s) | {duration_ms}ms")

    return {"status": "saved", "count": saved}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

_MAX_BODY = 2 * 1024 * 1024  # 2 MB


class LocalClipHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler for LocalClip server."""

    def log_message(self, fmt: str, *args: Any) -> None:  # type: ignore[override]
        # Replace default stderr logging with stdout
        print(f"[http] {self.address_string()} - {fmt % args}")

    # --- OPTIONS (pre-flight) ---
    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self.end_headers()

    # --- GET /health ---
    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"status": "error", "message": "not found"})

    # --- POST /clip ---
    def do_POST(self) -> None:
        if self.path != "/clip":
            self._json(404, {"status": "error", "message": "not found"})
            return

        config = load_config()

        # --- Auth ---
        token = self.headers.get("X-LocalClip-Token", "")
        if token != config.get("token", ""):
            self._json(401, {"status": "error", "message": "unauthorized"})
            return

        # --- Size check ---
        length = int(self.headers.get("Content-Length", 0))
        if length > _MAX_BODY:
            self._json(413, {"status": "error", "message": "payload too large"})
            return

        # --- Parse body ---
        raw_body = self.rfile.read(length)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._json(400, {"status": "error", "message": f"bad json: {exc}"})
            return

        # --- Run pipeline ---
        try:
            result = handle_clip(payload, config)
        except Exception as exc:  # noqa: BLE001
            print(f"[error] pipeline exception: {exc}", file=sys.stderr)
            self._json(500, {"status": "error", "message": str(exc)})
            return

        self._json(200, result)

    # --- Helper ---
    def _json(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Threading server
# ---------------------------------------------------------------------------

class _ThreadingServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def run_server(config: dict[str, Any]) -> None:
    port = int(config.get("port", 8787))
    server = _ThreadingServer(("127.0.0.1", port), LocalClipHandler)
    print(f"[server] LocalClip listening on http://127.0.0.1:{port}")
    server.serve_forever()


# ---------------------------------------------------------------------------
# Reprocess queued entries (placeholder — wired in Checkpoint 4)
# ---------------------------------------------------------------------------

def reprocess_queued(config: dict[str, Any]) -> None:
    """Retry all entries in state["queued"].  Full implementation in CP4."""
    state = store.load_state()
    queued = state.get("queued", [])
    if not queued:
        print("[requeue] Nothing queued.")
        return
    print(f"[requeue] {len(queued)} queued entry/entries — Ollama wired in Checkpoint 3.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="LocalClip server")
    parser.add_argument(
        "--reprocess-queued",
        action="store_true",
        help="Retry all queued entries (requires Ollama running)",
    )
    args = parser.parse_args()

    config = load_config()
    if args.reprocess_queued:
        reprocess_queued(config)
    else:
        run_server(config)


if __name__ == "__main__":
    main()
