"""
Compute ~20 zero-cost uncertainty signals per turn on the n20 log and
measure their predictive power for:

  (A) next-turn reasoning tokens  -> turn-level difficulty
  (B) eventual task failure       -> trajectory-level drift

All signals derivable from (a) prefix trajectory state, (b) the just-
produced assistant message, or (c) the original task instruction. No
new API calls.

Outputs:
  analysis/n20-o3mini-med-retail/signals_ranked.md
  analysis/n20-o3mini-med-retail/signals_per_turn.csv
"""

from __future__ import annotations

import csv
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from tg_detectors import parse_action, _args_hash  # noqa: E402

LOG = HERE / "logs" / "n20-o3mini-med-retail.json"
OUT_DIR = HERE / "analysis" / "n20-o3mini-med-retail"
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ---------- Linguistic lexica ----------

HEDGE = re.compile(
    r"\b(?:"
    r"maybe|perhaps|probably|possibly|might|may|could|should|seems|appears|"
    r"i think|i believe|i'll try|let me try|let me first|first i need|"
    r"i'm not sure|not entirely sure|unclear|ambiguous|"
    r"need to check|need to verify|let me check|i need more|"
    r"it's possible|it looks like|if i'm right|i suspect"
    r")\b",
    re.IGNORECASE,
)

ALT_MARKERS = re.compile(
    r"\b(?:either|or|alternatively|otherwise|on the other hand|"
    r"on one hand|two options|two choices|it depends|depending on)\b",
    re.IGNORECASE,
)

USER_FRUSTRATION = re.compile(
    r"\b(?:no|wrong|incorrect|not what|still|again|didn'?t|haven'?t|"
    r"you haven'?t|why|why not|please just|i already|i said|"
    r"frustrated|never mind|forget it)\b",
    re.IGNORECASE,
)

COND_WORDS = re.compile(r"\b(?:if|unless|only when|only if|otherwise)\b",
                        re.IGNORECASE)
ALT_WORDS = re.compile(r"\b(?:or|either|alternatively)\b", re.IGNORECASE)


# ---------- Helpers ----------


def extract_thought(content: str) -> str:
    if not content:
        return ""
    if "Thought:" in content and "Action:" in content:
        return content.split("Thought:", 1)[1].split("Action:", 1)[0]
    if "Thought:" in content:
        return content.split("Thought:", 1)[1]
    return content.split("Action:", 1)[0] if "Action:" in content else content


def respond_content(parsed: Optional[Dict[str, Any]]) -> str:
    if not parsed:
        return ""
    args = parsed.get("arguments") or {}
    if isinstance(args, dict):
        return str(args.get("content") or "")
    return ""


def char_ngrams(s: str, n: int = 5) -> set:
    s = s.lower().strip()
    return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}


# ---------- Feature extraction per task ----------


def extract_features(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Produce one feature dict per assistant turn in the trajectory."""
    traj = task["traj"]
    success = int(task["reward"] == 1.0)

    # Find the user instruction (first user message after system).
    instruction = ""
    for m in traj:
        if m.get("role") == "user":
            instruction = m.get("content") or ""
            break

    n_cond = len(COND_WORDS.findall(instruction))
    n_alt = len(ALT_WORDS.findall(instruction))
    instr_len = len(instruction)

    # Walk the trajectory, tracking state.
    feats: List[Dict[str, Any]] = []
    respond_streak = 0
    tool_errors = 0
    mutating_attempted = 0
    tools_used: set = set()
    action_hashes: List[tuple] = []
    recent_user_msgs: List[str] = []
    prev_assistant_msg: Optional[Dict[str, Any]] = None
    last_obs = ""

    asst_turn_idx = -1
    max_turns_est = max(1, sum(1 for m in traj if m.get("role") == "assistant"))

    for i, m in enumerate(traj):
        role = m.get("role")
        if role == "user":
            if i == 0:
                continue  # should not happen; skip any stray
            content = m.get("content") or ""
            last_obs = content
            if not content.startswith("API output:"):
                recent_user_msgs.append(content)
                if len(recent_user_msgs) > 3:
                    recent_user_msgs.pop(0)
            continue
        if role != "assistant":
            continue

        asst_turn_idx += 1
        parsed = parse_action(m.get("content") or "")
        name = parsed.get("name") if parsed else ""
        thought = extract_thought(m.get("content") or "")
        rt = m.get("_reasoning_tokens") or 0
        ct = m.get("_completion_tokens") or 0

        # --- state-based signals, captured BEFORE updating with this turn ---
        s_respond_streak = respond_streak
        s_tool_errors = tool_errors
        s_mutating_attempted = mutating_attempted
        s_distinct_tools = len(tools_used)
        s_turns_ratio = asst_turn_idx / 30.0  # vs max_num_steps

        # action repetition count: how often did THIS (name, args_hash) appear before?
        this_hash = (name, _args_hash(parsed)) if parsed else ("", "")
        s_action_repetition = action_hashes.count(this_hash)

        # info-gain velocity: 5-gram Jaccard between most recent two user msgs
        if len(recent_user_msgs) >= 2:
            g1 = char_ngrams(recent_user_msgs[-1])
            g2 = char_ngrams(recent_user_msgs[-2])
            j = len(g1 & g2) / len(g1 | g2) if (g1 | g2) else 0.0
            s_info_overlap = j   # high = looping / no new info
        else:
            s_info_overlap = 0.0

        # --- last-observation signals ---
        s_last_obs_len = len(last_obs)
        s_error_kw = int(bool(re.search(r"\b(error|invalid|not found|failed)\b",
                                         last_obs, re.IGNORECASE)))

        # --- user-behavior signals (from recent user/observation messages) ---
        concat_user = " ".join(recent_user_msgs[-2:])
        s_user_frustration = len(USER_FRUSTRATION.findall(concat_user))
        s_user_clarif = int("?" in concat_user)

        # --- instruction-level (constant per task) ---
        s_instr_len = instr_len
        s_cond = n_cond
        s_alt = n_alt

        # --- linguistic signals from THIS turn's thought (used as post-hoc signal
        # for PREDICTING the next turn; so we store them on this row and shift
        # when building the prediction target) ---
        s_hedge = len(HEDGE.findall(thought))
        s_alt_markers = len(ALT_MARKERS.findall(thought))
        # question density in respond content
        s_question_density = 0.0
        if name == "respond":
            rc = respond_content(parsed)
            if rc:
                s_question_density = rc.count("?") / max(1, len(rc.split()))

        # prev-turn compute signals (already-committed turn's stats)
        s_rt_prev = prev_assistant_msg.get("_reasoning_tokens") if prev_assistant_msg else None
        s_ct_prev = prev_assistant_msg.get("_completion_tokens") if prev_assistant_msg else None
        if s_rt_prev is not None and s_ct_prev is not None and s_ct_prev > 0:
            s_rt_over_ct_prev = s_rt_prev / s_ct_prev
        else:
            s_rt_over_ct_prev = 0.0

        feats.append({
            "task_id": task["task_id"],
            "turn": asst_turn_idx,
            "name": name or "",
            "reasoning_tokens": rt,
            "completion_tokens": ct,
            "success": success,
            # state signals
            "S1_respond_streak": s_respond_streak,
            "S2_action_repetition": s_action_repetition,
            "S3_turns_ratio": s_turns_ratio,
            "S4_tool_errors": s_tool_errors,
            "S5_mutating_attempted": s_mutating_attempted,
            "S6_distinct_tools": s_distinct_tools,
            "S7_info_overlap": s_info_overlap,
            # obs signals
            "S8_last_obs_len": s_last_obs_len,
            "S12_error_kw": s_error_kw,
            "S10_user_frustration": s_user_frustration,
            "S11_user_clarif": s_user_clarif,
            # instruction signals (constant per task; included for completeness)
            "S13_instr_len": s_instr_len,
            "S14_cond_count": s_cond,
            "S15_alt_count": s_alt,
            # current-turn linguistic signals (shift for next-turn prediction)
            "S17_hedge": s_hedge,
            "S18_alt_markers": s_alt_markers,
            "S19_question_density": s_question_density,
            # prev-turn compute signals
            "S16_rt_prev": s_rt_prev,
            "S20_ct_prev": s_ct_prev,
            "S21_rt_over_ct_prev": s_rt_over_ct_prev,
        })

        # --- update state for next iteration ---
        if name == "respond":
            respond_streak += 1
        else:
            respond_streak = 0
        if name and name not in ("", "respond"):
            tools_used.add(name)
            # heuristic: any non-respond, non-lookup -> mutating
            if not (name.startswith("get_") or name.startswith("find_")
                    or name.startswith("list_")):
                mutating_attempted = 1
        if "API output: Error" in last_obs or "Error:" in last_obs:
            tool_errors += 1
        action_hashes.append(this_hash)
        prev_assistant_msg = m

    return feats


# ---------- Statistics ----------


def _rank(vs: List[float]) -> List[float]:
    order = sorted(range(len(vs)), key=lambda i: vs[i])
    r = [0.0] * len(vs)
    i = 0
    while i < len(vs):
        j = i
        while j + 1 < len(vs) and vs[order[j + 1]] == vs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def spearman(xs: List[float], ys: List[float]) -> float:
    xs = [float(x) if x is not None else 0.0 for x in xs]
    ys = [float(y) if y is not None else 0.0 for y in ys]
    if len(xs) < 3:
        return 0.0
    rx, ry = _rank(xs), _rank(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = (sum((a - mx) ** 2 for a in rx)) ** 0.5
    dy = (sum((b - my) ** 2 for b in ry)) ** 0.5
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


def point_biserial(binary: List[int], xs: List[float]) -> float:
    xs = [float(x) if x is not None else 0.0 for x in xs]
    n = len(xs)
    if n < 3:
        return 0.0
    g1 = [x for x, b in zip(xs, binary) if b == 1]
    g0 = [x for x, b in zip(xs, binary) if b == 0]
    n1, n0 = len(g1), len(g0)
    if n1 < 2 or n0 < 2:
        return 0.0
    m1, m0 = sum(g1) / n1, sum(g0) / n0
    sd = statistics.pstdev(xs)
    if sd == 0:
        return 0.0
    return (m1 - m0) / sd * ((n1 * n0 / (n * n)) ** 0.5)


# ---------- Simple logistic combo (hand-fit, no training) ----------


def _logistic(z: float) -> float:
    import math
    return 1.0 / (1.0 + math.exp(-z))


def fit_logistic(X: List[List[float]], y: List[int],
                 n_iter: int = 400, lr: float = 0.05,
                 l2: float = 0.01) -> List[float]:
    """Minimal batch-GD logistic regression with z-score normalization
    done inside. Returns weight vector incl. bias at index 0.
    """
    import math
    if not X:
        return []
    d = len(X[0])
    # z-score per column
    means = [sum(col) / len(col) for col in zip(*X)]
    sds = [max(1e-6, statistics.pstdev(col)) for col in zip(*X)]
    Xn = [[(row[j] - means[j]) / sds[j] for j in range(d)] for row in X]
    w = [0.0] * (d + 1)
    for _ in range(n_iter):
        g = [0.0] * (d + 1)
        for xi, yi in zip(Xn, y):
            z = w[0] + sum(w[j + 1] * xi[j] for j in range(d))
            p = _logistic(z)
            err = p - yi
            g[0] += err
            for j in range(d):
                g[j + 1] += err * xi[j]
        n = len(Xn)
        w = [w[k] - lr * (g[k] / n + l2 * w[k]) for k in range(d + 1)]
    # attach normalization for inference
    return [w, means, sds]


def predict_logistic(model, x: List[float]) -> float:
    w, means, sds = model
    d = len(x)
    xn = [(x[j] - means[j]) / max(1e-6, sds[j]) for j in range(d)]
    z = w[0] + sum(w[j + 1] * xn[j] for j in range(d))
    return _logistic(z)


# ---------- Main ----------


def main() -> int:
    with LOG.open() as f:
        data = json.load(f)

    all_feats: List[Dict[str, Any]] = []
    by_task: Dict[int, List[Dict[str, Any]]] = {}
    for t in data:
        feats = extract_features(t)
        by_task[t["task_id"]] = feats
        all_feats.extend(feats)

    # CSV dump
    csv_path = OUT_DIR / "signals_per_turn.csv"
    if all_feats:
        with csv_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(all_feats[0].keys()))
            w.writeheader()
            for r in all_feats:
                w.writerow(r)

    signal_cols = [c for c in all_feats[0].keys() if c.startswith("S")]

    # Build targets: next-turn rt is the rt of the next row within the SAME task.
    # Trajectory-level failure: 1 if task failed, else 0.
    pairs_rt_next: Dict[str, List[tuple]] = {s: [] for s in signal_cols}
    pairs_fail: Dict[str, List[tuple]] = {s: [] for s in signal_cols}
    for task_id, feats in by_task.items():
        for i in range(len(feats) - 1):
            nxt_rt = feats[i + 1]["reasoning_tokens"]
            for s in signal_cols:
                v = feats[i][s]
                if v is None:
                    continue
                pairs_rt_next[s].append((v, nxt_rt))
        # trajectory failure target on every turn
        fail = 1 - feats[0]["success"] if feats else 0
        for row in feats:
            for s in signal_cols:
                v = row[s]
                if v is None:
                    continue
                pairs_fail[s].append((v, fail))

    # Compute Spearman + point-biserial for each signal.
    rows = []
    for s in signal_cols:
        rt_pairs = pairs_rt_next[s]
        rho = spearman([p[0] for p in rt_pairs], [p[1] for p in rt_pairs]) if rt_pairs else 0.0
        fp = pairs_fail[s]
        pbc = point_biserial([int(b) for _, b in fp], [p[0] for p in fp]) if fp else 0.0
        rows.append((s, rho, pbc, abs(rho) + abs(pbc)))
    rows.sort(key=lambda r: r[3], reverse=True)

    # Pick top-5 signals by combined abs score; fit leave-one-task-out
    # logistic for predicting trajectory failure using per-turn features.
    top_signals = [r[0] for r in rows[:5]]
    tasks = list(by_task.keys())
    loo_scores = []
    for held in tasks:
        Xtr, ytr, Xte, yte = [], [], [], []
        for t, feats in by_task.items():
            for row in feats:
                x = [float(row[s] or 0.0) for s in top_signals]
                y = 1 - row["success"]
                if t == held:
                    Xte.append(x); yte.append(y)
                else:
                    Xtr.append(x); ytr.append(y)
        model = fit_logistic(Xtr, ytr)
        preds = [predict_logistic(model, x) for x in Xte]
        # report mean predicted p(fail) on held-out task
        if preds:
            loo_scores.append((held, yte[0] if yte else None,
                               sum(preds) / len(preds)))

    # Simple AUC on LOO predictions (per-task fail label, mean-prob per task)
    from statistics import mean
    y_task = [s[1] for s in loo_scores]
    p_task = [s[2] for s in loo_scores]
    # AUC via Mann-Whitney
    def auc(y, p):
        pos = [pi for pi, yi in zip(p, y) if yi == 1]
        neg = [pi for pi, yi in zip(p, y) if yi == 0]
        if not pos or not neg:
            return 0.5
        wins = 0
        total = 0
        for a in pos:
            for b in neg:
                total += 1
                if a > b: wins += 1
                elif a == b: wins += 0.5
        return wins / total
    task_auc = auc(y_task, p_task)

    # Write markdown report
    md = OUT_DIR / "signals_ranked.md"
    lines = []
    lines.append("# Zero-cost uncertainty signals — ranked\n")
    lines.append("Signals computed on the existing n20 baseline-medium log. "
                 "No new API calls.\n")
    lines.append(f"- Total assistant turns: {len(all_feats)}")
    lines.append(f"- Failed tasks: {sum(1 for t in data if t['reward']!=1)}/{len(data)}\n")
    lines.append("## Per-signal predictive power\n")
    lines.append("- `rho(S, rt_next)`: Spearman vs NEXT turn's reasoning tokens "
                 "(turn-level difficulty)")
    lines.append("- `pb(S, fail)`: point-biserial vs trajectory failure "
                 "(drift-level signal)\n")
    lines.append("| rank | signal | ρ(S, rt_next) | pb(S, fail) | combined |")
    lines.append("|---|---|---|---|---|")
    for i, (s, rho, pbc, comb) in enumerate(rows, 1):
        lines.append(f"| {i} | `{s}` | {rho:+.3f} | {pbc:+.3f} | {comb:.3f} |")
    lines.append("")
    lines.append(f"## Leave-one-task-out logistic (top-5 signals: {top_signals})\n")
    lines.append(f"- per-task failure-prediction AUC: **{task_auc:.3f}**")
    lines.append("  - 0.5 = random; >=0.70 = useful; >=0.80 = strong")
    lines.append("")
    lines.append("## Interpretation guide\n")
    lines.append("- |ρ| ≥ 0.25 → usable turn-level difficulty signal\n"
                 "- |pb| ≥ 0.20 → usable trajectory-level drift signal\n"
                 "- combined ≥ 0.40 → good dual-purpose signal")

    md.write_text("\n".join(lines))
    print("\n".join(lines))
    print(f"\n(saved to {md})")
    print(f"(per-turn CSV: {csv_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
