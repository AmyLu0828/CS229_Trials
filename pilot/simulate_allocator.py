"""Pilot simulation: does adaptive compute allocation beat fixed-N at matched compute?

This is a zero-GPU sanity check. We simulate N_max samples per question from a
realistic latent-difficulty distribution, then evaluate:

    (a) Fixed self-consistency SC-N  for N in {1, 4, 8, 16, 32, 64}
    (b) Oracle adaptive  — upper bound: smallest N that gets the question right
    (c) Heuristic adaptive — our method: entropy of first k=4 samples → N

The per-question data model:
    For each question q we draw a latent "solve probability" p_q from a
    Beta distribution calibrated so that
        greedy (N=1) accuracy  ≈ 0.35
        SC-32 accuracy         ≈ 0.55
        any-of-64 accuracy     ≈ 0.80
    which matches published Qwen2.5-0.5B-Instruct numbers on GSM8K.

    Each of N_max samples is either "correct" (w.p. p_q) or lands on one of
    M=3 incorrect answer modes (uniformly). This lets majority vote fail in
    the realistic way: plurality of wrong-but-consistent samples beats the
    correct answer.

Usage:
    python pilot/simulate_allocator.py

Outputs:
    results/pilot/sim_pareto.png
    results/pilot/sim_summary.txt
    stdout table
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

# ── Config ────────────────────────────────────────────────────────────────────
RNG_SEED = 0
N_QUESTIONS = 500
N_MAX = 64
K_PROBE = 4
N_GRID = [1, 4, 8, 16, 32, 64]
M_WRONG_MODES = 3
HELD_OUT_FRAC = 0.5  # fraction of questions used for threshold tuning

# Beta params chosen so the simulated baselines hit realistic GSM8K targets.
BETA_A, BETA_B = 0.6, 1.0

OUT_DIR = Path(__file__).parent.parent / "results" / "pilot"
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ── Simulation ────────────────────────────────────────────────────────────────

def simulate_samples(
    n_questions: int,
    n_max: int,
    m_wrong: int,
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray]:
    """Return samples[q, i] in {0, 1..m_wrong} where 0 = correct.

    p_q ~ Beta(BETA_A, BETA_B); each sample is correct with probability p_q,
    otherwise uniform over m_wrong wrong modes.
    """
    p_q = rng.beta(BETA_A, BETA_B, size=n_questions)
    u = rng.uniform(size=(n_questions, n_max))
    correct_mask = u < p_q[:, None]
    wrong_modes = rng.integers(1, m_wrong + 1, size=(n_questions, n_max))
    samples = np.where(correct_mask, 0, wrong_modes)
    return samples, p_q


def sc_correct(samples_row: np.ndarray, n: int) -> bool:
    """Majority vote over first n samples is correct (label 0)."""
    counts = Counter(samples_row[:n].tolist())
    max_count = max(counts.values())
    winners = [k for k, v in counts.items() if v == max_count]
    # Deterministic tie-break: prefer label 0 (correct) only if it is tied.
    # This models "the correct answer, when it ties, is as likely as any other"
    # — pick the smallest label, which happens to be 0. That is optimistic for
    # the SC-N baseline (charitable to the baseline we want to beat).
    winner = min(winners)
    return winner == 0


def answer_entropy(samples_row: np.ndarray, k: int) -> float:
    counts = Counter(samples_row[:k].tolist())
    probs = np.array([v / k for v in counts.values()])
    raw = -np.sum(probs * np.log(probs + 1e-12))
    max_h = math.log(k)
    return float(raw / max_h) if max_h > 0 else 0.0


# ── Allocators ────────────────────────────────────────────────────────────────

def oracle_n(samples_row: np.ndarray, n_grid: list[int]) -> int:
    """Smallest N in n_grid such that SC-N is correct; else max(n_grid)."""
    for n in sorted(n_grid):
        if sc_correct(samples_row, n):
            return n
    return max(n_grid)


def heuristic_n(u: float, k: int, n_max: int, tau_low: float, tau_high: float) -> int:
    if u <= tau_low:
        return k
    if u >= tau_high:
        return n_max
    t = (u - tau_low) / (tau_high - tau_low)
    return int(round(k + t * (n_max - k)))


def tune_thresholds(
    uncertainties: np.ndarray,
    samples: np.ndarray,
    k: int,
    n_max: int,
    tau_low_cands: list[float],
    tau_high_cands: list[float],
    target_avg_n: float | None = None,
) -> tuple[float, float]:
    """Grid-search (tau_low, tau_high) to maximize accuracy on the tune split.

    If target_avg_n is given, restrict to threshold pairs whose avg N is within
    5% of the target; otherwise maximize accuracy subject to tau_low<tau_high.
    """
    best = (tau_low_cands[0], tau_high_cands[-1])
    best_score = -math.inf
    for tl in tau_low_cands:
        for th in tau_high_cands:
            if tl >= th:
                continue
            correct = 0
            total_n = 0
            for q, u in enumerate(uncertainties):
                n = heuristic_n(float(u), k, n_max, tl, th)
                total_n += n
                if sc_correct(samples[q], n):
                    correct += 1
            acc = correct / len(uncertainties)
            avg_n = total_n / len(uncertainties)
            if target_avg_n is not None and abs(avg_n - target_avg_n) > 0.1 * target_avg_n:
                continue
            if acc > best_score:
                best_score = acc
                best = (tl, th)
    return best


# ── Evaluation ────────────────────────────────────────────────────────────────

@dataclass
class Result:
    method: str
    accuracy: float
    avg_n: float
    std_n: float


def eval_fixed(samples: np.ndarray, n: int) -> Result:
    correct = sum(sc_correct(samples[q], n) for q in range(len(samples)))
    return Result(f"SC-{n}", correct / len(samples), float(n), 0.0)


def eval_oracle(samples: np.ndarray, n_grid: list[int]) -> Result:
    ns = [oracle_n(samples[q], n_grid) for q in range(len(samples))]
    # Oracle is always correct if any N in the grid can get it right.
    correct = sum(sc_correct(samples[q], ns[q]) for q in range(len(samples)))
    return Result("oracle-adaptive", correct / len(samples), float(np.mean(ns)), float(np.std(ns)))


def eval_heuristic(
    samples: np.ndarray,
    tau_low: float,
    tau_high: float,
    k: int,
    n_max: int,
    label: str,
) -> Result:
    ns = []
    correct = 0
    for q in range(len(samples)):
        u = answer_entropy(samples[q], k)
        n = heuristic_n(u, k, n_max, tau_low, tau_high)
        ns.append(n)
        if sc_correct(samples[q], n):
            correct += 1
    return Result(label, correct / len(samples), float(np.mean(ns)), float(np.std(ns)))


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    rng = np.random.default_rng(RNG_SEED)
    samples, p_q = simulate_samples(N_QUESTIONS, N_MAX, M_WRONG_MODES, rng)

    # Split into tune / eval
    idx = rng.permutation(N_QUESTIONS)
    n_tune = int(HELD_OUT_FRAC * N_QUESTIONS)
    tune_idx, eval_idx = idx[:n_tune], idx[n_tune:]
    tune_samples, eval_samples = samples[tune_idx], samples[eval_idx]

    # ── Fixed baselines ───────────────────────────────────────────────────────
    fixed_results = [eval_fixed(eval_samples, n) for n in N_GRID]

    # ── Oracle ────────────────────────────────────────────────────────────────
    oracle = eval_oracle(eval_samples, N_GRID)

    # ── Heuristic adaptive: dense Pareto sweep over (tau_low, tau_high) ──────
    # Precompute entropies on tune and eval splits
    tune_u = np.array([answer_entropy(tune_samples[q], K_PROBE) for q in range(len(tune_samples))])
    eval_u = np.array([answer_entropy(eval_samples[q], K_PROBE) for q in range(len(eval_samples))])

    tau_low_cands = np.linspace(0.0, 0.9, 19)
    tau_high_cands = np.linspace(0.1, 1.0, 19)

    # Sweep ALL valid threshold pairs on the eval set directly to trace
    # the achievable Pareto curve (this is the "what's possible" envelope).
    # We'll separately check generalization by tuning on tune and evaluating on eval.
    achievable: list[tuple[float, float, float, float]] = []   # (avg_n, acc, tl, th)
    for tl in tau_low_cands:
        for th in tau_high_cands:
            if tl >= th:
                continue
            ns = np.array([heuristic_n(float(u), K_PROBE, N_MAX, tl, th) for u in eval_u])
            correct = sum(sc_correct(eval_samples[q], int(ns[q])) for q in range(len(eval_samples)))
            achievable.append((float(ns.mean()), correct / len(eval_samples), float(tl), float(th)))

    # Pareto frontier (non-dominated points: for a given avg_n, keep max acc)
    achievable.sort(key=lambda x: x[0])
    pareto: list[tuple[float, float, float, float]] = []
    best_acc = -1.0
    for pt in achievable:
        if pt[1] > best_acc:
            pareto.append(pt)
            best_acc = pt[1]

    # Also evaluate a few tune-then-eval points to check generalization.
    # Bucket tune points by avg_n target and pick the best (tl, th) per bucket.
    adaptive_results: list[Result] = []
    for target_n in [6, 10, 16, 24, 40]:
        best_tl, best_th, best_acc_tune = 0.0, 1.0, -1.0
        for tl in tau_low_cands:
            for th in tau_high_cands:
                if tl >= th:
                    continue
                ns_t = np.array([heuristic_n(float(u), K_PROBE, N_MAX, tl, th) for u in tune_u])
                if abs(ns_t.mean() - target_n) > 0.15 * target_n:
                    continue
                correct_t = sum(sc_correct(tune_samples[q], int(ns_t[q]))
                                for q in range(len(tune_samples)))
                acc_t = correct_t / len(tune_samples)
                if acc_t > best_acc_tune:
                    best_acc_tune = acc_t
                    best_tl, best_th = float(tl), float(th)
        if best_acc_tune < 0:
            continue
        r = eval_heuristic(eval_samples, best_tl, best_th, K_PROBE, N_MAX,
                           f"adaptive@~{target_n}")
        adaptive_results.append(r)

    adaptive_best = adaptive_results[-1] if adaptive_results else Result("adaptive-none", 0, 0, 0)

    # ── Report ────────────────────────────────────────────────────────────────
    all_results = fixed_results + adaptive_results + [oracle]

    lines = []
    header = f"{'Method':<20} {'Accuracy':>10} {'Avg N':>8} {'Std N':>8}"
    lines.append(header)
    lines.append("-" * len(header))
    for r in all_results:
        lines.append(f"{r.method:<20} {r.accuracy:>10.3f} {r.avg_n:>8.2f} {r.std_n:>8.2f}")

    # Simulated baseline sanity check
    lines.append("")
    lines.append(f"Simulated baselines (target: greedy≈0.35, SC-32≈0.55, any-of-64≈0.80):")
    lines.append(f"  greedy   = {fixed_results[0].accuracy:.3f}")
    lines.append(f"  SC-32    = {fixed_results[4].accuracy:.3f}")
    lines.append(f"  oracle   = {oracle.accuracy:.3f}")
    lines.append(f"  p_q mean = {p_q.mean():.3f}   std = {p_q.std():.3f}")

    text = "\n".join(lines)
    print(text)
    (OUT_DIR / "sim_summary.txt").write_text(text + "\n")

    # ── Plot ──────────────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(7, 5))
    xs_fixed = [r.avg_n for r in fixed_results]
    ys_fixed = [r.accuracy for r in fixed_results]
    ax.plot(xs_fixed, ys_fixed, "o-", color="steelblue", label="Fixed SC-N", markersize=7)
    for r in fixed_results:
        ax.annotate(r.method, (r.avg_n, r.accuracy),
                    textcoords="offset points", xytext=(5, -10), fontsize=8, color="steelblue")

    # Achievable Pareto envelope (on-eval upper bound for the heuristic family)
    px = [pt[0] for pt in pareto]
    py = [pt[1] for pt in pareto]
    ax.plot(px, py, "-", color="lightcoral", alpha=0.6, linewidth=1.5,
            label="Heuristic achievable frontier (on eval)")

    xs_a = [r.avg_n for r in adaptive_results]
    ys_a = [r.accuracy for r in adaptive_results]
    ax.plot(xs_a, ys_a, "s", color="crimson", markersize=9,
            label="Heuristic adaptive (tuned on held-out)")

    ax.scatter([oracle.avg_n], [oracle.accuracy],
               marker="*", s=220, color="goldenrod",
               label="Oracle adaptive (upper bound)", zorder=5)

    ax.set_xlabel("Average number of samples per question")
    ax.set_ylabel("Accuracy")
    ax.set_title("Simulated: accuracy vs. average compute\n(calibrated to Qwen2.5-0.5B / GSM8K)")
    ax.set_xscale("log")
    ax.legend(loc="lower right")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    path = OUT_DIR / "sim_pareto.png"
    fig.savefig(path, dpi=130)
    plt.close(fig)
    print(f"\nSaved plot to {path}")


if __name__ == "__main__":
    main()
