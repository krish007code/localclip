"""
server.py — HTTP server and pipeline orchestration for LocalClip.

Binds to 127.0.0.1 only.

Architecture (post idempotency fix):
  POST /clip   — validate, dedupe, enqueue new turn ids, return {"status":"accepted"}
  GET  /health — liveness
  GET  /status — last worker result and last error
  OPTIONS *    — 200

A single background worker thread drains a queue.Queue one turn at a time.
The LLM call happens inside the worker, so POST /clip always returns fast.

Duplicate-prevention layers (defence in depth):
  1. store.dedupe_turns  — skips turn ids already past last_closed_id
  2. _processed_ids set  — skips ids already written to nodes.jsonl (in-memory,
                           rebuilt from nodes.jsonl on startup)
  3. _inflight set       — returns {"status":"busy"} if the same id is already
                           being processed right now
  Both sets are protected by _ID_LOCK (same lock as file writes).
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from pathlib import Path
from typing import Any

from server import store, linker, render as renderer
from server import llm as llm_module
from server.llm import OllamaUnavailableError, OllamaModelError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_REPO_ROOT           = Path(__file__).resolve().parent.parent
_CONFIG_PATH         = _REPO_ROOT / "config.json"
_CONFIG_EXAMPLE_PATH = _REPO_ROOT / "config.example.json"
_config_cache: dict[str, Any] | None = None
_DEFAULT_TOKEN = "CHANGE_ME"


def load_config() -> dict[str, Any]:
    """Three-level priority: config.json → config.example.json → LOCALCLIP_TOKEN env."""
    global _config_cache
    if _config_cache is None:
        if _CONFIG_PATH.exists():
            cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        elif _CONFIG_EXAMPLE_PATH.exists():
            cfg = json.loads(_CONFIG_EXAMPLE_PATH.read_text(encoding="utf-8"))
        else:
            raise FileNotFoundError(
                "Neither config.json nor config.example.json found. "
                "Copy config.example.json to config.json and set your token."
            )
        env_token = os.environ.get("LOCALCLIP_TOKEN")
        if env_token:
            cfg["token"] = env_token
        _config_cache = cfg
    return _config_cache


def check_token(config: dict[str, Any], insecure_dev: bool = False) -> None:
    if config.get("token", _DEFAULT_TOKEN) == _DEFAULT_TOKEN and not insecure_dev:
        print(
            "\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "  ERROR: Token is still set to the default 'CHANGE_ME'.\n"
            "\n"
            "  To fix this:\n"
            "    1. cp config.example.json config.json\n"
            "    2. openssl rand -hex 16   (generates a token)\n"
            "    3. Paste the token into config.json  (\"token\" field)\n"
            "    4. Paste the same token into the userscript TOKEN constant\n"
            "\n"
            "  To start anyway for local development (no real security):\n"
            "    uv run python -m server.server --insecure-dev\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n",
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Idempotency state  (module-level, survives across requests)
# ---------------------------------------------------------------------------

_ID_LOCK: threading.Lock = threading.Lock()

# ids of turns already persisted to nodes.jsonl — built at startup,
# maintained incrementally as turns are processed.
_processed_ids: set[str] = set()

# ids currently being processed by the worker — prevents double-enqueue.
_inflight: set[str] = set()


def _bootstrap_processed_ids() -> None:
    """Populate _processed_ids from nodes.jsonl on startup."""
    global _processed_ids
    nodes = store.load_nodes()
    with _ID_LOCK:
        _processed_ids = {
            f"{n.get('account', 'default')}:{n['id']}"
            for n in nodes if "id" in n
        }


# ---------------------------------------------------------------------------
# Background worker queue
# ---------------------------------------------------------------------------

# Each item: (turn_pairs, payload_meta, config)
# where turn_pairs is a list of (user_turn, asst_turn) dicts.
_work_queue: queue.Queue = queue.Queue()

# Last result / error surfaced by GET /status
_last_status: dict[str, Any] = {"status": "idle"}
_last_error:  str | None     = None
_status_lock  = threading.Lock()


def _set_status(result: dict[str, Any], error: str | None = None) -> None:
    with _status_lock:
        global _last_status, _last_error
        _last_status = result
        _last_error  = error


def _worker() -> None:
    """Single background thread: drain _work_queue one item at a time."""
    while True:
        item = _work_queue.get()
        if item is None:          # sentinel — shut down
            break
        new_pairs, payload_meta, config = item
        try:
            result = _process_turns(new_pairs, payload_meta, config)
            _set_status(result)
        except Exception as exc:  # noqa: BLE001
            msg = f"worker exception: {exc}"
            print(f"[error] {msg}", file=sys.stderr)
            _set_status({"status": "error", "message": msg}, error=msg)
        finally:
            # Release inflight regardless of outcome
            _acct = payload_meta.get("account", "default") or "default"
            with _ID_LOCK:
                for user_turn, _ in new_pairs:
                    _inflight.discard(f"{_acct}:{user_turn['id']}")
            _work_queue.task_done()


_worker_thread: threading.Thread | None = None


def _start_worker() -> None:
    global _worker_thread
    if _worker_thread is None or not _worker_thread.is_alive():
        _worker_thread = threading.Thread(target=_worker, name="lc-worker", daemon=True)
        _worker_thread.start()


# ---------------------------------------------------------------------------
# Turn logic (thin wrappers kept for import convenience)
# ---------------------------------------------------------------------------

def close_turns(
    messages: list[dict[str, Any]], flush: bool
) -> list[tuple[dict, dict]]:
    return store.close_turns(messages, flush)


# ---------------------------------------------------------------------------
# Core turn processor  (runs inside the worker thread)
# ---------------------------------------------------------------------------

def _process_turns(
    new_pairs: list[tuple[dict, dict]],
    payload_meta: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Process a list of (user, assistant) pairs sequentially.

    Returns a status dict.  Idempotency is enforced here: any turn whose id
    is already in _processed_ids is silently skipped.
    """
    t0 = time.monotonic()
    conv_id = payload_meta["conversation_id"]
    account = payload_meta.get("account", "default") or "default"

    all_nodes = store.load_nodes()
    existing_topics = sorted({n.get("topic", "") for n in all_nodes if n.get("topic")})
    recent_bullets: list[str] = []
    for n in reversed(all_nodes):
        recent_bullets.extend(n.get("bullets", []))
        if len(recent_bullets) >= 8:
            break
    recent_bullets = recent_bullets[:8]

    conv_nodes = sorted(
        [n for n in all_nodes if n.get("conv_id") == conv_id],
        key=lambda n: n.get("idx", 0),
    )
    prev_id:  str | None = conv_nodes[-1]["id"] if conv_nodes else None
    next_idx: int        = len(conv_nodes)

    saved = 0
    last_id_processed: str | None = None
    last_node: dict[str, Any] = {}
    last_llm_error: str | None = None

    # Load state once for this batch
    state = store.load_state()

    for user_turn, asst_turn in new_pairs:
        turn_id = user_turn["id"]
        acct_id = f"{account}:{turn_id}"

        # Layer-2 idempotency: skip if already persisted
        with _ID_LOCK:
            if acct_id in _processed_ids:
                print(f"[idempotent skip] id={turn_id[:16]}… acct={account}")
                last_id_processed = turn_id
                prev_id = turn_id
                continue

        raw_file = store.save_raw(payload_meta, user_turn, asst_turn)

        question = user_turn.get("markdown", "")
        answer   = asst_turn.get("markdown", "")

        try:
            llm_raw = llm_module.call_ollama(
                question=question,
                answer=answer,
                existing_topics=existing_topics,
                recent_bullets=recent_bullets,
                config=config,
            )
        except OllamaUnavailableError:
            return {"status": "error", "message": "Ollama not running"}
        except OllamaModelError:
            return {"status": "error", "message": f"model not found: {config.get('model', '?')}"}

        last_llm_error = llm_raw.pop("_llm_error", None)

        # ── Skip gate ────────────────────────────────────────────────────────
        if llm_raw.get("skip"):
            q_words   = len(question.split())
            a_chars   = len(answer)
            q_preview = question[:60]
            if q_words < 6 and a_chars < 400:
                print(f"[skip honored ] {q_preview!r}")
                last_id_processed = turn_id
                prev_id = turn_id
                # Mark as processed so retries don't re-enter the LLM
                with _ID_LOCK:
                    _processed_ids.add(acct_id)
                continue
            else:
                print(f"[skip overridden] {q_preview!r}")
                if not llm_raw.get("title", "").strip():
                    words = question.strip().split()
                    llm_raw["title"] = " ".join(words[:8]) or "Untitled"
                if not llm_raw.get("bullets"):
                    sentences = re.split(r"(?<=[.!?])\s+", answer.strip())
                    llm_raw["bullets"] = [s for s in sentences[:3] if s]
        # ─────────────────────────────────────────────────────────────────────

        llm_result = linker.process(llm_raw, existing_topics)

        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        node: dict[str, Any] = {
            "id":       turn_id,
            "conv_id":  conv_id,
            "account":  account,
            "idx":      next_idx,
            "prev":     prev_id,
            "question": question,
            "title":    llm_result.get("title", "Untitled"),
            "topic":    llm_result.get("topic", "Uncategorized"),
            "bullets":  llm_result.get("bullets", []),
            "raw_file": raw_file,
            "ts":       ts,
        }

        store.append_node(node)

        # Mark as processed — layer-2 fence
        with _ID_LOCK:
            _processed_ids.add(acct_id)

        existing_topics = sorted(set(existing_topics) | {node["topic"]})
        recent_bullets  = (node.get("bullets", []) + recent_bullets)[:8]
        prev_id          = turn_id
        next_idx        += 1
        last_id_processed = turn_id
        last_node         = node
        saved            += 1

    if last_id_processed is None:
        return {"status": "skipped"}

    store.update_state(conv_id, last_id_processed, state)
    renderer.rebuild()

    duration_ms = int((time.monotonic() - t0) * 1000)
    topic       = last_node.get("topic", "?") if saved else "?"
    status_str  = "llm_failed" if last_llm_error else "saved"
    print(f"[{status_str}] {topic} | {saved} turn(s) | {duration_ms}ms")

    return {"status": status_str, "count": saved}


# ---------------------------------------------------------------------------
# POST /clip  — fast path: validate → enqueue → return accepted
# ---------------------------------------------------------------------------

def handle_clip(payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Validate payload, filter to new turn ids, and enqueue for the worker.

    Always returns quickly.  Heavy work (LLM call) happens in the worker thread.
    """
    for field in ("url", "source", "conversation_id", "messages", "captured_at"):
        if field not in payload:
            return {"status": "error", "message": f"missing field: {field}"}

    conv_id  = payload["conversation_id"]
    messages = payload["messages"]
    flush    = bool(payload.get("flush", False))

    closed_pairs = close_turns(messages, flush)
    if not closed_pairs:
        return {"status": "duplicate"}

    state     = store.load_state()
    new_pairs = store.dedupe_turns(conv_id, closed_pairs, state)
    if not new_pairs:
        return {"status": "duplicate"}

    # In-flight and idempotency filter
    account    = payload.get("account", "default") or "default"
    to_enqueue: list[tuple[dict, dict]] = []
    busy_ids:   list[str]               = []

    with _ID_LOCK:
        for user_turn, asst_turn in new_pairs:
            tid     = user_turn["id"]
            acct_id = f"{account}:{tid}"
            if acct_id in _processed_ids:
                continue          # already done — treat as duplicate silently
            if acct_id in _inflight:
                busy_ids.append(acct_id)
                continue
            _inflight.add(acct_id)
            to_enqueue.append((user_turn, asst_turn))

    if busy_ids and not to_enqueue:
        return {"status": "busy"}

    if not to_enqueue:
        return {"status": "duplicate"}

    payload_meta = {
        "url":             payload.get("url", ""),
        "title":           payload.get("title", ""),
        "conversation_id": conv_id,
        "account":         payload.get("account", "default") or "default",
        "captured_at":     payload.get("captured_at", ""),
    }

    _work_queue.put((to_enqueue, payload_meta, config))
    return {"status": "accepted", "queued": _work_queue.qsize()}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

_MAX_BODY = 2 * 1024 * 1024  # 2 MB


class LocalClipHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt: str, *args: Any) -> None:  # type: ignore[override]
        print(f"[http] {self.address_string()} - {fmt % args}")

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"status": "ok"})
        elif self.path == "/status":
            with _status_lock:
                body: dict[str, Any] = dict(_last_status)
                if _last_error:
                    body["last_error"] = _last_error
                body["queue_depth"] = _work_queue.qsize()
            self._json(200, body)
        else:
            self._json(404, {"status": "error", "message": "not found"})

    def do_POST(self) -> None:
        if self.path != "/clip":
            self._json(404, {"status": "error", "message": "not found"})
            return

        config = load_config()

        token = self.headers.get("X-LocalClip-Token", "")
        if token != config.get("token", ""):
            self._json(401, {"status": "error", "message": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length", 0))
        if length > _MAX_BODY:
            self._json(413, {"status": "error", "message": "payload too large"})
            return

        raw_body = self.rfile.read(length)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._json(400, {"status": "error", "message": f"bad json: {exc}"})
            return

        try:
            result = handle_clip(payload, config)
        except Exception as exc:  # noqa: BLE001
            print(f"[error] pipeline exception: {exc}", file=sys.stderr)
            self._json(500, {"status": "error", "message": str(exc)})
            return

        self._json(200, result)

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
    _bootstrap_processed_ids()
    _start_worker()
    port   = int(config.get("port", 8787))
    server = _ThreadingServer(("127.0.0.1", port), LocalClipHandler)
    print(f"[server] LocalClip listening on http://127.0.0.1:{port}")
    server.serve_forever()


# ---------------------------------------------------------------------------
# Reprocess queued entries (placeholder — CP4)
# ---------------------------------------------------------------------------

def reprocess_queued(config: dict[str, Any]) -> None:
    state  = store.load_state()
    queued = state.get("queued", [])
    if not queued:
        print("[requeue] Nothing queued.")
        return
    print(f"[requeue] {len(queued)} queued entry/entries — Ollama wired in CP4.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="LocalClip server")
    parser.add_argument("--reprocess-queued", action="store_true")
    parser.add_argument(
        "--insecure-dev", action="store_true",
        help="Allow starting with the default CHANGE_ME token",
    )
    args   = parser.parse_args()
    config = load_config()
    check_token(config, insecure_dev=args.insecure_dev)

    if args.reprocess_queued:
        reprocess_queued(config)
    else:
        run_server(config)


if __name__ == "__main__":
    main()
