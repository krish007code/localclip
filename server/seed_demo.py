"""
seed_demo.py — populate a self-contained demo vault for LocalClip.

Usage:
    python -m server.seed_demo

What it does:
  1. Creates  demo/nodes.jsonl  (12 fake turns, 2 accounts, 3 topics)
  2. Runs the vault renderer on it → demo/vault/
  3. Prints the vault path and tells you how to run the handoff command.

Never touches  data/nodes.jsonl  or  vault/.

Reuses server.render.rebuild() and server.handoff.list_topics() — no new deps.
"""

from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT  = Path(__file__).resolve().parent.parent
DEMO_DIR    = _REPO_ROOT / "demo"
DEMO_NODES  = DEMO_DIR   / "nodes.jsonl"
DEMO_VAULT  = DEMO_DIR   / "vault"
DEMO_RAW    = DEMO_DIR   / "raw"


# ---------------------------------------------------------------------------
# Fake turn data
# ---------------------------------------------------------------------------
#
# 3 topics: "Python Generators", "PySpark Joins", "Async Python"
# 2 accounts: "acct-a", "acct-b"
# Topics overlap — both accounts have turns for "Python Generators" and
# "PySpark Joins"; only acct-a has "Async Python".
#
# Each node mirrors the schema written by server._process_turns:
#   id, conv_id, account, idx, prev, question, title, topic, bullets,
#   raw_file, ts

_NODES: list[dict] = [
    # ── Python Generators — acct-a ─────────────────────────────────────────
    {
        "id":       "a-gen-1",
        "conv_id":  "acct-a-conv-0001",
        "account":  "acct-a",
        "idx":      0,
        "prev":     None,
        "question": "What is a Python generator and when should I use one?",
        "title":    "What Is a Python Generator",
        "topic":    "Python Generators",
        "bullets": [
            "A generator is a function that uses `yield` to produce values one at a time.",
            "Use generators when iterating over large sequences to avoid loading everything into memory.",
            "Example: `def count_up(n): yield from range(n)` — produces integers lazily.",
        ],
        "raw_file": "demo-a-gen-1.md",
        "ts":       "2025-09-01T09:00:00Z",
    },
    {
        "id":       "a-gen-2",
        "conv_id":  "acct-a-conv-0001",
        "account":  "acct-a",
        "idx":      1,
        "prev":     "a-gen-1",
        "question": "What is the difference between a generator and a list comprehension?",
        "title":    "Generator vs List Comprehension",
        "topic":    "Python Generators",
        "bullets": [
            "A list comprehension evaluates eagerly and stores all results in memory.",
            "A generator expression `(x for x in ...)` evaluates lazily — one value at a time.",
            "Prefer generators for large datasets or infinite sequences.",
        ],
        "raw_file": "demo-a-gen-2.md",
        "ts":       "2025-09-01T09:15:00Z",
    },
    {
        "id":       "a-gen-3",
        "conv_id":  "acct-a-conv-0001",
        "account":  "acct-a",
        "idx":      2,
        "prev":     "a-gen-2",
        "question": "How does send() work with generators?",
        "title":    "Sending Values into a Generator",
        "topic":    "Python Generators",
        "bullets": [
            "`gen.send(value)` resumes the generator and injects `value` at the `yield` expression.",
            "The first call must be `next(gen)` or `gen.send(None)` to advance to the first yield.",
            "Useful for coroutines and two-way communication patterns.",
        ],
        "raw_file": "demo-a-gen-3.md",
        "ts":       "2025-09-01T09:30:00Z",
    },

    # ── Python Generators — acct-b ─────────────────────────────────────────
    {
        "id":       "b-gen-1",
        "conv_id":  "acct-b-conv-0001",
        "account":  "acct-b",
        "idx":      0,
        "prev":     None,
        "question": "Can generators be chained into pipelines?",
        "title":    "Generator Pipelines",
        "topic":    "Python Generators",
        "bullets": [
            "Generators can be chained so the output of one feeds the input of the next.",
            "No intermediate list is created — data flows one item at a time.",
            "Example: `compressed = compress(encrypt(read_file(path)))` — each stage is a generator.",
        ],
        "raw_file": "demo-b-gen-1.md",
        "ts":       "2025-09-05T14:00:00Z",
    },
    {
        "id":       "b-gen-2",
        "conv_id":  "acct-b-conv-0001",
        "account":  "acct-b",
        "idx":      1,
        "prev":     "b-gen-1",
        "question": "What does itertools.islice do with a generator?",
        "title":    "Slicing Generators with islice",
        "topic":    "Python Generators",
        "bullets": [
            "`itertools.islice(gen, n)` takes the first n items from a generator without consuming the rest.",
            "Useful for paginating or sampling infinite generators.",
            "Does not support negative indices unlike regular list slicing.",
        ],
        "raw_file": "demo-b-gen-2.md",
        "ts":       "2025-09-05T14:20:00Z",
    },

    # ── PySpark Joins — acct-a ─────────────────────────────────────────────
    {
        "id":       "a-spark-1",
        "conv_id":  "acct-a-conv-0002",
        "account":  "acct-a",
        "idx":      0,
        "prev":     None,
        "question": "When should I use a broadcast join in PySpark?",
        "title":    "Broadcast Join in PySpark",
        "topic":    "PySpark Joins",
        "bullets": [
            "Use a broadcast join when one side of the join is small enough to fit in executor memory.",
            "Spark sends a copy of the small table to every executor, avoiding a full shuffle.",
            "Triggered automatically when the table is below `spark.sql.autoBroadcastJoinThreshold` (default 10 MB).",
        ],
        "raw_file": "demo-a-spark-1.md",
        "ts":       "2025-09-10T10:00:00Z",
    },
    {
        "id":       "a-spark-2",
        "conv_id":  "acct-a-conv-0002",
        "account":  "acct-a",
        "idx":      1,
        "prev":     "a-spark-1",
        "question": "What is a shuffle hash join and when does Spark choose it?",
        "title":    "Shuffle Hash Join",
        "topic":    "PySpark Joins",
        "bullets": [
            "A shuffle hash join repartitions both tables on the join key then builds a hash table on the smaller partition.",
            "Chosen when the smaller side fits in memory but is too large to broadcast.",
            "More memory-intensive than sort-merge join but avoids a full sort.",
        ],
        "raw_file": "demo-a-spark-2.md",
        "ts":       "2025-09-10T10:20:00Z",
    },

    # ── PySpark Joins — acct-b ─────────────────────────────────────────────
    {
        "id":       "b-spark-1",
        "conv_id":  "acct-b-conv-0002",
        "account":  "acct-b",
        "idx":      0,
        "prev":     None,
        "question": "How do I force a broadcast join in PySpark if autoBroadcastJoinThreshold is too low?",
        "title":    "Forcing a Broadcast Join",
        "topic":    "PySpark Joins",
        "bullets": [
            "Wrap the small DataFrame with `broadcast()`: `df_large.join(broadcast(df_small), 'key')`.",
            "Import: `from pyspark.sql.functions import broadcast`.",
            "Overrides the threshold — use carefully, since the table must fit in executor memory.",
        ],
        "raw_file": "demo-b-spark-1.md",
        "ts":       "2025-09-12T11:00:00Z",
    },
    {
        "id":       "b-spark-2",
        "conv_id":  "acct-b-conv-0002",
        "account":  "acct-b",
        "idx":      1,
        "prev":     "b-spark-1",
        "question": "What is a sort-merge join and when is it the default?",
        "title":    "Sort-Merge Join Is the Default",
        "topic":    "PySpark Joins",
        "bullets": [
            "Sort-merge join sorts both sides by the join key then merges them — works for any size.",
            "It is Spark's default join strategy when neither side can be broadcast.",
            "Produces a full shuffle; optimize by pre-partitioning or bucketing on the join key.",
        ],
        "raw_file": "demo-b-spark-2.md",
        "ts":       "2025-09-12T11:25:00Z",
    },

    # ── Async Python — acct-a only ─────────────────────────────────────────
    {
        "id":       "a-async-1",
        "conv_id":  "acct-a-conv-0003",
        "account":  "acct-a",
        "idx":      0,
        "prev":     None,
        "question": "What is the Python event loop and how does asyncio use it?",
        "title":    "The asyncio Event Loop",
        "topic":    "Async Python",
        "bullets": [
            "The event loop runs coroutines by scheduling them and switching between them at `await` points.",
            "Only one coroutine runs at a time on a single thread — it is concurrent, not parallel.",
            "Start with `asyncio.run(main())` which creates and manages the loop for you.",
        ],
        "raw_file": "demo-a-async-1.md",
        "ts":       "2025-09-15T08:00:00Z",
    },
    {
        "id":       "a-async-2",
        "conv_id":  "acct-a-conv-0003",
        "account":  "acct-a",
        "idx":      1,
        "prev":     "a-async-1",
        "question": "What is the difference between asyncio.gather and asyncio.wait?",
        "title":    "gather vs wait in asyncio",
        "topic":    "Async Python",
        "bullets": [
            "`asyncio.gather(*coros)` runs coroutines concurrently and returns results as a list.",
            "`asyncio.wait(tasks)` gives finer control: FIRST_COMPLETED, FIRST_EXCEPTION, ALL_COMPLETED.",
            "`gather` raises on the first exception by default; `wait` lets you inspect each task.",
        ],
        "raw_file": "demo-a-async-2.md",
        "ts":       "2025-09-15T08:20:00Z",
    },
    {
        "id":       "a-async-3",
        "conv_id":  "acct-a-conv-0003",
        "account":  "acct-a",
        "idx":      2,
        "prev":     "a-async-2",
        "question": "How do I run blocking code from an async function without freezing the event loop?",
        "title":    "Running Blocking Code in asyncio",
        "topic":    "Async Python",
        "bullets": [
            "Use `await loop.run_in_executor(None, blocking_fn, *args)` to offload to a thread pool.",
            "`asyncio.to_thread(fn, *args)` is the cleaner Python 3.9+ shorthand.",
            "Never call `time.sleep()` inside a coroutine — use `await asyncio.sleep()` instead.",
        ],
        "raw_file": "demo-a-async-3.md",
        "ts":       "2025-09-15T08:45:00Z",
    },
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> None:
    from server.render import rebuild  # noqa: PLC0415

    # Create demo directories
    DEMO_DIR.mkdir(exist_ok=True)
    DEMO_VAULT.mkdir(exist_ok=True)
    DEMO_RAW.mkdir(exist_ok=True)

    # Write nodes.jsonl (overwrite each run so demo is always fresh)
    DEMO_NODES.write_text(
        "\n".join(json.dumps(n) for n in _NODES) + "\n",
        encoding="utf-8",
    )
    print(f"[seed_demo] wrote {len(_NODES)} nodes → {DEMO_NODES}")

    # Render vault
    rebuild(nodes_path=DEMO_NODES, vault_path=DEMO_VAULT)
    md_files = list(DEMO_VAULT.glob("**/*.md"))
    print(f"[seed_demo] rendered {len(md_files)} vault files → {DEMO_VAULT}")

    # Summary
    topics  = sorted({n["topic"]  for n in _NODES})
    accounts= sorted({n["account"] for n in _NODES})
    print()
    print(f"  Topics  : {', '.join(topics)}")
    print(f"  Accounts: {', '.join(accounts)}")
    print()
    print("Vault path:")
    print(f"  {DEMO_VAULT}")
    print()
    print("Run the handoff command against demo data:")
    for topic in topics:
        print(f"  uv run python -m server.handoff --topic \"{topic}\" "
              f"--nodes {DEMO_NODES}")
    print()
    print("Filter by account:")
    print(f"  uv run python -m server.handoff --topic \"Python Generators\" "
          f"--account acct-a --nodes {DEMO_NODES}")


if __name__ == "__main__":
    run()
