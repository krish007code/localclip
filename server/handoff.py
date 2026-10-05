"""
handoff.py — deterministic handoff brief builder for LocalClip.

Usage:
    python -m server.handoff --topic "<topic>" [--account <label>]

Builds a markdown brief for the given topic from nodes.jsonl.
No LLM call — entirely deterministic.

Output:
    vault/Handoffs/<sanitized_topic>.md   (also printed to stdout)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT   = Path(__file__).resolve().parent.parent
_DEFAULT_VAULT = _REPO_ROOT / "vault"


# ---------------------------------------------------------------------------
# Brief builder
# ---------------------------------------------------------------------------

def _load_nodes() -> list[dict[str, Any]]:
    """Import store at call time to avoid circular imports."""
    from server.store import load_nodes  # noqa: PLC0415
    return load_nodes()


def _sanitize(name: str) -> str:
    """Remove vault-unsafe characters from a filename component."""
    import re  # noqa: PLC0415
    return re.sub(r'[\\/:*?"<>|]', "", name).strip()


def build_brief(
    topic: str,
    account: str | None = None,
    nodes: list[dict[str, Any]] | None = None,
) -> str:
    """Return the markdown text of a handoff brief.

    Parameters
    ----------
    topic:   exact topic name (case-sensitive match against node["topic"])
    account: if given, restrict to nodes with this account label;
             if None, include all accounts
    nodes:   pre-loaded node list (used in tests); loads from disk if None
    """
    if nodes is None:
        nodes = _load_nodes()

    # Filter to this topic
    topic_nodes = [n for n in nodes if n.get("topic") == topic]
    if not topic_nodes:
        return ""

    # Optionally filter by account
    if account:
        topic_nodes = [n for n in topic_nodes
                       if (n.get("account") or "default") == account]
        if not topic_nodes:
            return ""

    # Sort by timestamp ascending — deterministic across accounts
    topic_nodes.sort(key=lambda n: n.get("ts", ""))

    # Header metadata
    accounts_present = sorted({n.get("account") or "default" for n in topic_nodes})
    first_ts = topic_nodes[0].get("ts", "")[:10] or "?"
    last_ts  = topic_nodes[-1].get("ts", "")[:10] or "?"
    date_range = first_ts if first_ts == last_ts else f"{first_ts} → {last_ts}"
    acct_str   = ", ".join(accounts_present)

    lines: list[str] = [
        f"# Handoff: {topic}",
        "",
        f"**Topic:** {topic}  ",
        f"**Accounts:** {acct_str}  ",
        f"**Date range:** {date_range}  ",
        f"**Turns:** {len(topic_nodes)}",
        "",
        "---",
        "",
    ]

    # Per-turn section
    for node in topic_nodes:
        node_account = node.get("account") or "default"
        title        = node.get("title") or "Untitled"
        ts_date      = (node.get("ts") or "")[:10] or "?"
        bullets      = node.get("bullets") or []

        lines.append(f"## {title}")
        lines.append(f"*{ts_date} · `{node_account}`*")
        lines.append("")
        if bullets:
            for b in bullets:
                lines.append(f"- {b}")
        else:
            q = (node.get("question") or "").strip()
            if q:
                lines.append(f"- _{q[:120]}_")
        lines.append("")

    # Paste-ready context block
    lines += [
        "---",
        "",
        "> Here is context from my earlier conversations on "
        f"**{topic}**. Treat it as background and continue from the last point.",
        "",
    ]

    return "\n".join(lines)


def list_topics(nodes: list[dict[str, Any]] | None = None) -> list[str]:
    """Return sorted list of all distinct topic names in nodes.jsonl."""
    if nodes is None:
        nodes = _load_nodes()
    return sorted({n.get("topic") for n in nodes if n.get("topic")})


def save_brief(topic: str, content: str, vault_path: Path | None = None) -> Path:
    """Write content to vault/Handoffs/<topic>.md and return the path."""
    if vault_path is None:
        vault_path = _DEFAULT_VAULT
    handoffs_dir = vault_path / "Handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    out = handoffs_dir / f"{_sanitize(topic)}.md"
    out.write_text(content, encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a handoff brief for a topic from LocalClip notes."
    )
    parser.add_argument("--topic",   required=True,  help="Topic name (exact match)")
    parser.add_argument("--account", default=None,   help="Restrict to one account label")
    args = parser.parse_args()

    nodes = _load_nodes()
    topics = list_topics(nodes)

    if args.topic not in topics:
        if topics:
            print(f"Topic {args.topic!r} not found.\n\nAvailable topics:", file=sys.stderr)
            for t in topics:
                print(f"  {t}", file=sys.stderr)
        else:
            print("No topics found — nodes.jsonl is empty.", file=sys.stderr)
        sys.exit(1)

    content = build_brief(args.topic, account=args.account, nodes=nodes)
    if not content:
        print(
            f"No nodes for topic={args.topic!r}"
            + (f" account={args.account!r}" if args.account else ""),
            file=sys.stderr,
        )
        sys.exit(1)

    out = save_brief(args.topic, content)
    print(str(out))


if __name__ == "__main__":
    main()
