"""Sampling primitives: greedy decoding and Best-of-N stochastic sampling.

All functions return lists of Generation objects and route through the model's
cache transparently.
"""

from dataclasses import dataclass
from typing import Optional

from src.models import Generation, ModelWrapper


@dataclass
class SampledSet:
    """A collection of generations for one question under one (method, N) config."""
    prompt: str
    generations: list[Generation]
    seeds: list[int]

    @property
    def n(self) -> int:
        return len(self.generations)

    @property
    def total_tokens(self) -> int:
        return sum(g.n_tokens for g in self.generations)


def greedy(
    model: ModelWrapper,
    prompt: str,
    seed: int = 0,
) -> SampledSet:
    """Single greedy (temperature=0) generation."""
    gen = model.generate_one(prompt, temperature=0.0, seed=seed)
    return SampledSet(prompt=prompt, generations=[gen], seeds=[seed])


def best_of_n(
    model: ModelWrapper,
    prompt: str,
    n: int,
    temperature: float = 0.7,
    base_seed: int = 42,
    max_batch_size: int = 16,
) -> SampledSet:
    """Sample N completions for a single prompt.

    Seeds are [base_seed, base_seed+1, ..., base_seed+N-1] so that
    a larger N always includes the samples from smaller N as a prefix.
    """
    seeds = list(range(base_seed, base_seed + n))
    gens = model.generate_batch(
        prompt,
        temperature=temperature,
        seeds=seeds,
        max_batch_size=max_batch_size,
    )
    return SampledSet(prompt=prompt, generations=gens, seeds=seeds)


def probe_then_topup(
    model: ModelWrapper,
    prompt: str,
    k: int,
    n_total: int,
    temperature: float = 0.7,
    base_seed: int = 42,
    max_batch_size: int = 16,
) -> SampledSet:
    """Draw k probe samples, then top up to n_total if n_total > k.

    Because seeds are consecutive from base_seed, the probe samples are
    always a prefix of the full sample set — no re-generation needed.
    """
    if n_total < k:
        raise ValueError(f"n_total ({n_total}) must be >= k ({k})")

    if n_total == k:
        return best_of_n(model, prompt, k, temperature, base_seed, max_batch_size)

    return best_of_n(model, prompt, n_total, temperature, base_seed, max_batch_size)


def get_probe(sampled: SampledSet, k: int) -> SampledSet:
    """Return the first k samples from an existing SampledSet (no new model calls)."""
    return SampledSet(
        prompt=sampled.prompt,
        generations=sampled.generations[:k],
        seeds=sampled.seeds[:k],
    )
