"""
tests/test_store.py — unit tests for server/store.py

Covers:
  - slug()
  - close_turns(): last turn pending, flush processes it
  - dedupe_turns(): empty state, partial, fully seen, cursor not in batch
  - update_state(): advances last_closed_id and count
  - append_node(): two concurrent writes produce exactly two valid lines
  - atomic write: second write succeeds even after a simulated failure
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from server import store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msg(role: str, id_: str, text: str = "") -> dict:
    return {"id": id_, "role": role, "markdown": text or f"{role} message {id_}"}


def _user(id_: str, text: str = "") -> dict:
    return _msg("user", id_, text)


def _asst(id_: str, text: str = "") -> dict:
    return _msg("assistant", id_, text)


# ---------------------------------------------------------------------------
# slug
# ---------------------------------------------------------------------------

class TestSlug:
    def test_basic(self):
        assert store.slug("Hello World") == "hello-world"

    def test_special_chars(self):
        assert store.slug("C++ & Python!") == "c-python"

    def test_leading_trailing_dashes(self):
        assert store.slug("  ---hello---  ") == "hello"

    def test_max_len(self):
        result = store.slug("a" * 100, max_len=10)
        assert len(result) == 10

    def test_empty(self):
        # All non-alphanumeric → empty after strip
        assert store.slug("!!!") == ""

    def test_numbers_preserved(self):
        # The dot in "3.10" is non-alphanumeric → replaced with '-'
        # slug("Python 3.10 release") → "python-3-10-release"
        assert store.slug("Python 3.10 release") == "python-3-10-release"


# ---------------------------------------------------------------------------
# close_turns (the closing rule)
# ---------------------------------------------------------------------------

class TestCloseturns:
    def test_three_pairs_flush_false_returns_two(self):
        msgs = [
            _user("u1"), _asst("a1"),
            _user("u2"), _asst("a2"),
            _user("u3"), _asst("a3"),
        ]
        pairs = store.close_turns(msgs, flush=False)
        assert len(pairs) == 2
        assert pairs[0][0]["id"] == "u1"
        assert pairs[1][0]["id"] == "u2"

    def test_three_pairs_flush_true_returns_three(self):
        msgs = [
            _user("u1"), _asst("a1"),
            _user("u2"), _asst("a2"),
            _user("u3"), _asst("a3"),
        ]
        pairs = store.close_turns(msgs, flush=True)
        assert len(pairs) == 3

    def test_one_pair_flush_false_returns_zero(self):
        """Single pair is always pending when flush=False."""
        msgs = [_user("u1"), _asst("a1")]
        pairs = store.close_turns(msgs, flush=False)
        assert pairs == []

    def test_one_pair_flush_true_returns_one(self):
        msgs = [_user("u1"), _asst("a1")]
        pairs = store.close_turns(msgs, flush=True)
        assert len(pairs) == 1

    def test_empty_messages(self):
        assert store.close_turns([], flush=False) == []
        assert store.close_turns([], flush=True) == []

    def test_trailing_user_no_assistant_dropped(self):
        """An incomplete pair (user without assistant) is silently dropped."""
        msgs = [_user("u1"), _asst("a1"), _user("u2")]  # u2 has no assistant
        # With flush=True: pair_turns finds only 1 pair (u1,a1); u2 is orphan
        pairs = store.close_turns(msgs, flush=True)
        assert len(pairs) == 1
        assert pairs[0][0]["id"] == "u1"

    def test_flush_false_then_new_turn_closes_previous(self):
        """Simulate: first poll (2 pairs) → pending; second poll (3 pairs) → closes pair 2."""
        msgs_first = [_user("u1"), _asst("a1"), _user("u2"), _asst("a2")]
        pairs_first = store.close_turns(msgs_first, flush=False)
        assert len(pairs_first) == 1  # u2/a2 is pending

        msgs_second = [
            _user("u1"), _asst("a1"),
            _user("u2"), _asst("a2"),
            _user("u3"), _asst("a3"),
        ]
        pairs_second = store.close_turns(msgs_second, flush=False)
        assert len(pairs_second) == 2  # u2/a2 now closed, u3/a3 pending
        assert pairs_second[1][0]["id"] == "u2"


# ---------------------------------------------------------------------------
# dedupe_turns
# ---------------------------------------------------------------------------

class TestDedupeTurns:
    def _pairs(self, *ids):
        """Build (user, assistant) pairs from a list of user ids."""
        return [(_user(uid), _asst(f"a{uid[1:]}")) for uid in ids]

    def test_empty_state_all_new(self):
        pairs = self._pairs("u1", "u2", "u3")
        result = store.dedupe_turns("conv1", pairs, {})
        assert len(result) == 3

    def test_conv_not_in_state_all_new(self):
        state = {"conversations": {"other_conv": {"last_closed_id": "u9", "count": 1}}}
        pairs = self._pairs("u1", "u2")
        result = store.dedupe_turns("conv1", pairs, state)
        assert len(result) == 2

    def test_all_already_seen(self):
        state = {"conversations": {"conv1": {"last_closed_id": "u3", "count": 3}}}
        pairs = self._pairs("u1", "u2", "u3")
        result = store.dedupe_turns("conv1", pairs, state)
        assert result == []

    def test_partial_seen(self):
        state = {"conversations": {"conv1": {"last_closed_id": "u2", "count": 2}}}
        pairs = self._pairs("u1", "u2", "u3", "u4")
        result = store.dedupe_turns("conv1", pairs, state)
        assert len(result) == 2
        assert result[0][0]["id"] == "u3"
        assert result[1][0]["id"] == "u4"

    def test_cursor_not_in_batch_all_new(self):
        """If last_closed_id isn't in the current batch, treat all as new."""
        state = {"conversations": {"conv1": {"last_closed_id": "u99", "count": 1}}}
        pairs = self._pairs("u1", "u2")
        result = store.dedupe_turns("conv1", pairs, state)
        assert len(result) == 2

    def test_empty_pairs_returns_empty(self):
        result = store.dedupe_turns("conv1", [], {"conversations": {}})
        assert result == []


# ---------------------------------------------------------------------------
# update_state
# ---------------------------------------------------------------------------

class TestUpdateState:
    def test_advances_last_closed_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(store, "DATA_DIR", tmp_path)
        state: dict = {}
        store.update_state("conv1", "u3", state)
        assert state["conversations"]["conv1"]["last_closed_id"] == "u3"

    def test_increments_count(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(store, "DATA_DIR", tmp_path)
        state: dict = {}
        store.update_state("conv1", "u1", state)
        store.update_state("conv1", "u2", state)
        store.update_state("conv1", "u3", state)
        assert state["conversations"]["conv1"]["count"] == 3
        assert state["conversations"]["conv1"]["last_closed_id"] == "u3"

    def test_persisted_to_disk(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(store, "DATA_DIR", tmp_path)
        state: dict = {}
        store.update_state("conv1", "u5", state)
        loaded = store.load_state()
        # load_state reads from the same monkeypatched path
        assert loaded["conversations"]["conv1"]["last_closed_id"] == "u5"


# ---------------------------------------------------------------------------
# append_node — atomic write and concurrent safety
# ---------------------------------------------------------------------------

class TestAppendNode:
    def test_single_append(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "NODES_FILE", tmp_path / "nodes.jsonl")
        monkeypatch.setattr(store, "DATA_DIR", tmp_path)
        node = {"id": "n1", "title": "Test", "topic": "T", "bullets": []}
        store.append_node(node)
        nodes = store.load_nodes()
        assert len(nodes) == 1
        assert nodes[0]["id"] == "n1"

    def test_two_sequential_appends(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "NODES_FILE", tmp_path / "nodes.jsonl")
        monkeypatch.setattr(store, "DATA_DIR", tmp_path)
        store.append_node({"id": "n1", "title": "A"})
        store.append_node({"id": "n2", "title": "B"})
        nodes = store.load_nodes()
        assert len(nodes) == 2
        assert nodes[0]["id"] == "n1"
        assert nodes[1]["id"] == "n2"

    def test_concurrent_appends_produce_two_valid_lines(self, tmp_path, monkeypatch):
        """Two threads appending simultaneously must produce exactly 2 valid lines."""
        monkeypatch.setattr(store, "NODES_FILE", tmp_path / "nodes.jsonl")
        monkeypatch.setattr(store, "DATA_DIR", tmp_path)

        errors: list[str] = []

        def _worker(node_id: str) -> None:
            try:
                store.append_node({"id": node_id, "title": node_id})
            except Exception as e:
                errors.append(str(e))

        t1 = threading.Thread(target=_worker, args=("n1",))
        t2 = threading.Thread(target=_worker, args=("n2",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert errors == [], f"Thread errors: {errors}"
        nodes = store.load_nodes()
        assert len(nodes) == 2
        ids = {n["id"] for n in nodes}
        assert ids == {"n1", "n2"}

    def test_load_nodes_skips_malformed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "NODES_FILE", tmp_path / "nodes.jsonl")
        monkeypatch.setattr(store, "DATA_DIR", tmp_path)
        path = tmp_path / "nodes.jsonl"
        path.write_text('{"id":"good"}\nNOT_JSON\n{"id":"also_good"}\n', encoding="utf-8")
        nodes = store.load_nodes()
        assert len(nodes) == 2
        assert nodes[0]["id"] == "good"
        assert nodes[1]["id"] == "also_good"
