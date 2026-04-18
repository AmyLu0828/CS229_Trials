"""Scope A — Tier 3 intrinsic-signal methods vs. Tier 0/1/2 baselines.

Reuses pilot #1's saved generations (results/pilot/real_mini_raw.jsonl) and
re-scores them with a teacher-forced forward pass to extract per-token log
probabilities. Then computes adaptive allocation using three new signals
beyond answer-entropy, plus a learned fusion of them.

Methods compared on the same 40-question GSM8K slice (k=4, N_max=16, Qwen-0.5B):

  Tier 0/1  —  Greedy-as-SC-1, fixed SC-{2, 4, 8, 16}          (baselines)
  Tier 2    —  Adaptive with answer-entropy                    (pilot #1 method)
  Tier 3a   —  Adaptive with mean probe surprisal
  Tier 3b   —  Adaptive with min probe final-token logprob
  Tier 3c   —  Adaptive with learned fusion of [entropy, surprisal, final-lp]
  Ceiling   —  Oracle adaptive                                 (upper bound)

Runtime on Colab T4:
  - If results/pilot/real_mini_raw.jsonl exists (from pilot #1): ~10-15 min
  - Otherwise regenerates from scratch: ~40-50 min

Outputs:
  results/pilot/scope_a_raw.jsonl        (per-sample signals)
  results/pilot/scope_a_summary.txt      (accuracy table)
  results/pilot/scope_a_pareto.png       (comparison plot)
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

# ── Config (mirrors pilot #1 exactly for direct comparability) ───────────────
HF_PATH = "Qwen/Qwen2.5-0.5B-Instruct"
N_QUESTIONS = 40
N_MAX = 16
K_PROBE = 4
N_GRID = [1, 2, 4, 8, 16]
TEMPERATURE = 0.7
MAX_NEW_TOKENS = 512
SEED = 42

PROMPT_TEMPLATE = (
    'Solve the following math problem step by step. '
    'Put your final numerical answer on the last line after "####".\n\n'
    "Problem: {question}"
)


def _resolve_out_dir() -> Path:
    try:
        candidate = Path(__file__).parent.parent / "results" / "pilot"
    except NameError:
        candidate = Path.cwd() / "results" / "pilot"
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


OUT_DIR = _resolve_out_dir()
PILOT1_RAW = OUT_DIR / "real_mini_raw.jsonl"
SCOPE_A_RAW = OUT_DIR / "scope_a_raw.jsonl"


# ── Answer extraction (identical to pilot #1) ────────────────────────────────

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


# ── Voting / allocator helpers ───────────────────────────────────────────────

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


# ── Sample generation (only if pilot #1 raw file is missing) ─────────────────

def load_problems(n: int, seed: int = SEED) -> list[dict]:
    from datasets import load_dataset
    ds = load_dataset("openai/gsm8k", "main", split="test")
    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(ds))[:n]
    return [
        {
            "id": f"gsm8k_{int(i)}",
            "question": ds[int(i)]["question"],
            "answer": normalize(extract_gsm8k_ref(ds[int(i)]["answer"])),
        }
        for i in idx
    ]


def generate_n_samples(tok, model, device, prompt: str, n: int) -> list[str]:
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
    return [tok.decode(seq[input_len:], skip_special_tokens=True) for seq in out]


# ── Teacher-forced scoring: recover logprobs for existing generations ────────

def score_sample(tok, model, device, prompt: str, generation: str) -> dict:
    """Teacher-forced forward pass. Returns per-token logprobs for the
    generated continuation only (not the prompt)."""
    import torch

    messages = [{"role": "user", "content": prompt}]
    formatted_prompt = tok.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    prompt_ids = tok(formatted_prompt, return_tensors="pt").input_ids.to(device)
    # Encode the generation separately and concatenate so we know the boundary.
    gen_ids = tok(generation, return_tensors="pt", add_special_tokens=False).input_ids.to(device)
    full_ids = torch.cat([prompt_ids, gen_ids], dim=1)

    with torch.no_grad():
        logits = model(full_ids).logits  # (1, seq_len, vocab)

    # logits[i] predicts token at position i+1. For generated tokens at
    # positions [prompt_len, prompt_len+gen_len-1], the predicting logits are
    # at positions [prompt_len-1, prompt_len+gen_len-2].
    prompt_len = prompt_ids.shape[1]
    gen_len = gen_ids.shape[1]
    if gen_len == 0:
        return {"token_logprobs": [], "mean_surprisal": 1.0,
                "min_logprob": 0.0, "final_token_logprob": 0.0}

    gen_logits = logits[0, prompt_len - 1 : prompt_len - 1 + gen_len, :]
    log_probs = torch.log_softmax(gen_logits.float(), dim=-1)
    gen_token_ids = gen_ids[0]
    token_logprobs = log_probs.gather(-1, gen_token_ids.unsqueeze(-1)).squeeze(-1)
    token_logprobs_list = token_logprobs.cpu().tolist()

    surprisals = [-lp for lp in token_logprobs_list]
    mean_surprisal_nats = float(np.mean(surprisals))
    min_logprob = float(min(token_logprobs_list))
    final_token_logprob = float(token_logprobs_list[-1])

    return {
        "token_logprobs": token_logprobs_list,
        "mean_surprisal": mean_surprisal_nats,
        "min_logprob": min_logprob,
        "final_token_logprob": final_token_logprob,
    }


# ── Load / rebuild the full augmented dataset ─────────────────────────────────

def load_or_generate_raw(tok=None, model=None, device=None) -> list[dict]:
    """Return list of per-question records, each with per-sample texts +
    answers (and logprobs if rebuilding). If pilot #1 raw exists, load it as-is
    (no logprobs yet — those come from scoring)."""
    if PILOT1_RAW.exists():
        print(f"Reusing pilot #1 generations from {PILOT1_RAW}")
        rows = []
        with open(PILOT1_RAW) as f:
            for line in f:
                rows.append(json.loads(line))
        if len(rows) != N_QUESTIONS:
            print(f"  (warning: expected {N_QUESTIONS} rows, found {len(rows)})")
        return rows

    print(f"No pilot #1 raw file found — regenerating from scratch")
    problems = load_problems(N_QUESTIONS)
    rows = []
    t0 = time.time()
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
        rows.append(row)
        elapsed = time.time() - t0
        eta = elapsed / (qi + 1) * (len(problems) - qi - 1)
        print(f"  gen [{qi + 1}/{len(problems)}] elapsed={elapsed:5.0f}s eta={eta:5.0f}s",
              flush=True)
    return rows


def score_all(rows: list[dict], tok, model, device) -> list[dict]:
    """Attach per-sample mean_surprisal / min_logprob / final_token_logprob
    to each row. Teacher-forced scoring, one forward pass per sample."""
    t0 = time.time()
    total = sum(len(r["samples"]) for r in rows)
    done = 0
    for qi, row in enumerate(rows):
        prompt = PROMPT_TEMPLATE.format(question=row["question"])
        per_sample = []
        for sample in row["samples"]:
            scored = score_sample(tok, model, device, prompt, sample)
            # Drop heavy token_logprobs list; keep scalar features only.
            per_sample.append({
                "mean_surprisal": scored["mean_surprisal"],
                "min_logprob": scored["min_logprob"],
                "final_token_logprob": scored["final_token_logprob"],
            })
            done += 1
        row["sample_features"] = per_sample
        elapsed = time.time() - t0
        rate = done / elapsed if elapsed > 0 else 0
        eta = (total - done) / rate if rate > 0 else 0
        print(f"  score [{qi + 1}/{len(rows)}] samples={done}/{total} "
              f"elapsed={elapsed:5.0f}s eta={eta:5.0f}s", flush=True)
    return rows


# ── Per-question signal extraction ───────────────────────────────────────────

def compute_per_question_signals(rows: list[dict], k: int) -> dict:
    """For each question, derive signals from its first k (probe) samples.

    Returns a dict mapping signal_name → list[float] across questions.
    Also returns answers[q] (list of N_MAX answers) and ground_truth[q].
    """
    n_q = len(rows)
    ent = np.zeros(n_q)
    mean_surp = np.zeros(n_q)
    min_logp = np.zeros(n_q)
    var_surp = np.zeros(n_q)
    for qi, row in enumerate(rows):
        ans = row["answers"][:k]
        ent[qi] = answer_entropy(ans)
        feats = row["sample_features"][:k]
        surp = [f["mean_surprisal"] for f in feats]
        min_lp = [f["min_logprob"] for f in feats]
        final_lp = [f["final_token_logprob"] for f in feats]
        mean_surp[qi] = float(np.mean(surp))
        # Use min across probe samples of the final-token logprob: the
        # "least confident finish" is an informative uncertainty signal.
        min_logp[qi] = float(np.min(final_lp))
        var_surp[qi] = float(np.var(surp))
    return {
        "entropy": ent,
        "mean_surprisal": mean_surp,
        "min_final_logprob": min_logp,
        "var_surprisal": var_surp,
    }


def normalize_01(x: np.ndarray) -> np.ndarray:
    """Rescale to [0, 1] so thresholds are comparable across signals."""
    lo, hi = float(x.min()), float(x.max())
    if hi <= lo:
        return np.zeros_like(x)
    return (x - lo) / (hi - lo)


# ── Adaptive method evaluation ───────────────────────────────────────────────

def sc_correct(answers: list[str], n: int, gt: str) -> bool:
    pred = majority(answers[:n])
    return pred is not None and pred == gt and pred != ""


def pareto_frontier(
    uncertainty: np.ndarray,
    rows: list[dict],
    ground_truth: list[str],
    k: int,
    n_max: int,
) -> list[tuple[float, float, float, float]]:
    """Sweep (tl, th) thresholds over normalized uncertainty; return Pareto set
    of (avg_n, accuracy, tl, th)."""
    tl_cands = np.linspace(0.0, 0.95, 20)
    th_cands = np.linspace(0.05, 1.0, 20)
    u = normalize_01(uncertainty)
    pts = []
    for tl in tl_cands:
        for th in th_cands:
            if tl >= th:
                continue
            ns = np.array([heuristic_n(float(ui), k, n_max, float(tl), float(th)) for ui in u])
            correct = sum(sc_correct(rows[q]["answers"], int(ns[q]), ground_truth[q])
                          for q in range(len(rows)))
            pts.append((float(ns.mean()), correct / len(rows), float(tl), float(th)))
    pts.sort(key=lambda x: x[0])
    pareto = []
    best = -1.0
    for pt in pts:
        if pt[1] > best:
            pareto.append(pt)
            best = pt[1]
    return pareto


# ── Fusion via logistic regression (held-out eval) ────────────────────────────

def fusion_uncertainty(
    signals: dict,
    rows: list[dict],
    ground_truth: list[str],
    k: int,
    seed: int = 0,
) -> np.ndarray:
    """Train a logistic regression on half the questions predicting
    "SC-k got the answer wrong", evaluate on the other half. Returns the
    predicted P(wrong) per question (used as the fusion uncertainty signal).
    """
    from sklearn.linear_model import LogisticRegression
    n_q = len(rows)
    # label = 1 if SC-k is wrong on this question
    labels = np.array([
        0 if sc_correct(rows[q]["answers"], k, ground_truth[q]) else 1
        for q in range(n_q)
    ])
    # features: [entropy, mean_surp_normalized, min_logp_normalized]
    feats = np.stack([
        normalize_01(signals["entropy"]),
        normalize_01(signals["mean_surprisal"]),
        normalize_01(-signals["min_final_logprob"]),   # higher = more uncertain
    ], axis=1)

    rng = np.random.default_rng(seed)
    idx = rng.permutation(n_q)
    half = n_q // 2
    tune_idx, eval_idx = idx[:half], idx[half:]

    # Predicted uncertainty for ALL questions, using splits so there's no leak:
    # - tune half: train on eval half, predict on tune half
    # - eval half: train on tune half, predict on eval half
    pred = np.zeros(n_q)
    for predict_on, train_on in [(tune_idx, eval_idx), (eval_idx, tune_idx)]:
        X_tr, y_tr = feats[train_on], labels[train_on]
        if len(set(y_tr)) < 2:
            # Degenerate: one class. Fall back to entropy.
            pred[predict_on] = normalize_01(signals["entropy"])[predict_on]
            continue
        clf = LogisticRegression(C=1.0, max_iter=1000)
        clf.fit(X_tr, y_tr)
        pred[predict_on] = clf.predict_proba(feats[predict_on])[:, 1]
    return pred


# ── Oracle ────────────────────────────────────────────────────────────────────

def oracle_result(rows: list[dict], ground_truth: list[str],
                  n_grid: list[int]) -> tuple[float, float]:
    ns = []
    correct = 0
    for q, row in enumerate(rows):
        found = None
        for n in sorted(n_grid):
            if sc_correct(row["answers"], n, ground_truth[q]):
                found = n
                break
        ns.append(found if found is not None else n_grid[-1])
        if found is not None:
            correct += 1
    return float(np.mean(ns)), correct / len(rows)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # Decide whether we need the model (for scoring or regenerating)
    need_model = True
    if PILOT1_RAW.exists() and SCOPE_A_RAW.exists():
        # If we've already scored once, we can skip the model entirely.
        need_model = False
        print(f"Loading already-scored data from {SCOPE_A_RAW}")

    tok = model = device = None
    if need_model:
        print(f"Loading {HF_PATH} ...")
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        tok = AutoTokenizer.from_pretrained(HF_PATH, trust_remote_code=True)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        if device == "cpu":
            print("WARNING: no GPU detected.  Runtime will be ~50x slower.")
        model = AutoModelForCausalLM.from_pretrained(
            HF_PATH,
            torch_dtype=torch.bfloat16 if device == "cuda" else torch.float32,
            trust_remote_code=True,
        ).to(device)
        model.eval()
        print(f"Running on: {device}")

    # Load raw generations (from pilot #1 if present, else regenerate)
    if SCOPE_A_RAW.exists():
        rows = []
        with open(SCOPE_A_RAW) as f:
            for line in f:
                rows.append(json.loads(line))
    else:
        rows = load_or_generate_raw(tok, model, device)
        # Score (teacher-forced logprobs)
        print("\nScoring samples with teacher-forced forward passes ...")
        rows = score_all(rows, tok, model, device)
        # Persist
        with open(SCOPE_A_RAW, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        print(f"Saved scored data to {SCOPE_A_RAW}")

    gts = [r["ground_truth"] for r in rows]
    signals = compute_per_question_signals(rows, k=K_PROBE)

    # ── Tier 0/1: fixed SC-N ──────────────────────────────────────────────────
    fixed_results: list[tuple[str, float, float]] = []
    for n in N_GRID:
        acc = sum(sc_correct(r["answers"], n, gts[qi]) for qi, r in enumerate(rows)) / len(rows)
        fixed_results.append((f"SC-{n}", acc, float(n)))

    # ── Tier 2: adaptive with answer-entropy (pilot #1 method) ────────────────
    pareto_ent = pareto_frontier(signals["entropy"], rows, gts, K_PROBE, N_MAX)

    # ── Tier 3a: adaptive with mean surprisal ─────────────────────────────────
    pareto_surp = pareto_frontier(signals["mean_surprisal"], rows, gts, K_PROBE, N_MAX)

    # ── Tier 3b: adaptive with min final-token logprob ────────────────────────
    # Higher min_logprob = more confident, so invert sign for "uncertainty".
    pareto_logp = pareto_frontier(-signals["min_final_logprob"], rows, gts, K_PROBE, N_MAX)

    # ── Tier 3c: adaptive with learned fusion ────────────────────────────────
    fusion_u = fusion_uncertainty(signals, rows, gts, K_PROBE, seed=0)
    pareto_fusion = pareto_frontier(fusion_u, rows, gts, K_PROBE, N_MAX)

    # ── Ceiling: oracle ──────────────────────────────────────────────────────
    oracle_n, oracle_acc = oracle_result(rows, gts, N_GRID)

    # ── Report ────────────────────────────────────────────────────────────────
    def best_at_budget(pareto, budget: float) -> tuple[float, float]:
        best = (0.0, 0.0)
        for avg_n, acc, _, _ in pareto:
            if avg_n <= budget and acc > best[1]:
                best = (avg_n, acc)
        return best

    lines = []
    lines.append(f"Scope A — Tier 3 signal comparison ({N_QUESTIONS} GSM8K, k={K_PROBE}, N_max={N_MAX})")
    lines.append("")
    lines.append(f"{'Method':<30} {'Accuracy':>10} {'Avg N':>8}")
    lines.append("-" * 52)
    for name, acc, n in fixed_results:
        lines.append(f"{name:<30} {acc:>10.3f} {n:>8.2f}")
    lines.append(f"{'oracle-adaptive':<30} {oracle_acc:>10.3f} {oracle_n:>8.2f}")
    lines.append("")
    lines.append("Adaptive methods — best point on Pareto frontier:")

    def dump(label: str, pareto: list[tuple[float, float, float, float]]):
        if not pareto:
            lines.append(f"  {label}: (no points)")
            return
        # Report best accuracy and also the "matched SC-8" point (avg_n <= 8)
        best_abs = max(pareto, key=lambda x: x[1])
        matched_8 = best_at_budget(pareto, 8.0)
        lines.append(f"  {label}")
        lines.append(f"    best:    avg_n={best_abs[0]:5.2f}  acc={best_abs[1]:.3f}  "
                     f"(tl={best_abs[2]:.2f}, th={best_abs[3]:.2f})")
        if matched_8[1] > 0:
            lines.append(f"    @N<=8:   avg_n={matched_8[0]:5.2f}  acc={matched_8[1]:.3f}")

    dump("Tier 2: answer-entropy        ", pareto_ent)
    dump("Tier 3a: mean probe surprisal ", pareto_surp)
    dump("Tier 3b: min final-token lp   ", pareto_logp)
    dump("Tier 3c: learned fusion       ", pareto_fusion)

    # Oracle-gap closure numbers (how much of the gap does each method close at N<=8)
    baseline_acc = max(a for _, a, n in fixed_results if n <= 8.0)
    gap = oracle_acc - baseline_acc
    lines.append("")
    lines.append(f"Oracle gap @ N<=8: baseline SC-4/SC-8 acc={baseline_acc:.3f}, "
                 f"oracle acc={oracle_acc:.3f}  ->  gap={gap:.3f}")

    def gap_closed(pareto):
        pt = best_at_budget(pareto, 8.0)
        if gap <= 0:
            return 0.0
        return max(0.0, (pt[1] - baseline_acc) / gap)

    lines.append(f"  Tier 2 (entropy)       closes {gap_closed(pareto_ent) * 100:5.1f}% of oracle gap")
    lines.append(f"  Tier 3a (surprisal)    closes {gap_closed(pareto_surp) * 100:5.1f}% of oracle gap")
    lines.append(f"  Tier 3b (final-lp)     closes {gap_closed(pareto_logp) * 100:5.1f}% of oracle gap")
    lines.append(f"  Tier 3c (fusion)       closes {gap_closed(pareto_fusion) * 100:5.1f}% of oracle gap")

    text = "\n".join(lines)
    print("\n" + text)
    (OUT_DIR / "scope_a_summary.txt").write_text(text + "\n")

    # ── Plot ──────────────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(9, 6))
    xs = [n for _, _, n in fixed_results]
    ys = [a for _, a, _ in fixed_results]
    ax.plot(xs, ys, "o-", color="steelblue", linewidth=2, markersize=7, label="Tier 0/1: Fixed SC-N")

    def plot_pareto(pareto, marker, color, label):
        if not pareto:
            return
        px = [p[0] for p in pareto]
        py = [p[1] for p in pareto]
        ax.plot(px, py, marker + "-", color=color, markersize=7, label=label, alpha=0.9)

    plot_pareto(pareto_ent, "s", "orange", "Tier 2: entropy")
    plot_pareto(pareto_surp, "^", "mediumseagreen", "Tier 3a: surprisal")
    plot_pareto(pareto_logp, "v", "purple", "Tier 3b: final-token logprob")
    plot_pareto(pareto_fusion, "D", "crimson", "Tier 3c: fusion (ours)")

    ax.scatter([oracle_n], [oracle_acc], marker="*", s=280, color="goldenrod",
               edgecolor="black", linewidth=0.8, zorder=6, label="Oracle (upper bound)")

    ax.set_xlabel("Average number of samples per question  (compute →)", fontsize=11)
    ax.set_ylabel("Accuracy  (↑)", fontsize=11)
    ax.set_xscale("log")
    ax.set_xticks([1, 2, 4, 8, 16])
    ax.set_xticklabels(["1", "2", "4", "8", "16"])
    ax.set_xlim(0.85, 20)
    ax.set_title(f"Scope A — Qwen2.5-0.5B on {N_QUESTIONS} GSM8K questions\n"
                 "Tier 3 intrinsic signals vs. Tier 1/2 baselines",
                 fontsize=11)
    ax.legend(loc="lower right", fontsize=9, framealpha=0.95)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    path = OUT_DIR / "scope_a_pareto.png"
    fig.savefig(path, dpi=140)
    plt.close(fig)
    print(f"\nSaved plot:    {path}")
    print(f"Saved summary: {OUT_DIR / 'scope_a_summary.txt'}")
    print(f"Saved data:    {SCOPE_A_RAW}")


if __name__ == "__main__":
    main()
