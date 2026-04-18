"""Tests for selection rules."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.selection import majority_vote


def test_majority_vote_clear_winner():
    assert majority_vote(["42", "42", "7", "42"]) == "42"


def test_majority_vote_tie_first_occurrence():
    # "7" appears first in the list, ties with "42" at count 2
    assert majority_vote(["7", "42", "7", "42"]) == "7"


def test_majority_vote_all_none():
    assert majority_vote([None, None]) is None


def test_majority_vote_single():
    assert majority_vote(["99"]) == "99"


def test_majority_vote_filters_none():
    assert majority_vote([None, "5", "5"]) == "5"
