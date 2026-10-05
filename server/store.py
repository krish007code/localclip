"""
store.py — state, deduplication, and persistence for LocalClip.

Single source of truth: data/nodes.jsonl (append-only, never stores `next`).
All writes to nodes.jsonl and state.json are atomic (temp+rename) and protected
by a single threading.Lock.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths (resolved relative to repo root: two levels up from this file)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = _REPO_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
NODES_FILE = DATA_DIR / "nodes.jsonl"
STATE_FILE = DATA_DIR / "state.json"

# ---------------------------------------------------------------------------
# Module-level write lock — shared by all callers in the same process
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def slug(text: str, max_len: int = 40) -> str:
    """Return a filesystem-safe slug from *text*.

    Lowercases, replaces runs of non-alphanumeric characters with '-',
    strips leading/trailing '-', and truncates to *max_len*.
    """
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text[:max_len]


# ---------------------------------------------------------------------------
# State file helpers
# ---------------------------------------------------------------------------

def load_state() -> dict[str, Any]:
    """Read data/state.json.  Returns {} if the file is missing or unreadable."""
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    """Atomically overwrite data/state.json.  Protected by _LOCK."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(STATE_FILE, json.dumps(state, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# nodes.jsonl helpers
# ---------------------------------------------------------------------------

def load_nodes() -> list[dict[str, Any]]:
    """Read every line from data/nodes.jsonl.  Skips and warns on bad lines."""
    if not NODES_FILE.exists():
        return []
    nodes = []
    for i, line in enumerate(NODES_FILE.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            nodes.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"[store] WARNING: malformed line {i} in nodes.jsonl — skipped")
    return nodes


def append_node(node: dict[str, Any]) -> None:
    """Atomically append one JSON line to data/nodes.jsonl.  Protected by _LOCK."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        # Read current content (may not exist yet)
        existing = NODES_FILE.read_text(encoding="utf-8") if NODES_FILE.exists() else ""
        # Ensure existing content ends with a newline before appending
        if existing and not existing.endswith("\n"):
            existing += "\n"
        new_content = existing + json.dumps(node, ensure_ascii=False) + "\n"
        _atomic_write(NODES_FILE, new_content)


# ---------------------------------------------------------------------------
# Turn pairing (messages list → closed (user, assistant) pairs)
# ---------------------------------------------------------------------------

def pair_turns(messages: list[dict[str, Any]]) -> list[tuple[dict, dict]]:
    """Pair consecutive user+assistant messages into turns.

    Scans left-to-right; skips any message that doesn't fit the expected
    pattern so that an orphaned trailing message is silently dropped.
    Returns a list of (user_msg, assistant_msg) tuples.
    """
    pairs: list[tuple[dict, dict]] = []
    i = 0
    while i < len(messages) - 1:
        if messages[i]["role"] == "user" and messages[i + 1]["role"] == "assistant":
            pairs.append((messages[i], messages[i + 1]))
            i += 2
        else:
            i += 1
    return pairs


def close_turns(
    messages: list[dict[str, Any]], flush: bool
) -> list[tuple[dict, dict]]:
    """Return only *closed* (user, assistant) pairs.

    The last pair is PENDING (not processed) unless flush=True.
    A single pair with flush=False → returns [].
    """
    pairs = pair_turns(messages)
    if not flush and pairs:
        pairs = pairs[:-1]
    return pairs


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def dedupe_turns(
    conv_id: str,
    closed_pairs: list[tuple[dict, dict]],
    state: dict[str, Any],
) -> list[tuple[dict, dict]]:
    """Return only the closed pairs that have not yet been processed.

    Uses state["conversations"][conv_id]["last_closed_id"] as the cursor.
    If the cursor id is not found in the current batch, all pairs are treated
    as new (handles replay / out-of-order delivery gracefully).
    """
    if not closed_pairs:
        return []

    conv = state.get("conversations", {}).get(conv_id, {})
    last_id = conv.get("last_closed_id")

    if last_id is None:
        return closed_pairs  # first time seeing this conversation

    ids = [u["id"] for u, _ in closed_pairs]
    if last_id not in ids:
        return closed_pairs  # cursor not in batch → treat all as new

    idx = ids.index(last_id)
    return closed_pairs[idx + 1 :]


# ---------------------------------------------------------------------------
# Raw archive
# ---------------------------------------------------------------------------

def save_raw(
    payload: dict[str, Any],
    user_turn: dict[str, Any],
    asst_turn: dict[str, Any],
) -> str:
    """Write one raw markdown file to data/raw/ and return the basename."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    title_slug = slug(payload.get("title", "untitled"), 30)
    filename = f"{ts}-{title_slug}.md"
    path = RAW_DIR / filename

    content = (
        f"---\n"
        f"url: {payload.get('url', '')}\n"
        f"title: {payload.get('title', '')}\n"
        f"conversation_id: {payload.get('conversation_id', '')}\n"
        f"captured_at: {payload.get('captured_at', '')}\n"
        f"---\n\n"
        f"## Question\n\n{user_turn.get('markdown', '')}\n\n"
        f"## Answer\n\n{asst_turn.get('markdown', '')}\n"
    )
    path.write_text(content, encoding="utf-8")
    return filename


# ---------------------------------------------------------------------------
# State updates
# ---------------------------------------------------------------------------

def update_state(
    conv_id: str,
    new_last_id: str,
    state: dict[str, Any],
) -> None:
    """Advance last_closed_id and increment count for *conv_id*, then save."""
    convs = state.setdefault("conversations", {})
    conv = convs.setdefault(conv_id, {"last_closed_id": None, "count": 0})
    conv["last_closed_id"] = new_last_id
    conv["count"] = conv.get("count", 0) + 1
    save_state(state)


# ---------------------------------------------------------------------------
# Internal: atomic file write
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, data: str) -> None:
    """Write *data* to *path* atomically via a temp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
