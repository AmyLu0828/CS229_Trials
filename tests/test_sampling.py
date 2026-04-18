"""Tests for sampling primitives (no GPU required — uses mock model)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models import Generation
from src.sampling import SampledSet, best_of_n, get_probe, greedy


def _mock_model(model_name: str = "test-model", param_count: int = 1_000_000) -> MagicMock:
    m = MagicMock()
    m.model_name = model_name
    m.param_count = param_count
    m.max_new_tokens = 128
    m.generate_one.return_value = Generation(output="The answer is 42\n#### 42", n_tokens=10, token_log_probs=None)
    m.generate_batch.side_effect = lambda prompt, temperature, seeds, **kwargs: [
        Generation(output=f"answer {s}\n#### {s}", n_tokens=8, token_log_probs=None)
        for s in seeds
    ]
    return m


def test_greedy_returns_one_sample():
    model = _mock_model()
    result = greedy(model, "What is 2+2?", seed=0)
    assert result.n == 1
    assert len(result.generations) == 1
    model.generate_one.assert_called_once()


def test_best_of_n_uses_consecutive_seeds():
    model = _mock_model()
    result = best_of_n(model, "What is 2+2?", n=4, base_seed=10)
    assert result.n == 4
    _, kwargs = model.generate_batch.call_args
    seeds = kwargs.get("seeds") or model.generate_batch.call_args[0][2]
    assert seeds == [10, 11, 12, 13]


def test_get_probe_is_prefix():
    model = _mock_model()
    full = best_of_n(model, "q", n=8, base_seed=0)
    probe = get_probe(full, k=4)
    assert probe.n == 4
    assert probe.seeds == [0, 1, 2, 3]
    assert probe.generations == full.generations[:4]


def test_total_tokens():
    model = _mock_model()
    result = best_of_n(model, "q", n=4, base_seed=0)
    assert result.total_tokens == 4 * 8  # 4 seeds × 8 tokens each
