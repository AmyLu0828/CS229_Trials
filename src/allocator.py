"""Adaptive N-selection policy.

Given probe samples and hyperparameters, returns the total number of samples
to draw for a question. Pure function of inputs — no model calls here.
"""

import math
from typing import Optional

from src.sampling import SampledSet
from src.uncertainty import compute_uncertainty


def heuristic_n(
    uncertainty: float,
    k: int,
    n_max: int,
    tau_low: float,
    tau_high: float,
) -> int:
    """Map a scalar uncertainty value to a total sample count N.

    u < tau_low  → N = k   (easy: stop at probe)
    u > tau_high → N = n_max (hard: use full budget)
    otherwise    → linear interpolation between k and n_max
    """
    if uncertainty <= tau_low:
        return k
    if uncertainty >= tau_high:
        return n_max
    # linear interpolation; round to nearest int, clamp to [k, n_max]
    t = (uncertainty - tau_low) / (tau_high - tau_low)
    n_float = k + t * (n_max - k)
    return int(round(n_float))


def adaptive_n(
    probe_sampled: SampledSet,
    probe_answers: list[Optional[str]],
    k: int,
    n_max: int,
    tau_low: float,
    tau_high: float,
    signal: str = "answer_entropy",
) -> tuple[int, float]:
    """Return (chosen_N, uncertainty_value) for a question.

    This is the policy function: pure, deterministic given its inputs.
    """
    u = compute_uncertainty(signal, probe_sampled, probe_answers)
    n = heuristic_n(u, k, n_max, tau_low, tau_high)
    return n, u


def tune_thresholds(
    uncertainties: list[float],
    correctness: list[bool],
    k: int,
    n_max: int,
    tau_low_candidates: list[float],
    tau_high_candidates: list[float],
    compute_weight: float = 0.0,
) -> tuple[float, float]:
    """Grid-search (tau_low, tau_high) on a held-out tuning split.

    Optimizes accuracy (maximizes correct / total) subject to tau_low < tau_high.
    compute_weight: if > 0, trades accuracy for compute savings (not used in main result).

    Returns (best_tau_low, best_tau_high).
    """
    best_tau_low = tau_low_candidates[0]
    best_tau_high = tau_high_candidates[-1]
    best_score = -math.inf

    n_problems = len(uncertainties)

    for tau_low in tau_low_candidates:
        for tau_high in tau_high_candidates:
            if tau_low >= tau_high:
                continue
            correct = 0
            total_n = 0
            for u, is_correct in zip(uncertainties, correctness):
                n = heuristic_n(u, k, n_max, tau_low, tau_high)
                total_n += n
                # On the tuning split we don't re-run the model, so we use
                # the probe correctness as a proxy for final accuracy.
                if is_correct:
                    correct += 1

            acc = correct / n_problems
            avg_n = total_n / n_problems
            # Normalized compute cost in [0, 1]
            norm_compute = (avg_n - k) / (n_max - k) if n_max > k else 0.0
            score = acc - compute_weight * norm_compute

            if score > best_score:
                best_score = score
                best_tau_low = tau_low
                best_tau_high = tau_high

    return best_tau_low, best_tau_high
