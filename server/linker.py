"""
linker.py — topic normalization and fuzzy-matching for LocalClip.

In v1 the LLM does not return `links`; cross-node edges are structural
(prev/next) and categorical (topic).  This module only normalises topic
names and fuzzy-matches them against the existing topic namespace so the
vault stays consistent.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any


# ---------------------------------------------------------------------------
# Topic normalization
# ---------------------------------------------------------------------------

def _singularize(word: str) -> str:
    """Naive English singularization for the *last* word of a topic name.

    Rules (in order):
    1. 'ies' suffix (len > 4) → replace with 'y'   e.g. Libraries → Library
    2. trailing 's' (len > 4, not 'ss')              e.g. Models → Model
    3. Otherwise leave unchanged.
    """
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def normalize_topic(name: str) -> str:
    """Return a canonical topic name.

    Steps:
    1. Strip leading/trailing whitespace.
    2. Collapse internal whitespace to single spaces.
    3. Title-case every word.
    4. Singularize the last word only.
    """
    name = re.sub(r"\s+", " ", name.strip())
    if not name:
        return "Uncategorized"
    words = name.title().split()
    words[-1] = _singularize(words[-1])
    return " ".join(words)


# ---------------------------------------------------------------------------
# Fuzzy matching
# ---------------------------------------------------------------------------

def fuzzy_match(name: str, existing: list[str]) -> str | None:
    """Return the best-matching name from *existing* if ratio ≥ 0.85.

    Comparison is case-insensitive.  Returns None if no match is good enough.
    """
    best: str | None = None
    best_ratio = 0.0
    name_lower = name.lower()
    for candidate in existing:
        ratio = SequenceMatcher(None, name_lower, candidate.lower()).ratio()
        if ratio >= 0.85 and ratio > best_ratio:
            best = candidate
            best_ratio = ratio
    return best


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

def process(
    llm_result: dict[str, Any],
    existing_topics: list[str],
) -> dict[str, Any]:
    """Post-process a raw LLM result dict.

    Mutates and returns a copy with the `topic` field normalised and
    fuzzy-matched against *existing_topics*.  All other fields are passed
    through unchanged.
    """
    result = dict(llm_result)
    raw_topic = result.get("topic", "")
    normalized = normalize_topic(raw_topic)
    matched = fuzzy_match(normalized, existing_topics)
    result["topic"] = matched if matched is not None else normalized
    return result
