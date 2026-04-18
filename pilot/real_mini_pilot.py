"""Real-model mini pilot: 40 GSM8K questions, Qwen2.5-0.5B, 16 samples each.

Runs in ~15 min on a Colab T4. One generation pass per question — all methods
(SC-1, SC-4, SC-8, SC-16, heuristic adaptive, oracle) are derived from that
one pass by slicing the sample set. No caching infrastructure needed for this
pilot; we just hold samples in memory and write one JSON at the end.

Use this to validate whether the findings from pilot/simulate_allocator.py
carry over to the real model before committing to the full experiment plan.

Colab setup:
    !pip install -q transformers accelerate datasets matplotlib numpy
    !python pilot/real_mini_pilot.py

Outputs:
    results/pilot/real_mini_raw.jsonl    (per-question samples + answers)
    results/pilot/real_mini_summary.txt  (accuracy table)
    results/pilot/real_mini_pareto.png
"""

from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from fractions import Fraction
from pathlib import Path
from typing import Optional

import matplotlib.pyplot as plt
import numpy as np

# ── Config ────────────────────────────────────────────────────────────────────
HF_PATH = "Qwen/Qwen2.5-0.5B-Instruct"
N_QUESTIONS = 100
N_MAX = 32                       # samples drawn per question
K_PROBE = 8                      # probe size for uncertainty
N_GRID = [1, 2, 4, 8, 16, 32]    # fixed-N points to compare
TEMPERATURE = 0.7
MAX_NEW_TOKENS = 512
SEED = 42

def _resolve_out_dir() -> Path:
    """Prefer the repo layout; otherwise fall back to cwd so the script is
    runnable standalone (e.g. as a single Colab cell)."""
    try:
        candidate = Path(__file__).parent.parent / "results" / "pilot"
    except NameError:
        candidate = Path.cwd() / "results" / "pilot"
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


OUT_DIR = _resolve_out_dir()

PROMPT_TEMPLATE = (
    'Solve the following math problem step by step. '
    'Put your final numerical answer on the last line after "####".\n\n'
    "Problem: {question}"
)


# ── Answer extraction ────────────────────────────────────────────────────────

def extract_gsm8k_ref(text: str) -> str:
    m = re.search(r"####\s*([\d,\.\-]+)", text)
    return (m.group(1) if m else text.strip()).replace(",", "")


def extract_gsm8k_gen(text: str) -> Optional[str]:
    m = re.search(r"####\s*([\d,\.\-]+)", text)
    if m:
        return m.group(1).replace(",", "")
    nums = re.findall(r"-?\d[\d,\.]*", text)
    return nums[-1].replace(",", "") if nums else None


def normalize(s: Optional[str]) -> str:
    if s is None:
        return ""
    s = str(s).strip().lower().replace(",", "")
    frac = re.fullmatch(r"(-?\d+)\s*/\s*(-?\d+)", s)
    if frac:
        try:
            s = str(float(Fraction(int(frac.group(1)), int(frac.group(2)))))
        except Exception:
            pass
    try:
        f = float(s)
        s = str(int(f)) if f == int(f) else str(f)
    except ValueError:
        pass
    return s


# ── Selection / allocator helpers ────────────────────────────────────────────

def majority(answers: list[Optional[str]]) -> Optional[str]:
    valid = [a for a in answers if a]
    if not valid:
        return None
    c = Counter(valid)
    top = c.most_common(1)[0][1]
    for a in valid:
        if c[a] == top:
            return a
    return None


def answer_entropy(answers: list[Optional[str]]) -> float:
    n = len(answers)
    if n == 0:
        return 1.0
    canonical = [a if a else "__NONE__" for a in answers]
    c = Counter(canonical)
    probs = [v / n for v in c.values()]
    raw = -sum(p * math.log(p) for p in probs if p > 0)
    return raw / math.log(n) if n > 1 else 0.0


def heuristic_n(u: float, k: int, n_max: int, tl: float, th: float) -> int:
    if u <= tl:
        return k
    if u >= th:
        return n_max
    t = (u - tl) / (th - tl)
    return int(round(k + t * (n_max - k)))


# ── Generation ────────────────────────────────────────────────────────────────

def load_model():
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(HF_PATH, trust_remote_code=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cpu":
        print("WARNING: no GPU detected. This script is ~50x slower on CPU.")
        print("         In Colab, select Runtime -> Change runtime type -> T4 GPU.")
    model = AutoModelForCausalLM.from_pretrained(
        HF_PATH,
        torch_dtype=torch.bfloat16 if device == "cuda" else torch.float32,
        trust_remote_code=True,
    ).to(device)
    model.eval()
    return tok, model, device


def generate_n_samples(tok, model, device, prompt: str, n: int) -> list[str]:
    """Draw n stochastic samples in a single batched generate() call."""
    import torch
    messages = [{"role": "user", "content": prompt}]
    formatted = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    enc = tok(formatted, return_tensors="pt").to(device)
    input_len = enc["input_ids"].shape[1]

    torch.manual_seed(SEED)
    with torch.no_grad():
        out = model.generate(
            input_ids=enc["input_ids"],
            attention_mask=enc["attention_mask"],
            max_new_tokens=MAX_NEW_TOKENS,
            do_sample=True,
            temperature=TEMPERATURE,
            num_return_sequences=n,
            pad_token_id=tok.eos_token_id,
        )
    gens = []
    for seq in out:
        text = tok.decode(seq[input_len:], skip_special_tokens=True)
        gens.append(text)
    return gens


# ── Problem loading ───────────────────────────────────────────────────────────

def load_problems(n: int, seed: int = SEED) -> list[dict]:
    from datasets import load_dataset
    ds = load_dataset("openai/gsm8k", "main", split="test")
    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(ds))[:n]
    return [
        {
            "id": f"gsm8k_{int(i)}",
            "question": ds[int(i)]["question"],
            "answer_raw": ds[int(i)]["answer"],
            "answer": normalize(extract_gsm8k_ref(ds[int(i)]["answer"])),
        }
        for i in idx
    ]


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"Loading {HF_PATH} ...")
    tok, model, device = load_model()
    print(f"Running on: {device}")
    problems = load_problems(N_QUESTIONS)

    raw_path = OUT_DIR / "real_mini_raw.jsonl"
    per_q: list[dict] = []

    t0 = time.time()
    with open(raw_path, "w") as fout:
        for qi, p in enumerate(problems):
            prompt = PROMPT_TEMPLATE.format(question=p["question"])
            gens = generate_n_samples(tok, model, device, prompt, N_MAX)
            answers = [normalize(extract_gsm8k_gen(g)) for g in gens]
            row = {
                "id": p["id"],
                "question": p["question"],
                "ground_truth": p["answer"],
                "samples": gens,
                "answers": answers,
            }
            fout.write(json.dumps(row) + "\n")
            fout.flush()
            per_q.append(row)
            elapsed = time.time() - t0
            eta = elapsed / (qi + 1) * (len(problems) - qi - 1)
            print(f"  [{qi + 1}/{len(problems)}] elapsed={elapsed:5.0f}s eta={eta:5.0f}s",
                  flush=True)

    # ── Derive all methods from the raw samples ──────────────────────────────
    gts = [r["ground_truth"] for r in per_q]
    all_answers = [r["answers"] for r in per_q]

    def sc_correct(answers: list[str], n: int, gt: str) -> bool:
        pred = majority(answers[:n])
        return pred == gt and pred != ""

    # Fixed SC-N
    fixed: dict[int, float] = {}
    for n in N_GRID:
        correct = sum(sc_correct(all_answers[q], n, gts[q]) for q in range(len(per_q)))
        fixed[n] = correct / len(per_q)

    # Oracle adaptive
    oracle_ns = []
    oracle_correct = 0
    for q in range(len(per_q)):
        found = None
        for n in sorted(N_GRID):
            if sc_correct(all_answers[q], n, gts[q]):
                found = n
                break
        oracle_ns.append(found if found is not None else N_MAX)
        if found is not None:
            oracle_correct += 1
    oracle_acc = oracle_correct / len(per_q)
    oracle_avg_n = float(np.mean(oracle_ns))

    # Heuristic adaptive: sweep (tau_low, tau_high) on all questions to trace
    # achievable Pareto (this is in-sample because the pilot is tiny; a real
    # experiment would tune on a held-out split).
    tl_cands = np.linspace(0.0, 0.9, 10)
    th_cands = np.linspace(0.1, 1.0, 10)
    achievable = []
    entropies = [answer_entropy(all_answers[q][:K_PROBE]) for q in range(len(per_q))]
    for tl in tl_cands:
        for th in th_cands:
            if tl >= th:
                continue
            ns = [heuristic_n(entropies[q], K_PROBE, N_MAX, float(tl), float(th))
                  for q in range(len(per_q))]
            correct = sum(sc_correct(all_answers[q], ns[q], gts[q]) for q in range(len(per_q)))
            achievable.append((float(np.mean(ns)), correct / len(per_q), float(tl), float(th)))

    achievable.sort(key=lambda x: x[0])
    pareto = []
    best_acc = -1.0
    for pt in achievable:
        if pt[1] > best_acc:
            pareto.append(pt)
            best_acc = pt[1]

    # ── Report ────────────────────────────────────────────────────────────────
    lines = []
    lines.append(f"Real mini pilot — {HF_PATH} on {len(per_q)} GSM8K questions, N_max={N_MAX}")
    lines.append("")
    lines.append(f"{'Method':<25} {'Accuracy':>10} {'Avg N':>8}")
    lines.append("-" * 48)
    for n in N_GRID:
        lines.append(f"{f'SC-{n}':<25} {fixed[n]:>10.3f} {float(n):>8.2f}")
    lines.append(f"{'oracle-adaptive':<25} {oracle_acc:>10.3f} {oracle_avg_n:>8.2f}")
    lines.append("")
    lines.append("Heuristic adaptive — achievable Pareto frontier (in-sample):")
    for avg_n, acc, tl, th in pareto:
        lines.append(f"  avg_n={avg_n:5.2f}  acc={acc:.3f}  (tau_low={tl:.2f}, tau_high={th:.2f})")

    text = "\n".join(lines)
    print("\n" + text)
    (OUT_DIR / "real_mini_summary.txt").write_text(text + "\n")

    # ── Plot ──────────────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(7, 5))
    xs = list(fixed.keys())
    ys = list(fixed.values())
    ax.plot(xs, ys, "o-", color="steelblue", markersize=7, label="Fixed SC-N")
    for n, a in fixed.items():
        ax.annotate(f"SC-{n}", (n, a), textcoords="offset points", xytext=(5, -10),
                    fontsize=8, color="steelblue")
    px = [pt[0] for pt in pareto]
    py = [pt[1] for pt in pareto]
    ax.plot(px, py, "s-", color="crimson", markersize=7, label="Heuristic adaptive (in-sample)")
    ax.scatter([oracle_avg_n], [oracle_acc], marker="*", s=220, color="goldenrod",
               label="Oracle adaptive", zorder=5)
    ax.set_xlabel("Average number of samples per question")
    ax.set_ylabel("Accuracy")
    ax.set_xscale("log")
    ax.set_title(f"Real pilot — {HF_PATH.split('/')[-1]} on GSM8K ({len(per_q)} q)")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT_DIR / "real_mini_pareto.png", dpi=130)
    plt.close(fig)
    print(f"\nSaved plot:    {OUT_DIR / 'real_mini_pareto.png'}")
    print(f"Saved summary: {OUT_DIR / 'real_mini_summary.txt'}")
    print(f"Saved raw:     {OUT_DIR / 'real_mini_raw.jsonl'}")
    print(f"\nTotal wall-clock: {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
