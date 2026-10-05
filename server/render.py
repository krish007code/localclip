"""
render.py — deterministic, idempotent vault renderer for LocalClip.

Reads data/nodes.jsonl and writes:
  vault/<conv_short>-<idx> <title>.md   — one note per closed turn
  vault/<Topic>.md                       — one hub per topic
  vault/_Index.md                        — global index

`next` is NEVER stored in nodes.jsonl; it is derived at render time by
inverting the `prev` pointers.

Running rebuild() twice on the same nodes.jsonl produces byte-for-byte
identical output.
"""

from __future__ import annotations

import argparse
import re
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_NODES = _REPO_ROOT / "data" / "nodes.jsonl"
_DEFAULT_VAULT = _REPO_ROOT / "vault"


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------

_FORBIDDEN = re.compile(r'[\\/:*?"<>|]')


def sanitize_filename(name: str) -> str:
    """Remove characters forbidden in filenames on Windows and Linux."""
    return _FORBIDDEN.sub("", name).strip()


def node_filename(node: dict[str, Any]) -> str:
    """Return the vault filename (without .md) for a node.

    Format: <conv_short>-<idx> <sanitized_title>
    conv_short = first 8 chars of conv_id.
    """
    conv_short = (node.get("conv_id") or "unknown")[:8]
    idx = node.get("idx", 0)
    title = sanitize_filename(node.get("title") or "Untitled")
    return f"{conv_short}-{idx} {title}"


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------

def build_next_map(nodes: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Invert prev pointers to produce {node_id → [successor_node_id, ...]}.

    Nodes are processed in (conv_id, idx) order so that branch ordering is
    stable across runs (deterministic).
    """
    sorted_nodes = sorted(nodes, key=lambda n: (n.get("conv_id", ""), n.get("idx", 0)))
    nxt: dict[str, list[str]] = defaultdict(list)
    for node in sorted_nodes:
        prev = node.get("prev")
        if prev:
            nxt[prev].append(node["id"])
    return dict(nxt)


# ---------------------------------------------------------------------------
# Node note renderer
# ---------------------------------------------------------------------------

def render_node_file(
    node: dict[str, Any],
    nodes_by_id: dict[str, dict[str, Any]],
    next_map: dict[str, list[str]],
    all_filenames: set[str],
) -> str:
    """Render the full markdown content for a single node note."""
    conv_id = node.get("conv_id", "")
    conv_short = conv_id[:8]
    idx = node.get("idx", 0)
    node_id = node["id"]
    topic = node.get("topic", "Uncategorized")
    ts = node.get("ts", "")
    title = node.get("title") or "Untitled"
    bullets = node.get("bullets") or []
    raw_file = node.get("raw_file", "")

    # --- Prev line ---
    prev_id = node.get("prev")
    if prev_id is None:
        prev_line = "Prev: (start)"
    else:
        prev_node = nodes_by_id.get(prev_id)
        if prev_node is None:
            # Dangling prev — target doesn't exist; omit the link
            prev_line = "Prev: (unknown)"
        else:
            prev_fname = node_filename(prev_node)
            if prev_fname in all_filenames:
                prev_line = f"Prev: [[{prev_fname}]]"
            else:
                # Target exists in map but file was somehow missing — omit
                prev_line = "Prev: (unknown)"

    # --- Next line ---
    successor_ids = next_map.get(node_id, [])
    if not successor_ids:
        next_line = "Next: (waiting)"
    else:
        links = []
        for sid in successor_ids:
            s_node = nodes_by_id.get(sid)
            if s_node is None:
                continue
            s_fname = node_filename(s_node)
            if s_fname in all_filenames:
                links.append(f"[[{s_fname}]]")
        if links:
            next_line = "Next: " + " ".join(links)
        else:
            next_line = "Next: (waiting)"

    # --- Bullets ---
    bullet_lines = "\n".join(f"- {b}" for b in bullets) if bullets else "- (no bullets)"

    # --- Assemble ---
    lines = [
        "---",
        f"id: {node_id}",
        f"conv_id: {conv_id}",
        f"idx: {idx}",
        f"topic: {topic}",
        f"ts: {ts}",
        "---",
        "",
        f"# {title}",
        "",
        f"**Topic:** [[{topic}]]",
        f"{prev_line} | {next_line}",
        "",
        bullet_lines,
        "",
        f"Raw: [[raw/{raw_file}]]",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Topic hub renderer
# ---------------------------------------------------------------------------

def render_topic_file(
    topic: str,
    topic_nodes: list[dict[str, Any]],
    all_filenames: set[str],
) -> str:
    """Render the hub note for *topic* listing all its nodes."""
    # Sort by ts ascending (deterministic)
    sorted_nodes = sorted(topic_nodes, key=lambda n: n.get("ts", ""))

    node_lines = []
    for n in sorted_nodes:
        fname = node_filename(n)
        if fname not in all_filenames:
            continue
        ts = n.get("ts", "")
        date = ts[:10] if ts else "?"
        node_lines.append(f"- [[{fname}]] — {date}")

    body = "\n".join(node_lines) if node_lines else "- (no nodes yet)"

    lines = [
        f"# {topic}",
        "",
        "## Nodes",
        "",
        body,
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Index renderer
# ---------------------------------------------------------------------------

def render_index(nodes: list[dict[str, Any]]) -> str:
    """Render vault/_Index.md from the full node list."""
    # --- Topics ---
    topic_map: dict[str, list[dict]] = defaultdict(list)
    for n in nodes:
        topic_map[n.get("topic", "Uncategorized")].append(n)

    topic_stats = []
    for topic, tnodes in topic_map.items():
        count = len(tnodes)
        last_ts = max(n.get("ts", "") for n in tnodes)
        last_date = last_ts[:10] if last_ts else "?"
        topic_stats.append((topic, count, last_date, last_ts))
    # Sort by last_ts descending, then topic name
    topic_stats.sort(key=lambda x: (-ord(x[3][0]) if x[3] else 0, x[0]))
    topic_stats.sort(key=lambda x: x[3], reverse=True)

    topic_lines = []
    for topic, count, last_date, _ in topic_stats:
        safe = sanitize_filename(topic)
        topic_lines.append(f"- [[{safe}]] — {count} node(s), last updated {last_date}")

    # --- Conversations ---
    conv_map: dict[str, list[dict]] = defaultdict(list)
    for n in nodes:
        conv_map[n.get("conv_id", "unknown")].append(n)

    conv_stats = []
    for conv_id, cnodes in conv_map.items():
        count = len(cnodes)
        last_ts = max(n.get("ts", "") for n in cnodes)
        last_date = last_ts[:10] if last_ts else "?"
        conv_stats.append((conv_id, count, last_date, last_ts))
    conv_stats.sort(key=lambda x: x[3], reverse=True)

    conv_lines = []
    for conv_id, count, last_date, _ in conv_stats:
        short = conv_id[:8]
        conv_lines.append(f"- `{conv_id}` ({short}…) — {count} node(s), last {last_date}")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# LocalClip Index",
        "",
        f"_Last rebuilt: {now}_",
        "",
        "## Topics",
        "",
        "\n".join(topic_lines) if topic_lines else "- (none yet)",
        "",
        "## Conversations",
        "",
        "\n".join(conv_lines) if conv_lines else "- (none yet)",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main rebuild
# ---------------------------------------------------------------------------

def rebuild(
    nodes_path: Path | None = None,
    vault_path: Path | None = None,
) -> None:
    """Full re-render: clear vault, write all node notes, topic hubs, _Index."""
    if nodes_path is None:
        nodes_path = _DEFAULT_NODES
    if vault_path is None:
        vault_path = _DEFAULT_VAULT

    # Import here to avoid circular imports when store.py imports render
    from server.store import load_nodes as _load_nodes  # noqa: PLC0415
    nodes = _load_nodes() if nodes_path == _DEFAULT_NODES else _load_nodes_from(nodes_path)

    vault_path.mkdir(parents=True, exist_ok=True)

    # Deduplicate by id: if nodes.jsonl somehow contains the same id more than
    # once (e.g. from a crash mid-write), keep only the first occurrence.
    seen_ids: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for n in nodes:
        nid = n.get("id")
        if nid and nid not in seen_ids:
            seen_ids.add(nid)
            deduped.append(n)
        elif nid:
            print(f"[render] duplicate id {nid[:16]}… — keeping first, skipping later")
    nodes = deduped

    # Clear existing .md files (preserves .gitkeep and subdirectories)
    for f in vault_path.glob("*.md"):
        f.unlink()

    if not nodes:
        # Write empty index so the vault is still valid
        (vault_path / "_Index.md").write_text(render_index([]), encoding="utf-8")
        return

    nodes_by_id = {n["id"]: n for n in nodes}
    next_map = build_next_map(nodes)

    # Compute all filenames first so Prev/Next links can be validated
    all_filenames: set[str] = {node_filename(n) for n in nodes}

    # Write node notes
    for node in nodes:
        content = render_node_file(node, nodes_by_id, next_map, all_filenames)
        fname = node_filename(node) + ".md"
        (vault_path / fname).write_text(content, encoding="utf-8")

    # Write topic hubs
    topic_map: dict[str, list[dict]] = defaultdict(list)
    for n in nodes:
        topic_map[n.get("topic", "Uncategorized")].append(n)
    for topic, tnodes in topic_map.items():
        content = render_topic_file(topic, tnodes, all_filenames)
        fname = sanitize_filename(topic) + ".md"
        (vault_path / fname).write_text(content, encoding="utf-8")

    # Write index
    (vault_path / "_Index.md").write_text(render_index(nodes), encoding="utf-8")


def _load_nodes_from(path: Path) -> list[dict[str, Any]]:
    """Load nodes from an arbitrary path (used in tests / CLI)."""
    import json  # noqa: PLC0415
    if not path.exists():
        return []
    nodes = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            nodes.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"[render] WARNING: malformed line {i} in {path} — skipped")
    return nodes


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="LocalClip vault renderer")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Clear and re-render the entire vault from data/nodes.jsonl",
    )
    args = parser.parse_args()
    if args.rebuild:
        print("[render] Rebuilding vault …")
        rebuild()
        print("[render] Done.")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
