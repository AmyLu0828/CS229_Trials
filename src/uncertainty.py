"""Uncertainty signal computations from a probe sample set.

Three signals:
  1. answer_entropy  — normalized entropy over extracted final answers (primary)
  2. token_entropy   — mean per-token entropy from log-probs of one generation
  3. self_confidence — parsed "confidence: X/10" from model output (optional)

All signals are in [0, 1] where 0 = certain, 1 = maximally uncertain.
"""

import math
from collections import Counter
from typing import Optional

import numpy as np

from src.sampling import SampledSet


def answer_entropy(
    answers: list[Optional[str]],
) -> float:
    """Normalized entropy over the extracted answer distribution.

    Returns 0 if all answers agree, 1 if all answers differ (max entropy).
    answers: list of extracted answer strings (None counts as a distinct token).
    """
    n = len(answers)
    if n == 0:
        return 1.0

    canonical = [a if (a is not None and a != "") else "__NONE__" for a in answers]
    counts = Counter(canonical)
    probs = [c / n for c in counts.values()]

    raw_entropy = -sum(p * math.log(p) for p in probs if p > 0)
    max_entropy = math.log(n) if n > 1 else 1.0
    return raw_entropy / max_entropy if max_entropy > 0 else 0.0


def token_entropy(
    sampled: SampledSet,
    sample_idx: int = 0,
) -> float:
    """Mean per-token entropy from log-probs of a single generation.

    Requires the generation to have been produced with return_logprobs=True.
    Returns 1.0 (max uncertainty) if log-probs are unavailable.
    """
    gen = sampled.generations[sample_idx]
    if gen.token_log_probs is None or len(gen.token_log_probs) == 0:
        return 1.0

    # token_log_probs[i] is log P(chosen token | context).
    # We don't have the full distribution so we use -log_prob as a proxy for
    # per-token surprise (higher = more uncertain).
    surprisals = [-lp for lp in gen.token_log_probs]
    mean_surprisal = float(np.mean(surprisals))

    # Normalize heuristically: cap at ln(vocab_size) ≈ ln(152000) ≈ 11.9 for Qwen
    MAX_SURPRISAL = math.log(152_000)
    return min(mean_surprisal / MAX_SURPRISAL, 1.0)


def parse_self_confidence(text: str) -> Optional[float]:
    """Extract a self-reported confidence score from generation text.

    Looks for patterns like "confidence: 7/10" or "Confidence: 8/10".
    Returns a value in [0, 1] where 1 = fully confident, or None if not found.
    """
    import re
    match = re.search(r"confidence[:\s]+(\d+)\s*/\s*10", text, re.IGNORECASE)
    if match:
        score = int(match.group(1))
        return 1.0 - score / 10.0   # convert to uncertainty: 10/10 → 0 (certain)
    return None


def self_confidence_uncertainty(sampled: SampledSet) -> float:
    """Mean uncertainty from self-reported confidence across all probe generations."""
    scores = []
    for gen in sampled.generations:
        conf = parse_self_confidence(gen.output)
        if conf is not None:
            scores.append(conf)
    if not scores:
        return 0.5  # neutral fallback when model doesn't report confidence
    return float(np.mean(scores))


def compute_uncertainty(
    signal: str,
    sampled: SampledSet,
    answers: list[Optional[str]],
) -> float:
    """Dispatch to the correct uncertainty signal.

    Args:
        signal: one of "answer_entropy", "token_entropy", "self_confidence"
        sampled: probe SampledSet
        answers: pre-extracted answers parallel to sampled.generations
    """
    if signal == "answer_entropy":
        return answer_entropy(answers)
    elif signal == "token_entropy":
        return token_entropy(sampled)
    elif signal == "self_confidence":
        return self_confidence_uncertainty(sampled)
    else:
        raise ValueError(f"Unknown uncertainty signal: {signal}")
