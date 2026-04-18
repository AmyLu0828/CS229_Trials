"""Accuracy and compute bookkeeping for experiment results.

Evaluator accumulates per-question results, then computes aggregate metrics.
"""

from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass
class QuestionResult:
    problem_id: str
    question: str
    ground_truth: str
    predicted_answer: Optional[str]
    is_correct: bool
    n_samples: int
    total_tokens: int
    flops_proxy: int          # total_tokens × param_count × 2
    uncertainty: Optional[float] = None
    method: str = ""
    model_name: str = ""


@dataclass
class AggregateResult:
    method: str
    model_name: str
    n_questions: int
    accuracy: float
    avg_flops: float
    std_flops: float
    avg_n_samples: float
    std_n_samples: float
    avg_tokens: float


def compute_flops_proxy(n_tokens: int, param_count: int) -> int:
    return n_tokens * param_count * 2


def aggregate(results: list[QuestionResult]) -> AggregateResult:
    if not results:
        raise ValueError("No results to aggregate")
    n = len(results)
    accuracy = sum(r.is_correct for r in results) / n
    flops = np.array([r.flops_proxy for r in results], dtype=float)
    n_samples = np.array([r.n_samples for r in results], dtype=float)
    tokens = np.array([r.total_tokens for r in results], dtype=float)
    return AggregateResult(
        method=results[0].method,
        model_name=results[0].model_name,
        n_questions=n,
        accuracy=accuracy,
        avg_flops=float(np.mean(flops)),
        std_flops=float(np.std(flops)),
        avg_n_samples=float(np.mean(n_samples)),
        std_n_samples=float(np.std(n_samples)),
        avg_tokens=float(np.mean(tokens)),
    )


def print_aggregate_table(aggregates: list[AggregateResult]) -> None:
    header = f"{'Model':<30} {'Method':<25} {'Acc':>6} {'AvgFLOPs':>14} {'StdFLOPs':>14} {'AvgN':>6}"
    print(header)
    print("-" * len(header))
    for r in aggregates:
        print(
            f"{r.model_name:<30} {r.method:<25} {r.accuracy:>6.3f} "
            f"{r.avg_flops:>14.3e} {r.std_flops:>14.3e} {r.avg_n_samples:>6.1f}"
        )
