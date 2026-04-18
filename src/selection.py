"""Selection rules for choosing a final answer from a set of generations.

Majority vote (self-consistency) is the primary selection rule.
"""

from collections import Counter
from typing import Optional

from src.sampling import SampledSet


def majority_vote(
    answers: list[Optional[str]],
) -> Optional[str]:
    """Return the most common non-None answer. Tie-break by first occurrence."""
    valid = [a for a in answers if a is not None and a != ""]
    if not valid:
        return None
    counter = Counter(valid)
    # most_common preserves insertion order for ties in Python 3.7+; for
    # determinism we take the answer that appears first among the tied top count.
    max_count = counter.most_common(1)[0][1]
    for a in valid:
        if counter[a] == max_count:
            return a
    return None  # unreachable


def select_answer(
    sampled_set: SampledSet,
    answers: list[Optional[str]],
    method: str = "majority_vote",
) -> Optional[str]:
    """Apply a selection rule to a SampledSet given pre-extracted answers."""
    if len(answers) != sampled_set.n:
        raise ValueError("answers length must match sampled_set.n")

    if method == "majority_vote":
        return majority_vote(answers)
    elif method == "first":
        return answers[0] if answers else None
    else:
        raise ValueError(f"Unknown selection method: {method}")
