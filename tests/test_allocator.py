"""Tests for the adaptive allocator policy."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.allocator import heuristic_n, tune_thresholds


def test_heuristic_n_easy():
    # u below tau_low → k
    assert heuristic_n(0.1, k=4, n_max=64, tau_low=0.3, tau_high=0.7) == 4


def test_heuristic_n_hard():
    # u above tau_high → n_max
    assert heuristic_n(0.9, k=4, n_max=64, tau_low=0.3, tau_high=0.7) == 64


def test_heuristic_n_midpoint():
    # u = 0.5 exactly between tau_low=0.3 and tau_high=0.7 → halfway between 4 and 64
    n = heuristic_n(0.5, k=4, n_max=64, tau_low=0.3, tau_high=0.7)
    assert 4 <= n <= 64


def test_heuristic_n_at_boundaries():
    assert heuristic_n(0.3, k=4, n_max=64, tau_low=0.3, tau_high=0.7) == 4
    assert heuristic_n(0.7, k=4, n_max=64, tau_low=0.3, tau_high=0.7) == 64


def test_tune_thresholds_selects_valid_pair():
    # Synthetic: high uncertainty → wrong, low uncertainty → right
    uncertainties = [0.1, 0.1, 0.9, 0.9]
    correctness = [True, True, False, False]
    tau_low, tau_high = tune_thresholds(
        uncertainties, correctness, k=4, n_max=64,
        tau_low_candidates=[0.2, 0.4],
        tau_high_candidates=[0.6, 0.8],
    )
    assert tau_low < tau_high
    assert tau_low in [0.2, 0.4]
    assert tau_high in [0.6, 0.8]
