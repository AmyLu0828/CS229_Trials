"""
Utilities for parsing per-token logprobs from a LiteLLM/OpenAI-style response
and computing TECA-NLL signals (surprisal-based proxies for the token-entropy
cumulative average signal of Liu et al., ICLR 2025, adapted for providers that
only expose the chosen-token logprob).

Only the chosen-token `logprob` per position is needed. Together's serverless
DeepSeek-R1 returns this for every generated token (including the reasoning
section between <think> and </think>), which is what we consume here.

All functions are pure.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class TecaSummary:
    """Per-turn TECA-NLL summary over a chosen-token logprob stream."""

    n_total_tokens: int
    n_reasoning_tokens: int
    n_content_tokens: int
    # reasoning span (between <think> and </think>, inclusive of interior only)
    nll_mean_reasoning: Optional[float]
    nll_sum_reasoning: Optional[float]
    nll_var_reasoning: Optional[float]
    nll_max_reasoning: Optional[float]
    # final answer / action span (after </think>)
    nll_mean_content: Optional[float]
    nll_sum_content: Optional[float]
    # full generation (includes think tags themselves)
    nll_mean_total: Optional[float]
    # boundaries in the logprobs.content list
    think_open_idx: Optional[int]
    think_close_idx: Optional[int]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _find_tag_indices(
    tokens: List[Dict[str, Any]], tag: str
) -> Optional[int]:
    """Return the index of the first token whose surface form *contains* `tag`.

    DeepSeek-R1 on Together emits `<think>` and `</think>` as single tokens, but
    we fall back to substring-in-token in case a provider splits them.
    """
    for i, tok in enumerate(tokens):
        s = tok.get("token") or ""
        if tag in s:
            return i
    return None


def _mean(xs: List[float]) -> Optional[float]:
    if not xs:
        return None
    return sum(xs) / len(xs)


def _var(xs: List[float]) -> Optional[float]:
    if len(xs) < 2:
        return 0.0 if xs else None
    m = sum(xs) / len(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


def logprobs_to_dicts(logprobs_obj: Any) -> List[Dict[str, Any]]:
    """Normalize a ChoiceLogprobs (LiteLLM/pydantic) or dict into a plain list.

    Returns list of {token, logprob} dicts. `top_logprobs` is dropped because
    current serverless providers cap it at 1 entry anyway — we only keep the
    chosen-token surprisal.
    """
    if logprobs_obj is None:
        return []
    if hasattr(logprobs_obj, "model_dump"):
        d = logprobs_obj.model_dump()
    elif isinstance(logprobs_obj, dict):
        d = logprobs_obj
    else:
        try:
            d = dict(logprobs_obj)
        except Exception:
            return []
    raw = d.get("content") or []
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "token": item.get("token", ""),
                "logprob": float(item.get("logprob") or 0.0),
            }
        )
    return out


def compute_teca_summary(
    tokens: List[Dict[str, Any]],
    open_tag: str = "<think>",
    close_tag: str = "</think>",
) -> TecaSummary:
    """Compute TECA-NLL stats from a chosen-token logprob stream.

    NLL per token is `-logprob`. We drop tokens whose logprob is exactly 0.0
    from the reasoning-span statistics because providers report 0.0 for forced
    / prefix tokens (e.g., the <think> tag itself) which would bias the mean
    down. If all tokens in a span happen to be 0.0 we leave the mean at 0.0.
    """
    n_total = len(tokens)
    if n_total == 0:
        return TecaSummary(
            n_total_tokens=0,
            n_reasoning_tokens=0,
            n_content_tokens=0,
            nll_mean_reasoning=None,
            nll_sum_reasoning=None,
            nll_var_reasoning=None,
            nll_max_reasoning=None,
            nll_mean_content=None,
            nll_sum_content=None,
            nll_mean_total=None,
            think_open_idx=None,
            think_close_idx=None,
        )

    open_idx = _find_tag_indices(tokens, open_tag)
    close_idx = _find_tag_indices(tokens, close_tag)

    if open_idx is None:
        reasoning_slice: Tuple[int, int] = (0, 0)
        content_slice: Tuple[int, int] = (0, n_total)
    elif close_idx is None:
        reasoning_slice = (open_idx + 1, n_total)
        content_slice = (n_total, n_total)
    else:
        reasoning_slice = (open_idx + 1, close_idx)
        content_slice = (close_idx + 1, n_total)

    r_nlls = [
        -t["logprob"]
        for t in tokens[reasoning_slice[0]:reasoning_slice[1]]
        if t["logprob"] != 0.0
    ]
    c_nlls = [
        -t["logprob"]
        for t in tokens[content_slice[0]:content_slice[1]]
        if t["logprob"] != 0.0
    ]
    all_nlls = [-t["logprob"] for t in tokens if t["logprob"] != 0.0]

    return TecaSummary(
        n_total_tokens=n_total,
        n_reasoning_tokens=max(0, reasoning_slice[1] - reasoning_slice[0]),
        n_content_tokens=max(0, content_slice[1] - content_slice[0]),
        nll_mean_reasoning=_mean(r_nlls),
        nll_sum_reasoning=(sum(r_nlls) if r_nlls else None),
        nll_var_reasoning=_var(r_nlls),
        nll_max_reasoning=(max(r_nlls) if r_nlls else None),
        nll_mean_content=_mean(c_nlls),
        nll_sum_content=(sum(c_nlls) if c_nlls else None),
        nll_mean_total=_mean(all_nlls),
        think_open_idx=open_idx,
        think_close_idx=close_idx,
    )
