"""Threshold sweep for R-ATC. Can we find a dispatch policy that
spends <= fixed-medium compute while still putting 'high' effort on
enough failing-task turns to matter?
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from validate_ratc_signals import trajectory_features  # noqa


EFFORT_RT_TARGET = {"low": 250, "medium": 500, "high": 1500}


def dispatch(feats, tau_hi_rt, tau_lo_rt, streak_high):
    out = []
    for i, f in enumerate(feats):
        if i == 0:
            out.append("medium")
            continue
        prev = feats[i - 1]
        hi_hit = (prev["reasoning_tokens"] >= tau_hi_rt
                  and prev["respond_streak"] >= streak_high)
        lo_hit = (prev["reasoning_tokens"] <= tau_lo_rt
                  and prev["respond_streak"] == 0)
        if hi_hit:
            out.append("high")
        elif lo_hit:
            out.append("low")
        else:
            out.append("medium")
    return out


def main():
    log = HERE / "logs" / "n20-o3mini-med-retail.json"
    with log.open() as f:
        data = json.load(f)
    per_task = [trajectory_features(t) for t in data]

    print(f"{'tau_hi':>6} {'tau_lo':>6} {'sk_hi':>5} |"
          f" {'low%':>5} {'med%':>5} {'high%':>5} |"
          f" {'ratio_vs_med':>12} | hits on failing tasks")
    print("-" * 95)

    fail_tasks = [f for f, t in zip(per_task, data) if t["reward"] != 1.0]
    pass_tasks = [f for f, t in zip(per_task, data) if t["reward"] == 1.0]
    n_total = sum(len(f) for f in per_task)

    for tau_hi in [1500, 2000, 2500, 3000]:
        for tau_lo in [100, 200, 300]:
            for streak_high in [2, 3]:
                all_eff = []
                fail_eff = []
                for feats in per_task:
                    e = dispatch(feats, tau_hi, tau_lo, streak_high)
                    all_eff.extend(e)
                for feats in fail_tasks:
                    fail_eff.extend(dispatch(feats, tau_hi, tau_lo, streak_high))
                c = Counter(all_eff)
                theo = sum(EFFORT_RT_TARGET[e] for e in all_eff)
                fixed_med = EFFORT_RT_TARGET["medium"] * n_total
                ratio = theo / fixed_med
                fc = Counter(fail_eff)
                print(f"{tau_hi:>6} {tau_lo:>6} {streak_high:>5} |"
                      f" {100*c['low']/n_total:>4.0f}% {100*c['medium']/n_total:>4.0f}%"
                      f" {100*c['high']/n_total:>4.0f}% |"
                      f" {ratio:>12.2f} |"
                      f" {fc['high']} high / {fc['medium']+fc['low']+fc['high']} fail turns"
                      f"  (= {100*fc['high']/max(1,sum(fc.values())):.0f}%)")


if __name__ == "__main__":
    main()
