"""
tests/test_account.py — tests for the account label feature.

Covers:
  A. account stored on node (server pipeline)
  B. account:turn_id dedupe key — same turn_id different account → two nodes
  C. account in frontmatter and tag; topic hub shows account
  D. handoff.py: build_brief (single account, all accounts, unknown topic,
     list_topics, deterministic order, paste-ready block)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import server.server  as srv
import server.store   as store_mod
import server.llm     as llm_mod
import server.render  as render_mod
from server.handoff import build_brief, list_topics, save_brief


# ---------------------------------------------------------------------------
# Helpers shared with test_idempotency
# ---------------------------------------------------------------------------

def _reset(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(store_mod, "DATA_DIR",   tmp_path)
    monkeypatch.setattr(store_mod, "RAW_DIR",    tmp_path / "raw")
    monkeypatch.setattr(store_mod, "NODES_FILE", tmp_path / "nodes.jsonl")
    monkeypatch.setattr(store_mod, "STATE_FILE", tmp_path / "state.json")
    (tmp_path / "raw").mkdir(parents=True, exist_ok=True)
    srv._processed_ids = set()
    srv._inflight      = set()
    srv._config_cache  = {
        "token": "t", "model": "stub", "num_ctx": 8192,
        "max_chars": 6000, "blocklist": [], "idle_ms": 5000,
    }


def _good_llm(*_a, **_kw):
    return {"skip": False, "title": "A Title", "topic": "PySpark", "bullets": ["fact"]}


def _payload(account: str = "default", turn_id: str = "u1",
             conv_id: str = "conv-1") -> dict:
    return {
        "url": "https://chatgpt.com/c/test", "title": "T",
        "source": "chat", "conversation_id": conv_id,
        "flush": True, "account": account,
        "captured_at": "2025-10-05T10:00:00Z",
        "messages": [
            {"id": turn_id,  "role": "user",      "markdown": "How does broadcast join work?"},
            {"id": "a1",     "role": "assistant",  "markdown": "It sends the smaller table."},
        ],
    }


def _run(payload: dict, cfg: dict) -> dict:
    """Call _process_turns synchronously (bypasses queue)."""
    msgs  = payload["messages"]
    pairs = store_mod.close_turns(msgs, True)
    state = store_mod.load_state()
    new   = store_mod.dedupe_turns(payload["conversation_id"], pairs, state)
    if not new:
        return {"status": "duplicate"}
    meta = {
        "url": payload.get("url", ""), "title": payload.get("title", ""),
        "conversation_id": payload["conversation_id"],
        "account": payload.get("account", "default"),
        "captured_at": payload.get("captured_at", ""),
    }
    return srv._process_turns(new, meta, cfg)


def _node(id_: str, topic: str = "PySpark", account: str = "default",
          ts: str = "2025-01-01T00:00:00Z", bullets=None, title: str = "T") -> dict:
    return {
        "id": id_, "conv_id": "conv-abc12345", "account": account,
        "idx": 0, "prev": None, "question": "Q", "title": title,
        "topic": topic, "bullets": bullets or ["b"], "raw_file": "r.md", "ts": ts,
    }


# ---------------------------------------------------------------------------
# A — account stored on node
# ---------------------------------------------------------------------------

class TestAccountStoredOnNode:
    def test_account_written_to_jsonl(self, tmp_path, monkeypatch):
        _reset(tmp_path, monkeypatch)
        cfg = srv._config_cache
        with patch.object(llm_mod, "call_ollama", side_effect=_good_llm):
            with patch.object(render_mod, "rebuild"):
                _run(_payload(account="work"), cfg)
        line = (tmp_path / "nodes.jsonl").read_text().strip()
        node = json.loads(line)
        assert node["account"] == "work"

    def test_default_account_when_missing(self, tmp_path, monkeypatch):
        """Payload without 'account' field → node gets 'default'."""
        _reset(tmp_path, monkeypatch)
        cfg = srv._config_cache
        p = _payload()
        del p["account"]
        with patch.object(llm_mod, "call_ollama", side_effect=_good_llm):
            with patch.object(render_mod, "rebuild"):
                _run(p, cfg)
        node = json.loads((tmp_path / "nodes.jsonl").read_text().strip())
        assert node["account"] == "default"


# ---------------------------------------------------------------------------
# B — dedupe key includes account
# ---------------------------------------------------------------------------

class TestAccountDedupeKey:
    def test_same_turn_id_different_account_produces_two_nodes(
        self, tmp_path, monkeypatch
    ):
        """turn_id 'u1' from 'work' and 'u1' from 'personal' are distinct."""
        _reset(tmp_path, monkeypatch)
        cfg = srv._config_cache
        with patch.object(llm_mod, "call_ollama", side_effect=_good_llm):
            with patch.object(render_mod, "rebuild"):
                _run(_payload(account="work",     conv_id="c1"), cfg)
                _run(_payload(account="personal", conv_id="c2"), cfg)
        lines = [l for l in (tmp_path / "nodes.jsonl").read_text().splitlines() if l.strip()]
        assert len(lines) == 2

    def test_same_turn_id_same_account_produces_one_node(self, tmp_path, monkeypatch):
        """Idempotency: same account + same turn_id → only one node."""
        _reset(tmp_path, monkeypatch)
        cfg = srv._config_cache
        with patch.object(llm_mod, "call_ollama", side_effect=_good_llm):
            with patch.object(render_mod, "rebuild"):
                _run(_payload(account="work", conv_id="c1"), cfg)
                _run(_payload(account="work", conv_id="c1"), cfg)
        lines = [l for l in (tmp_path / "nodes.jsonl").read_text().splitlines() if l.strip()]
        assert len(lines) == 1

    def test_processed_ids_uses_account_key(self, tmp_path, monkeypatch):
        _reset(tmp_path, monkeypatch)
        cfg = srv._config_cache
        with patch.object(llm_mod, "call_ollama", side_effect=_good_llm):
            with patch.object(render_mod, "rebuild"):
                _run(_payload(account="research", turn_id="u1"), cfg)
        with srv._ID_LOCK:
            assert "research:u1" in srv._processed_ids
            assert "u1"          not in srv._processed_ids


# ---------------------------------------------------------------------------
# C — renderer: account in frontmatter and topic hub
# ---------------------------------------------------------------------------

class TestRendererAccount:
    def _rebuild(self, nodes, tmp_path):
        vault = tmp_path / "vault"
        nf    = tmp_path / "nodes.jsonl"
        vault.mkdir(exist_ok=True)
        nf.write_text("\n".join(json.dumps(n) for n in nodes) + "\n", encoding="utf-8")
        render_mod.rebuild(nodes_path=nf, vault_path=vault)
        return vault

    def test_account_in_frontmatter(self, tmp_path):
        vault = self._rebuild([_node("n1", account="work")], tmp_path)
        files = [f for f in vault.glob("*.md") if f.name.startswith("conv-ab")]
        assert len(files) == 1
        content = files[0].read_text()
        assert "account: work" in content

    def test_tag_in_frontmatter(self, tmp_path):
        vault = self._rebuild([_node("n1", account="research")], tmp_path)
        files = [f for f in vault.glob("*.md") if f.name.startswith("conv-ab")]
        content = files[0].read_text()
        assert "acct/research" in content

    def test_default_account_for_old_nodes(self, tmp_path):
        """Nodes without 'account' key render as 'default'."""
        node = _node("n1")
        del node["account"]
        vault = self._rebuild([node], tmp_path)
        files = [f for f in vault.glob("*.md") if f.name.startswith("conv-ab")]
        content = files[0].read_text()
        assert "account: default" in content

    def test_topic_hub_shows_account(self, tmp_path):
        vault = self._rebuild([_node("n1", account="personal")], tmp_path)
        hub = vault / "PySpark.md"
        assert hub.exists()
        content = hub.read_text()
        assert "`personal`" in content

    def test_topic_hub_merges_multiple_accounts(self, tmp_path):
        nodes = [
            _node("n1", account="work",     ts="2025-01-01T00:00:00Z"),
            _node("n2", account="personal", ts="2025-01-02T00:00:00Z"),
        ]
        vault = self._rebuild(nodes, tmp_path)
        content = (vault / "PySpark.md").read_text()
        assert "`work`"     in content
        assert "`personal`" in content


# ---------------------------------------------------------------------------
# D — handoff.py
# ---------------------------------------------------------------------------

def _sample_nodes():
    return [
        _node("n1", topic="PySpark", account="work",
              ts="2025-01-01T10:00:00Z", title="Broadcast Join",
              bullets=["Sends smaller table to executors."]),
        _node("n2", topic="PySpark", account="personal",
              ts="2025-01-02T09:00:00Z", title="Shuffle vs Broadcast",
              bullets=["Shuffle moves all data; broadcast moves only the small side."]),
        _node("n3", topic="Python Basics", account="work",
              ts="2025-01-03T08:00:00Z", title="List Comprehensions",
              bullets=["Compact syntax for building lists."]),
    ]


class TestHandoffBuildBrief:
    def test_returns_empty_for_unknown_topic(self):
        nodes = _sample_nodes()
        assert build_brief("Nonexistent", nodes=nodes) == ""

    def test_returns_empty_for_unknown_account_filter(self):
        nodes = _sample_nodes()
        assert build_brief("PySpark", account="ghost", nodes=nodes) == ""

    def test_header_contains_topic(self):
        content = build_brief("PySpark", nodes=_sample_nodes())
        assert "# Handoff: PySpark" in content

    def test_header_contains_both_accounts(self):
        content = build_brief("PySpark", nodes=_sample_nodes())
        assert "personal" in content
        assert "work"     in content

    def test_header_contains_date_range(self):
        content = build_brief("PySpark", nodes=_sample_nodes())
        assert "2025-01-01" in content
        assert "2025-01-02" in content

    def test_sorted_by_timestamp(self):
        content = build_brief("PySpark", nodes=_sample_nodes())
        pos_first  = content.index("Broadcast Join")
        pos_second = content.index("Shuffle vs Broadcast")
        assert pos_first < pos_second

    def test_account_filter_restricts_to_one_account(self):
        content = build_brief("PySpark", account="work", nodes=_sample_nodes())
        assert "Broadcast Join"       in content
        assert "Shuffle vs Broadcast" not in content

    def test_paste_ready_block_present(self):
        content = build_brief("PySpark", nodes=_sample_nodes())
        assert "Here is context from my earlier conversations on" in content
        assert "**PySpark**" in content

    def test_bullets_appear_in_output(self):
        content = build_brief("PySpark", nodes=_sample_nodes())
        assert "Sends smaller table to executors." in content

    def test_deterministic_output(self):
        nodes = _sample_nodes()
        assert build_brief("PySpark", nodes=nodes) == build_brief("PySpark", nodes=nodes)

    def test_single_date_not_a_range(self):
        nodes = [_node("n1", topic="T", ts="2025-06-01T00:00:00Z")]
        content = build_brief("T", nodes=nodes)
        assert "2025-06-01 →" not in content
        assert "2025-06-01" in content


class TestHandoffListTopics:
    def test_returns_sorted_unique_topics(self):
        topics = list_topics(nodes=_sample_nodes())
        assert topics == sorted({"PySpark", "Python Basics"})

    def test_empty_nodes(self):
        assert list_topics(nodes=[]) == []


class TestHandoffSaveBrief:
    def test_saves_to_handoffs_subfolder(self, tmp_path):
        content = "# Test"
        path = save_brief("My Topic", content, vault_path=tmp_path)
        assert path.parent.name == "Handoffs"
        assert path.read_text() == content

    def test_filename_sanitized(self, tmp_path):
        path = save_brief("Topic: With?Chars", "x", vault_path=tmp_path)
        assert ":" not in path.name
        assert "?" not in path.name
