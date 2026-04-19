"""Sensitivity sweep over D1 parameters (k, sim_threshold) to find a setting
with high coverage on failures and low FPR on passes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from tg_detectors import (  # noqa: E402
    _action_sequence,
    _respond_content,
    _ngram_jaccard,
    detect_D2_premature_escalation,
    detect_D3_action_repetition,
    parse_action,
    RESPOND_ACTIONS,
)


def d1_with_sim(actions, k: int, sim_thr: float):
    if len(actions) < k:
        return None
    tail = actions[-k:]
    if any(a.get("name") not in RESPOND_ACTIONS for a in tail):
        return None
    # require paraphrase similarity >= sim_thr between any two of the last k
    contents = [_respond_content(a) for a in tail]
    max_sim = 0.0
    for i in range(len(contents)):
        for j in range(i + 1, len(contents)):
            s = _ngram_jaccard(contents[i], contents[j])
            if s > max_sim:
                max_sim = s
    if max_sim < sim_thr:
        return None
    return {"id": "D1", "reason": "respond_loop", "max_sim": round(max_sim, 3)}


def replay(task, k: int, sim_thr: float):
    traj = task["traj"]
    asst_indices = [i for i, m in enumerate(traj) if m.get("role") == "assistant"]
    n_turns = len(asst_indices)
    first = None
    for turn_i, msg_idx in enumerate(asst_indices):
        prefix = traj[:msg_idx]
        acts = _action_sequence(prefix)
        proposed = parse_action(traj[msg_idx].get("content") or "")
        fires = []
        if d1_with_sim(acts, k, sim_thr):
            fires.append("D1")
        if detect_D2_premature_escalation(acts, proposed,
                                          mutating_names=None):
            fires.append("D2")
        if detect_D3_action_repetition(acts):
            fires.append("D3")
        if fires and first is None:
            first = turn_i
    return first is not None and first < n_turns - 1, first is not None


def main():
    log = HERE / "logs" / "n20-o3mini-med-retail.json"
    with log.open() as f:
        data = json.load(f)

    failed = [t for t in data if t["reward"] != 1.0]
    passed = [t for t in data if t["reward"] == 1.0]

    print(f"{'k':>3} {'sim':>5} | cov-pre-final  FPR-any  | (|fail|={len(failed)}, |pass|={len(passed)})")
    print("-" * 70)
    for k in [3, 4, 5]:
        for sim in [0.0, 0.2, 0.3, 0.4, 0.5]:
            cov = sum(replay(t, k, sim)[0] for t in failed) / max(1, len(failed))
            fpr = sum(replay(t, k, sim)[1] for t in passed) / max(1, len(passed))
            flag = "  *" if cov >= 0.60 and fpr <= 0.30 else ""
            print(f"{k:>3} {sim:>5.2f} | {cov:>11.1%}   {fpr:>6.1%}{flag}")


if __name__ == "__main__":
    main()
