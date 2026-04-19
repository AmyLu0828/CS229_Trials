"""
Offline validation of R-ATC signals on the existing n20 data.

Tests three hypotheses, all for free (uses existing logs):

  L1. Locality: reasoning_tokens(t) autocorrelates with reasoning_tokens(t-1)
      within a trajectory. If rho >= 0.3, a lagged predictor is viable.

  L2. Respond-streak signal: respond_streak at turn t predicts
      reasoning_tokens(t).  Higher streak -> harder / more reasoning.

  L3. Error-obs signal: turns following a "API output: Error" observation
      use more reasoning tokens than turns after a successful obs.

Also answers the critical question: does a R-ATC-style *dispatched* policy
(low/medium/high assigned from the signal) give meaningful variance in
effort across turns? We simulate the dispatch decision turn-by-turn on the
recorded data.
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

HERE = Path(__file__).resolve().parent


def spearman(xs: List[float], ys: List[float]) -> float:
    # simple Spearman via ranking + Pearson
    def rank(vs):
        order = sorted(range(len(vs)), key=lambda i: vs[i])
        r = [0] * len(vs)
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
    if len(xs) < 3:
        return 0.0
    rx, ry = rank(xs), rank(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = (sum((a - mx) ** 2 for a in rx)) ** 0.5
    dy = (sum((b - my) ** 2 for b in ry)) ** 0.5
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


def parse_action_name(content: str) -> str:
    if not content or "Action:" not in content:
        return ""
    tail = content.split("Action:", 1)[-1].strip()
    try:
        j = json.loads(tail)
        return j.get("name", "") if isinstance(j, dict) else ""
    except (json.JSONDecodeError, ValueError):
        m = re.search(r'"name"\s*:\s*"([^"]+)"', tail)
        return m.group(1) if m else ""


def trajectory_features(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract per-turn features from a trajectory."""
    traj = task["traj"]
    feats: List[Dict[str, Any]] = []
    respond_streak = 0
    last_obs = ""
    prev_rt = None
    for i, m in enumerate(traj):
        role = m.get("role")
        if role == "user" and i > 0:
            last_obs = m.get("content") or ""
            continue
        if role != "assistant":
            continue
        name = parse_action_name(m.get("content") or "")
        if name == "respond":
            respond_streak += 1
        else:
            respond_streak = 0
        rt = m.get("_reasoning_tokens") or 0
        tool_error = "Error" in last_obs and "API output" in last_obs
        feats.append({
            "task_id": task["task_id"],
            "turn": len(feats),
            "name": name,
            "reasoning_tokens": rt,
            "prev_reasoning_tokens": prev_rt,
            "respond_streak": respond_streak,
            "last_obs_was_error": int(tool_error),
            "last_obs_len": len(last_obs),
            "success": int(task["reward"] == 1.0),
        })
        prev_rt = rt
    return feats


def simulate_ratc_dispatch(
    feats: List[Dict[str, Any]],
    tau_hi_rt: int = 1500,
    tau_lo_rt: int = 300,
    streak_high: int = 2,
    error_high: bool = True,
) -> List[str]:
    """Given feature dicts (in chronological order), produce the effort
    R-ATC would have chosen for each turn. The *current* turn's effort
    is decided from the *previous* turn's features (lagged)."""
    out = []
    for i, f in enumerate(feats):
        if i == 0:
            out.append("medium")
            continue
        prev = feats[i - 1]
        prev_rt = prev["reasoning_tokens"]
        prev_streak = prev["respond_streak"]
        prev_err = prev["last_obs_was_error"]
        if (prev_rt >= tau_hi_rt) or (prev_streak >= streak_high) or (error_high and prev_err):
            out.append("high")
        elif prev_rt <= tau_lo_rt and prev_streak == 0:
            out.append("low")
        else:
            out.append("medium")
    return out


def main() -> int:
    log = HERE / "logs" / "n20-o3mini-med-retail.json"
    with log.open() as f:
        data = json.load(f)

    all_feats: List[Dict[str, Any]] = []
    per_task_feats: List[List[Dict[str, Any]]] = []
    for t in data:
        feats = trajectory_features(t)
        all_feats.extend(feats)
        per_task_feats.append(feats)

    print("# R-ATC signal validation (offline, n20 baseline-medium log)\n")

    # L1: autocorrelation of reasoning_tokens
    pairs = [(f["prev_reasoning_tokens"], f["reasoning_tokens"])
             for f in all_feats if f["prev_reasoning_tokens"] is not None]
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    rho = spearman(xs, ys)
    print(f"## L1 — lagged autocorrelation of reasoning_tokens")
    print(f"- pairs: {len(pairs)}")
    print(f"- Spearman rho(rt[t-1], rt[t]): **{rho:.3f}**")
    print(f"- decision (>=0.3): {'PASS' if rho >= 0.3 else 'FAIL'}\n")

    # L2: respond_streak vs reasoning_tokens
    streaks = [f["respond_streak"] for f in all_feats]
    rts = [f["reasoning_tokens"] for f in all_feats]
    rho2 = spearman(streaks, rts)
    print(f"## L2 — respond_streak ↔ reasoning_tokens")
    print(f"- Spearman rho: **{rho2:.3f}**")
    # also: mean rt at streak >=2 vs streak ==0
    rt_at_zero = [f["reasoning_tokens"] for f in all_feats if f["respond_streak"] == 0]
    rt_at_hi = [f["reasoning_tokens"] for f in all_feats if f["respond_streak"] >= 2]
    print(f"- mean rt | streak==0: {statistics.mean(rt_at_zero) if rt_at_zero else 0:.0f} (n={len(rt_at_zero)})")
    print(f"- mean rt | streak>=2: {statistics.mean(rt_at_hi) if rt_at_hi else 0:.0f} (n={len(rt_at_hi)})")
    print()

    # L3: last_obs_was_error
    err_rts = [f["reasoning_tokens"] for f in all_feats if f["last_obs_was_error"] == 1]
    ok_rts = [f["reasoning_tokens"] for f in all_feats if f["last_obs_was_error"] == 0]
    print(f"## L3 — reasoning_tokens after error vs success")
    print(f"- mean rt after error-obs: {statistics.mean(err_rts) if err_rts else 0:.0f} (n={len(err_rts)})")
    print(f"- mean rt after normal obs: {statistics.mean(ok_rts) if ok_rts else 0:.0f} (n={len(ok_rts)})")
    print()

    # Dispatch simulation
    print("## R-ATC dispatch simulation (would-have-been efforts on this log)")
    all_efforts = []
    fail_efforts: List[str] = []
    pass_efforts: List[str] = []
    for feats in per_task_feats:
        efforts = simulate_ratc_dispatch(feats)
        all_efforts.extend(efforts)
        if feats and feats[0]["success"] == 1:
            pass_efforts.extend(efforts)
        else:
            fail_efforts.extend(efforts)
    from collections import Counter
    print(f"- total turns: {len(all_efforts)}")
    print(f"- dispatched effort distribution: {dict(Counter(all_efforts))}")
    print(f"- on failing tasks:  {dict(Counter(fail_efforts))}")
    print(f"- on passing tasks:  {dict(Counter(pass_efforts))}")
    print()

    # Compute saving estimate: low=~500 rt target, med=~1500, high=~3500
    EFFORT_RT = {"low": 500, "medium": 1500, "high": 3500}
    sim_rt = sum(EFFORT_RT[e] for e in all_efforts)
    baseline_rt = len(all_efforts) * EFFORT_RT["medium"]
    actual_rt = sum(f["reasoning_tokens"] for f in all_feats)
    print(f"## Rough compute comparison (based on mean-rt-per-tier targets)")
    print(f"- actual baseline (as logged): {actual_rt:,} reasoning tokens")
    print(f"- fixed-medium (theoretical):  {baseline_rt:,}")
    print(f"- R-ATC dispatched (theoretical): {sim_rt:,}")
    print(f"- R-ATC vs fixed-medium ratio: {sim_rt / max(1, baseline_rt):.2f}x")

    # Effort variance within-task (H3 proxy)
    print("\n## Within-task effort variance (H3 proxy)")
    variances = []
    for feats in per_task_feats:
        efforts = simulate_ratc_dispatch(feats)
        if len(efforts) < 2:
            continue
        # number of distinct effort tiers used
        variances.append(len(set(efforts)))
    print(f"- mean distinct effort tiers per task: {sum(variances)/max(1,len(variances)):.2f}")
    print(f"- tasks using >=2 tiers: {sum(1 for v in variances if v>=2)}/{len(variances)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
