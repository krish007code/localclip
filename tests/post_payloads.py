"""
tests/post_payloads.py — smoke test: POST sample payloads and show vault output.

Usage:
    uv run python tests/post_payloads.py

The server must be running:
    uv run python -m server.server

Sends 4 requests in order and asserts the expected status sequence:
  1. chat.json         → saved   (2 closed turns from 3-pair conv, last pending)
  2. duplicate.json    → duplicate (same turns already processed)
  3. chat_continued.json → saved  (2 new closed turns from extended conv)
  4. branch.json       → saved   (new conversation, creates a branch scenario)
"""

from __future__ import annotations

import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SERVER = "http://127.0.0.1:8787"
TOKEN = "CHANGE_ME"
PAYLOADS_DIR = Path(__file__).parent / "payloads"


# ---------------------------------------------------------------------------
# HTTP helper (stdlib only)
# ---------------------------------------------------------------------------

def post_clip(payload_path: Path) -> dict:
    data = json.dumps(json.loads(payload_path.read_text())).encode()
    req = urllib.request.Request(
        f"{SERVER}/clip",
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-LocalClip-Token": TOKEN,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"status": "http_error", "code": e.code, "body": e.read().decode()}
    except urllib.error.URLError as e:
        print(f"\n[ERROR] Cannot reach server at {SERVER}: {e.reason}")
        print("Start it with:  uv run python -m server.server")
        sys.exit(1)


def health_check() -> None:
    req = urllib.request.Request(f"{SERVER}/health")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            r = json.loads(resp.read())
            assert r.get("status") == "ok", f"Unexpected health response: {r}"
    except urllib.error.URLError as e:
        print(f"[ERROR] Server not reachable: {e.reason}")
        print("Start it with:  uv run python -m server.server")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Vault display helpers
# ---------------------------------------------------------------------------

def print_vault_tree(vault: Path) -> None:
    files = sorted(vault.glob("*.md"))
    print(f"\nvault/ ({len(files)} files)")
    for f in files:
        size = f.stat().st_size
        print(f"  {f.name}  ({size} bytes)")


def print_file(vault: Path, partial_name: str) -> None:
    matches = [f for f in vault.glob("*.md") if partial_name.lower() in f.name.lower()]
    if not matches:
        print(f"  (no file matching '{partial_name}')")
        return
    f = sorted(matches)[0]
    print(f"\n{'─'*60}")
    print(f"  {f.name}")
    print(f"{'─'*60}")
    print(f.read_text(encoding="utf-8"))
    print(f"{'─'*60}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    vault = Path(__file__).parent.parent / "vault"

    print("=" * 60)
    print("LocalClip Smoke Test")
    print("=" * 60)

    health_check()
    print("[health] OK\n")

    steps = [
        ("chat.json",           "saved",     "Initial clip: 2 closed turns (3rd pending)"),
        ("duplicate.json",      "duplicate", "Re-clip same turns → duplicate"),
        ("chat_continued.json", "saved",     "Continued conv: 2 more closed turns"),
    ]

    all_ok = True
    for fname, expected_status, description in steps:
        path = PAYLOADS_DIR / fname
        result = post_clip(path)
        actual = result.get("status", "?")
        ok = actual == expected_status
        mark = "✓" if ok else "✗"
        detail = f"count={result.get('count', '-')}" if "count" in result else ""
        print(f"  {mark} {fname:<30} → {actual:<12} {detail}   [{description}]")
        if not ok:
            print(f"      expected: {expected_status}, got: {result}")
            all_ok = False

    # Branch scenario: second conversation
    branch_payload = {
        "url": "https://chatgpt.com/c/bbbbcccc-0000-0000-0000-000000000002",
        "title": "Async Python Deep Dive",
        "source": "chat",
        "conversation_id": "bbbbcccc-0000-0000-0000-000000000002",
        "flush": True,
        "captured_at": "2025-10-05T11:00:00Z",
        "messages": [
            {"id": "b_u1", "role": "user",      "markdown": "What is the event loop in asyncio?"},
            {"id": "b_a1", "role": "assistant", "markdown": "The event loop runs async tasks. It schedules coroutines and I/O callbacks, running one at a time on a single thread."},
            {"id": "b_u2", "role": "user",      "markdown": "How does asyncio.gather work?"},
            {"id": "b_a2", "role": "assistant", "markdown": "`asyncio.gather` runs multiple coroutines concurrently and returns their results as a list."},
            {"id": "b_u3", "role": "user",      "markdown": "What is the difference between asyncio.gather and asyncio.wait?"},
            {"id": "b_a3", "role": "assistant", "markdown": "`gather` raises on first exception; `wait` gives you fine-grained control via FIRST_COMPLETED / FIRST_EXCEPTION / ALL_COMPLETED."},
        ],
    }

    # Post the branch conversation (flush=True so all 3 turns processed)
    data = json.dumps(branch_payload).encode()
    req = urllib.request.Request(
        f"{SERVER}/clip",
        data=data,
        headers={"Content-Type": "application/json", "X-LocalClip-Token": TOKEN},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
    ok = result.get("status") == "saved"
    mark = "✓" if ok else "✗"
    print(f"  {mark} branch_payload (inline)           → {result.get('status'):<12} count={result.get('count','-')}   [New conv, 3 turns, flush=True]")
    if not ok:
        all_ok = False

    print()
    print_vault_tree(vault)

    # Print the first node note from the first conversation
    print("\n[Sample note — first turn of conv aaaabbbb]")
    print_file(vault, "aaaabbbb-0 ")

    # Print _Index.md
    print("\n[_Index.md]")
    index = vault / "_Index.md"
    if index.exists():
        print(index.read_text(encoding="utf-8"))

    print()
    if all_ok:
        print("All assertions passed ✓")
    else:
        print("Some assertions FAILED ✗")
        sys.exit(1)


if __name__ == "__main__":
    main()
