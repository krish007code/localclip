"""
tests/test_linker.py — unit tests for server/linker.py
"""

from __future__ import annotations

import pytest

from server import linker


# ---------------------------------------------------------------------------
# normalize_topic
# ---------------------------------------------------------------------------

class TestNormalizeTopic:
    def test_basic_title_case(self):
        assert linker.normalize_topic("python basics") == "Python Basic"

    def test_collapse_whitespace(self):
        assert linker.normalize_topic("  machine   learning  ") == "Machine Learning"

    def test_ies_to_y(self):
        assert linker.normalize_topic("libraries") == "Library"

    def test_plain_s_stripped(self):
        assert linker.normalize_topic("models") == "Model"

    def test_ss_not_stripped(self):
        # "stress" ends in ss — the trailing s is part of ss, not a plural
        assert linker.normalize_topic("stress") == "Stress"

    def test_short_word_not_stripped(self):
        # "cats" is 4 chars — rule requires len > 4 to strip
        assert linker.normalize_topic("cats") == "Cats"

    def test_only_last_word_singularized(self):
        # "Data Models" → last word "Models" → "Model"; "Data" stays
        assert linker.normalize_topic("Data Models") == "Data Model"

    def test_multi_word_ies(self):
        assert linker.normalize_topic("neural network libraries") == "Neural Network Library"

    def test_empty_string(self):
        assert linker.normalize_topic("") == "Uncategorized"

    def test_whitespace_only(self):
        assert linker.normalize_topic("   ") == "Uncategorized"

    def test_already_normalized(self):
        assert linker.normalize_topic("Python Basics") == "Python Basic"

    def test_single_word_no_plural(self):
        assert linker.normalize_topic("Python") == "Python"


# ---------------------------------------------------------------------------
# fuzzy_match
# ---------------------------------------------------------------------------

class TestFuzzyMatch:
    def test_exact_match(self):
        existing = ["Python Basics", "Machine Learning"]
        assert linker.fuzzy_match("Python Basics", existing) == "Python Basics"

    def test_close_match_reuses_existing(self):
        # "Python Basic" vs "Python Basics" — should exceed 0.85
        existing = ["Python Basics", "Machine Learning"]
        result = linker.fuzzy_match("Python Basic", existing)
        assert result == "Python Basics"

    def test_case_insensitive(self):
        existing = ["Machine Learning"]
        assert linker.fuzzy_match("machine learning", existing) == "Machine Learning"

    def test_no_match_returns_none(self):
        existing = ["Python Basics", "Machine Learning"]
        result = linker.fuzzy_match("Quantum Computing", existing)
        assert result is None

    def test_empty_existing(self):
        assert linker.fuzzy_match("anything", []) is None

    def test_best_match_wins(self):
        # "Data Model" is closer to "Data Models" than "Data Modelling"
        existing = ["Data Models", "Data Modelling"]
        result = linker.fuzzy_match("Data Model", existing)
        # Both should be close; result must be one of them
        assert result in existing

    def test_below_threshold_returns_none(self):
        existing = ["Quantum Physics"]
        result = linker.fuzzy_match("Async Programming", existing)
        assert result is None


# ---------------------------------------------------------------------------
# process (full pipeline)
# ---------------------------------------------------------------------------

class TestProcess:
    def test_normalizes_topic(self):
        llm_result = {
            "skip": False,
            "title": "Test",
            "topic": "data models",
            "bullets": ["fact"],
        }
        result = linker.process(llm_result, [])
        assert result["topic"] == "Data Model"

    def test_fuzzy_matches_existing(self):
        llm_result = {
            "skip": False,
            "title": "Test",
            "topic": "Python Basic",   # close to "Python Basics"
            "bullets": ["fact"],
        }
        result = linker.process(llm_result, ["Python Basics", "Machine Learning"])
        assert result["topic"] == "Python Basics"

    def test_no_match_keeps_normalized(self):
        llm_result = {
            "skip": False,
            "title": "Test",
            "topic": "Quantum Computing",
            "bullets": ["fact"],
        }
        result = linker.process(llm_result, ["Python Basics"])
        assert result["topic"] == "Quantum Computing"

    def test_other_fields_passed_through(self):
        llm_result = {
            "skip": False,
            "title": "My Title",
            "topic": "Testing",
            "bullets": ["b1", "b2"],
        }
        result = linker.process(llm_result, [])
        assert result["title"] == "My Title"
        assert result["bullets"] == ["b1", "b2"]
        assert result["skip"] is False

    def test_empty_topic_becomes_uncategorized(self):
        llm_result = {"skip": False, "title": "T", "topic": "", "bullets": []}
        result = linker.process(llm_result, [])
        assert result["topic"] == "Uncategorized"
