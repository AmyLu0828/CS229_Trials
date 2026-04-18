"""Dataset loaders for GSM8K and MATH-500 with answer extraction and normalization.

Problems are selected deterministically via seeded shuffle so every run
uses the exact same subset.
"""

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Optional

import numpy as np
from datasets import load_dataset


@dataclass
class Problem:
    id: str
    question: str
    answer: str          # normalized ground-truth answer string
    raw_answer: str      # original reference answer text


def load_gsm8k(n: int = 200, seed: int = 42) -> list[Problem]:
    """Load n problems from GSM8K test set, deterministically shuffled."""
    ds = load_dataset("openai/gsm8k", "main", split="test")
    rng = np.random.default_rng(seed)
    indices = rng.permutation(len(ds))[:n]
    problems = []
    for idx in indices:
        row = ds[int(idx)]
        raw_ans = row["answer"]
        answer = _extract_gsm8k_answer(raw_ans)
        problems.append(
            Problem(
                id=f"gsm8k_{idx}",
                question=row["question"],
                answer=normalize_answer(answer),
                raw_answer=raw_ans,
            )
        )
    return problems


def load_math500(n: int = 200, seed: int = 42) -> list[Problem]:
    """Load n problems from MATH-500, deterministically shuffled."""
    ds = load_dataset("HuggingFaceH4/MATH-500", split="test")
    rng = np.random.default_rng(seed)
    indices = rng.permutation(len(ds))[:n]
    problems = []
    for idx in indices:
        row = ds[int(idx)]
        raw_ans = row["answer"]
        problems.append(
            Problem(
                id=f"math500_{idx}",
                question=row["problem"],
                answer=normalize_answer(raw_ans),
                raw_answer=raw_ans,
            )
        )
    return problems


# ── Answer extraction ────────────────────────────────────────────────────────

def _extract_gsm8k_answer(text: str) -> str:
    """Extract the number after #### in a GSM8K reference answer."""
    match = re.search(r"####\s*([\d,\.\-]+)", text)
    if match:
        return match.group(1).replace(",", "")
    # fallback: last number in the text
    numbers = re.findall(r"-?\d[\d,\.]*", text)
    if numbers:
        return numbers[-1].replace(",", "")
    return text.strip()


def extract_gsm8k_from_generation(text: str) -> Optional[str]:
    """Extract a final numerical answer from a model generation for GSM8K."""
    # prefer the #### marker the model was asked to use
    match = re.search(r"####\s*([\d,\.\-]+)", text)
    if match:
        return match.group(1).replace(",", "")
    # last line that contains a number
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    for line in reversed(lines):
        nums = re.findall(r"-?\d[\d,\.]*", line)
        if nums:
            return nums[-1].replace(",", "")
    # absolute fallback: last number anywhere
    all_nums = re.findall(r"-?\d[\d,\.]*", text)
    if all_nums:
        return all_nums[-1].replace(",", "")
    return None


def extract_math500_from_generation(text: str) -> Optional[str]:
    """Extract a final answer from a model generation for MATH-500."""
    # prefer \boxed{...}
    match = re.search(r"\\boxed\{([^}]+)\}", text)
    if match:
        return match.group(1).strip()
    # last $...$ expression
    matches = re.findall(r"\$([^\$]+)\$", text)
    if matches:
        return matches[-1].strip()
    return None


def extract_answer(text: str, dataset: str) -> Optional[str]:
    """Route to the correct extractor by dataset name."""
    if dataset == "gsm8k":
        raw = extract_gsm8k_from_generation(text)
    elif dataset == "math500":
        raw = extract_math500_from_generation(text)
    else:
        raise ValueError(f"Unknown dataset: {dataset}")
    return normalize_answer(raw) if raw is not None else None


# ── Normalization ─────────────────────────────────────────────────────────────

def normalize_answer(answer: Optional[str]) -> str:
    """Normalize an answer string for comparison.

    - Strip whitespace
    - Remove trailing .0 from floats that are whole numbers
    - Lowercase
    - Strip commas from numbers
    """
    if answer is None:
        return ""
    s = str(answer).strip().lower()
    s = s.replace(",", "")
    # convert simple fractions to decimals
    frac_match = re.fullmatch(r"(-?\d+)\s*/\s*(-?\d+)", s)
    if frac_match:
        try:
            val = Fraction(int(frac_match.group(1)), int(frac_match.group(2)))
            s = str(float(val))
        except (ZeroDivisionError, ValueError):
            pass
    # strip trailing .0
    try:
        f = float(s)
        if f == int(f):
            s = str(int(f))
        else:
            s = str(f)
    except ValueError:
        pass
    return s


# ── Prompt formatting ─────────────────────────────────────────────────────────

GSM8K_PROMPT_TEMPLATE = (
    'Solve the following math problem step by step. '
    'Put your final numerical answer on the last line after "####".\n\n'
    "Problem: {question}"
)

MATH500_PROMPT_TEMPLATE = (
    "Solve the following math problem step by step. "
    r"Put your final answer inside \boxed{{}}." + "\n\n"
    "Problem: {question}"
)


def format_prompt(question: str, dataset: str) -> str:
    if dataset == "gsm8k":
        return GSM8K_PROMPT_TEMPLATE.format(question=question)
    elif dataset == "math500":
        return MATH500_PROMPT_TEMPLATE.format(question=question)
    raise ValueError(f"Unknown dataset: {dataset}")
