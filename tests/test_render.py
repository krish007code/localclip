"""
tests/test_render.py — unit tests for server/render.py

Required cases (from checkpoint spec):
  - filename sanitization (Linux + Windows forbidden chars)
  - last turn stays pending (not processed)
  - flush:true processes the last turn
  - a new turn later makes the previous node's Next link appear (derived, never stored)
  - two nodes sharing a prev render both Next links (branch)
  - a dangling prev doesn't crash or create a broken link
  - render is idempotent (run twice, identical output)
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from server import render, store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_node(
    id_: str,
    conv_id: str = "abcdef12-0000-0000-0000-000000000000",
    idx: int = 0,
    prev: str | None = None,
    title: str = "A Title",
    topic: str = "Test Topic",
    bullets: list[str] | None = None,
    ts: str = "2025-01-01T00:00:00Z",
    raw_file: str = "20250101-000000-test.md",
) -> dict:
    return {
        "id": id_,
        "conv_id": conv_id,
        "idx": idx,
        "prev": prev,
        "question": f"question for {id_}",
        "title": title,
        "topic": topic,
        "bullets": bullets or ["bullet one", "bullet two"],
        "ts": ts,
        "raw_file": raw_file,
    }


def _write_nodes(path: Path, nodes: list[dict]) -> None:
    lines = [json.dumps(n) for n in nodes]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _rebuild_to(nodes: list[dict], vault: Path, nodes_file: Path) -> None:
    _write_nodes(nodes_file, nodes)
    render.rebuild(nodes_path=nodes_file, vault_path=vault)


# ---------------------------------------------------------------------------
# sanitize_filename
# ---------------------------------------------------------------------------

class TestSanitizeFilename:
    def test_windows_forbidden_chars(self):
        raw = r'My:File*Name?"<Test>|Done\path/sub'
        result = render.sanitize_filename(raw)
        for ch in r'\/:*?"<>|':
            assert ch not in result, f"forbidden char {ch!r} still present"

    def test_valid_name_unchanged(self):
        name = "Python Basics 2025"
        assert render.sanitize_filename(name) == name

    def test_all_forbidden_produces_empty(self):
        assert render.sanitize_filename(r'\/|*?:"<>') == ""

    def test_mixed(self):
        assert render.sanitize_filename("Hello: World") == "Hello World"

    def test_linux_path_separator(self):
        assert "/" not in render.sanitize_filename("path/to/file")

    def test_windows_path_separator(self):
        assert "\\" not in render.sanitize_filename("path\\to\\file")


# ---------------------------------------------------------------------------
# build_next_map
# ---------------------------------------------------------------------------

class TestBuildNextMap:
    def test_linear_chain(self):
        nodes = [
            _make_node("A", prev=None,  idx=0),
            _make_node("B", prev="A",   idx=1),
            _make_node("C", prev="B",   idx=2),
        ]
        nxt = render.build_next_map(nodes)
        assert nxt.get("A") == ["B"]
        assert nxt.get("B") == ["C"]
        assert "C" not in nxt

    def test_root_has_no_entry(self):
        nodes = [_make_node("A", prev=None, idx=0)]
        nxt = render.build_next_map(nodes)
        assert "A" not in nxt

    def test_branch_two_successors(self):
        """Two nodes share the same prev — parent must list both."""
        nodes = [
            _make_node("A", prev=None, idx=0),
            _make_node("B", prev="A",  idx=1, ts="2025-01-01T00:00:01Z"),
            _make_node("C", prev="A",  idx=1, ts="2025-01-01T00:00:02Z"),
        ]
        nxt = render.build_next_map(nodes)
        assert set(nxt["A"]) == {"B", "C"}

    def test_branch_order_is_stable(self):
        """Same input must produce same order across runs."""
        nodes = [
            _make_node("A", prev=None, idx=0),
            _make_node("B", prev="A",  idx=1),
            _make_node("C", prev="A",  idx=2),
        ]
        first  = render.build_next_map(nodes)["A"]
        second = render.build_next_map(nodes)["A"]
        assert first == second


# ---------------------------------------------------------------------------
# render_node_file
# ---------------------------------------------------------------------------

class TestRenderNodeFile:
    def _render(self, nodes: list[dict], target_id: str) -> str:
        nodes_by_id = {n["id"]: n for n in nodes}
        nxt = render.build_next_map(nodes)
        all_fnames = {render.node_filename(n) for n in nodes}
        target = nodes_by_id[target_id]
        return render.render_node_file(target, nodes_by_id, nxt, all_fnames)

    def test_first_node_prev_start(self):
        nodes = [_make_node("A", prev=None, idx=0)]
        content = self._render(nodes, "A")
        assert "Prev: (start)" in content

    def test_first_node_next_waiting(self):
        nodes = [_make_node("A", prev=None, idx=0)]
        content = self._render(nodes, "A")
        assert "Next: (waiting)" in content

    def test_next_link_appears_when_successor_added(self):
        """After adding node B (prev=A), A's note must show Next: [[B_filename]]."""
        nodes = [
            _make_node("A", prev=None, idx=0, title="First Question"),
            _make_node("B", prev="A",  idx=1, title="Second Question"),
        ]
        content = self._render(nodes, "A")
        b_fname = render.node_filename(nodes[1])
        assert f"[[{b_fname}]]" in content
        assert "Next: (waiting)" not in content

    def test_prev_link_on_middle_node(self):
        nodes = [
            _make_node("A", prev=None, idx=0, title="First"),
            _make_node("B", prev="A",  idx=1, title="Second"),
        ]
        content = self._render(nodes, "B")
        a_fname = render.node_filename(nodes[0])
        assert f"[[{a_fname}]]" in content
        assert "Prev: (start)" not in content

    def test_branch_both_next_links(self):
        """Two nodes sharing a prev must both appear in parent's Next line."""
        nodes = [
            _make_node("A", prev=None, idx=0, title="Root"),
            _make_node("B", prev="A",  idx=1, title="Branch One"),
            _make_node("C", prev="A",  idx=1, title="Branch Two"),
        ]
        content = self._render(nodes, "A")
        b_fname = render.node_filename(nodes[1])
        c_fname = render.node_filename(nodes[2])
        assert f"[[{b_fname}]]" in content
        assert f"[[{c_fname}]]" in content

    def test_dangling_prev_does_not_crash(self):
        """A node whose prev id doesn't exist must render without error."""
        node = _make_node("B", prev="GHOST_ID", idx=1, title="Orphan")
        nodes = [node]  # "GHOST_ID" is absent from the list
        # Must not raise
        content = self._render(nodes, "B")
        # The Prev line must say "(unknown)" — not a [[wiki link]] to the ghost
        assert "Prev: (unknown)" in content

    def test_dangling_prev_no_wiki_link(self):
        """Dangling prev must not produce a [[...]] link to a missing file."""
        node = _make_node("B", prev="GHOST_ID", idx=1, title="Orphan")
        content = self._render([node], "B")
        # No wiki link should contain GHOST_ID
        assert "GHOST_ID" not in content

    def test_frontmatter_fields(self):
        nodes = [_make_node("A", idx=0, topic="My Topic")]
        content = self._render(nodes, "A")
        assert "id: A" in content
        assert "topic: My Topic" in content
        assert "idx: 0" in content

    def test_bullets_rendered(self):
        nodes = [_make_node("A", bullets=["fact one", "fact two"])]
        content = self._render(nodes, "A")
        assert "- fact one" in content
        assert "- fact two" in content

    def test_raw_link_present(self):
        nodes = [_make_node("A", raw_file="20250101-000000-test.md")]
        content = self._render(nodes, "A")
        assert "[[raw/20250101-000000-test.md]]" in content

    def test_topic_link_present(self):
        nodes = [_make_node("A", topic="Python Basics")]
        content = self._render(nodes, "A")
        assert "[[Python Basics]]" in content


# ---------------------------------------------------------------------------
# Closing rule — integration with store.close_turns
# ---------------------------------------------------------------------------

class TestClosingRule:
    """Verify the last-turn-pending rule end-to-end through store + render."""

    def _msgs(self, *pairs):
        """Build message list from ('u_id', 'a_id') pairs."""
        msgs = []
        for uid, aid in pairs:
            msgs.append({"id": uid, "role": "user",      "markdown": f"Q {uid}"})
            msgs.append({"id": aid, "role": "assistant", "markdown": f"A {aid}"})
        return msgs

    def test_last_turn_stays_pending(self):
        msgs = self._msgs(("u1","a1"), ("u2","a2"), ("u3","a3"))
        closed = store.close_turns(msgs, flush=False)
        ids = [u["id"] for u, _ in closed]
        assert "u3" not in ids, "u3 is the last turn and must stay pending"
        assert "u1" in ids and "u2" in ids

    def test_flush_closes_last_turn(self):
        msgs = self._msgs(("u1","a1"), ("u2","a2"), ("u3","a3"))
        closed = store.close_turns(msgs, flush=True)
        ids = [u["id"] for u, _ in closed]
        assert "u3" in ids, "flush=True must include the last turn"

    def test_next_link_derived_not_stored(self, tmp_path):
        """
        Nodes must never store `next`.
        When B is added (prev=A), A's Next link is computed at render time.
        """
        vault = tmp_path / "vault"
        nf = tmp_path / "nodes.jsonl"
        vault.mkdir()

        node_a = _make_node("A", prev=None, idx=0, title="First Turn")
        _rebuild_to([node_a], vault, nf)

        # A's file must say "Next: (waiting)"
        a_fname = render.node_filename(node_a) + ".md"
        a_content = (vault / a_fname).read_text()
        assert "Next: (waiting)" in a_content
        assert "next" not in json.loads((tmp_path / "nodes.jsonl").read_text().splitlines()[0])

        # Now add B
        node_b = _make_node("B", prev="A", idx=1, title="Second Turn")
        _rebuild_to([node_a, node_b], vault, nf)

        a_content2 = (vault / a_fname).read_text()
        b_fname = render.node_filename(node_b)
        assert f"[[{b_fname}]]" in a_content2, "Next link must appear after B is added"

    def test_next_key_never_in_jsonl(self, tmp_path):
        """append_node must never write a `next` key."""
        nf = tmp_path / "nodes.jsonl"
        monkeystore = store  # just using directly
        import server.store as st
        import unittest.mock as mock
        with mock.patch.object(st, "NODES_FILE", nf), \
             mock.patch.object(st, "DATA_DIR", tmp_path):
            node = _make_node("A")
            # Ensure `next` is not in the node dict before appending
            assert "next" not in node
            st.append_node(node)
            raw = nf.read_text()
            data = json.loads(raw.strip())
            assert "next" not in data


# ---------------------------------------------------------------------------
# Branch rendering
# ---------------------------------------------------------------------------

class TestBranchRendering:
    def test_two_nodes_same_prev_both_next_links(self, tmp_path):
        vault = tmp_path / "vault"
        nf = tmp_path / "nodes.jsonl"
        vault.mkdir()

        root  = _make_node("ROOT",    prev=None,   idx=0, title="Root Node")
        left  = _make_node("LEFT",    prev="ROOT", idx=1, title="Left Branch",  ts="2025-01-01T00:00:01Z")
        right = _make_node("RIGHT",   prev="ROOT", idx=1, title="Right Branch", ts="2025-01-01T00:00:02Z")

        _rebuild_to([root, left, right], vault, nf)

        root_fname = render.node_filename(root) + ".md"
        content = (vault / root_fname).read_text()

        left_fname  = render.node_filename(left)
        right_fname = render.node_filename(right)

        assert f"[[{left_fname}]]"  in content, "Left branch missing from parent Next"
        assert f"[[{right_fname}]]" in content, "Right branch missing from parent Next"

    def test_each_child_has_correct_prev(self, tmp_path):
        vault = tmp_path / "vault"
        nf = tmp_path / "nodes.jsonl"
        vault.mkdir()

        root  = _make_node("ROOT",  prev=None,   idx=0, title="Root")
        left  = _make_node("LEFT",  prev="ROOT", idx=1, title="Left")
        right = _make_node("RIGHT", prev="ROOT", idx=1, title="Right")

        _rebuild_to([root, left, right], vault, nf)

        root_fname = render.node_filename(root)

        left_fname = render.node_filename(left) + ".md"
        left_content = (vault / left_fname).read_text()
        assert f"[[{root_fname}]]" in left_content

        right_fname = render.node_filename(right) + ".md"
        right_content = (vault / right_fname).read_text()
        assert f"[[{root_fname}]]" in right_content


# ---------------------------------------------------------------------------
# Dangling prev
# ---------------------------------------------------------------------------

class TestDanglingPrev:
    def test_no_crash_on_dangling_prev(self, tmp_path):
        vault = tmp_path / "vault"
        nf = tmp_path / "nodes.jsonl"
        vault.mkdir()

        orphan = _make_node("ORPHAN", prev="DOES_NOT_EXIST", idx=0, title="Orphan Node")
        # Must not raise
        _rebuild_to([orphan], vault, nf)

    def test_dangling_prev_no_broken_link(self, tmp_path):
        vault = tmp_path / "vault"
        nf = tmp_path / "nodes.jsonl"
        vault.mkdir()

        orphan = _make_node("ORPHAN", prev="DOES_NOT_EXIST", idx=0, title="Orphan Node")
        _rebuild_to([orphan], vault, nf)

        fname = render.node_filename(orphan) + ".md"
        content = (vault / fname).read_text()
        # Should not contain a link to the ghost ID
        assert "DOES_NOT_EXIST" not in content
        # Should not contain a valid wiki-link for the missing prev
        assert "Prev: [[" not in content


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def _collect_files(self, vault: Path) -> dict[str, str]:
        return {
            f.name: f.read_text(encoding="utf-8")
            for f in sorted(vault.glob("*.md"))
        }

    def test_rebuild_twice_identical(self, tmp_path):
        vault = tmp_path / "vault"
        nf = tmp_path / "nodes.jsonl"
        vault.mkdir()

        nodes = [
            _make_node("A", prev=None, idx=0, title="First",  topic="Alpha", ts="2025-01-01T00:00:00Z"),
            _make_node("B", prev="A",  idx=1, title="Second", topic="Beta",  ts="2025-01-01T00:01:00Z"),
            _make_node("C", prev="B",  idx=2, title="Third",  topic="Alpha", ts="2025-01-01T00:02:00Z"),
        ]

        _rebuild_to(nodes, vault, nf)
        first_run = self._collect_files(vault)

        # Second rebuild on same data
        render.rebuild(nodes_path=nf, vault_path=vault)
        second_run = self._collect_files(vault)

        assert first_run.keys() == second_run.keys(), "File set changed between runs"
        for fname in first_run:
            assert first_run[fname] == second_run[fname], \
                f"Content differs for {fname}"

    def test_rebuild_twice_with_branch_identical(self, tmp_path):
        vault = tmp_path / "vault"
        nf = tmp_path / "nodes.jsonl"
        vault.mkdir()

        nodes = [
            _make_node("ROOT",  prev=None,   idx=0, title="Root",  topic="T", ts="2025-01-01T00:00:00Z"),
            _make_node("LEFT",  prev="ROOT", idx=1, title="Left",  topic="T", ts="2025-01-01T00:00:01Z"),
            _make_node("RIGHT", prev="ROOT", idx=1, title="Right", topic="T", ts="2025-01-01T00:00:02Z"),
        ]

        _rebuild_to(nodes, vault, nf)
        first_run = self._collect_files(vault)

        render.rebuild(nodes_path=nf, vault_path=vault)
        second_run = self._collect_files(vault)

        assert first_run == second_run


# ---------------------------------------------------------------------------
# Filename sanitization — cross-platform coverage
# ---------------------------------------------------------------------------

class TestFilenameOnDisk:
    def test_colon_in_title_sanitized(self, tmp_path):
        vault = tmp_path / "vault"
        nf = tmp_path / "nodes.jsonl"
        vault.mkdir()

        node = _make_node("A", title="Python: The Language", topic="Python")
        _rebuild_to([node], vault, nf)

        files = list(vault.glob("*.md"))
        for f in files:
            assert ":" not in f.name, f"Colon found in filename: {f.name}"

    def test_question_mark_in_title_sanitized(self, tmp_path):
        vault = tmp_path / "vault"
        nf = tmp_path / "nodes.jsonl"
        vault.mkdir()

        node = _make_node("A", title="What is AI?", topic="AI")
        _rebuild_to([node], vault, nf)

        files = list(vault.glob("*.md"))
        for f in files:
            assert "?" not in f.name

    def test_pipe_in_topic_sanitized(self, tmp_path):
        vault = tmp_path / "vault"
        nf = tmp_path / "nodes.jsonl"
        vault.mkdir()

        node = _make_node("A", title="Test", topic="Left|Right")
        _rebuild_to([node], vault, nf)

        files = list(vault.glob("*.md"))
        for f in files:
            assert "|" not in f.name
