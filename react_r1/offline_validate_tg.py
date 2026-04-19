"""
Offline validation of TG detectors on an existing run log.

Replays each trajectory turn-by-turn. At each turn t we (1) reconstruct the
prefix as the agent would have seen it just before the action at turn t, and
(2) run all detectors with the action at turn t as the 'proposed_action'.

For H5 (detectors fire on most failed trajectories in hindsight):
  - fraction of failed tasks with >=1 detector fire before the final turn
  - distribution of first-fire turn index (relative + absolute)
  - false-positive rate on successful tasks
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent

import sys  # noqa: E402
sys.path.insert(0, str(HERE))

from tg_detectors import (  # noqa: E402
    parse_action,
    run_detectors,
    _assistant_turns,
)

# Retail mutating action names (used only as a tighter D2 check; the detectors
# also work with the heuristic fallback when this is None).
RETAIL_MUTATING = {
    "cancel_pending_order",
    "exchange_delivered_order_items",
    "modify_pending_order_address",
    "modify_pending_order_items",
    "modify_pending_order_payment",
    "modify_user_address",
    "return_delivered_order_items",
    "transfer_to_human_agents",
}


def replay_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """For each assistant turn, run the detectors on the prefix and the
    action at that turn, recording which (if any) detector fires and when.
    """
    traj = task["traj"]
    asst_indices = [i for i, m in enumerate(traj) if m.get("role") == "assistant"]
    n_turns = len(asst_indices)

    first_fire_idx = None  # 0-indexed turn number at which any detector fires
    fires_per_turn: List[List[str]] = []

    for turn_i, msg_idx in enumerate(asst_indices):
        prefix = traj[:msg_idx]
        proposed = parse_action(traj[msg_idx].get("content") or "")
        # For D2 to apply at turn t, the prefix must NOT include the proposed
        # action yet (it's "about to be executed"); since prefix = traj[:msg_idx],
        # the proposed action has not yet been appended. Good.
        # But D1/D3 look at the history of actions already committed — which is
        # exactly `prefix`. Also good.
        fires = run_detectors(
            traj_prefix=prefix,
            proposed_action=proposed,
            mutating_names=RETAIL_MUTATING,
        )
        fire_ids = [f["id"] for f in fires]
        fires_per_turn.append(fire_ids)
        if fire_ids and first_fire_idx is None:
            first_fire_idx = turn_i

    return {
        "task_id": task["task_id"],
        "success": int(task["reward"] == 1.0),
        "n_turns": n_turns,
        "first_fire_idx": first_fire_idx,  # None = never fired
        "fires_per_turn": fires_per_turn,
        "total_fires": sum(len(f) for f in fires_per_turn),
        "fired_before_final": (
            first_fire_idx is not None and first_fire_idx < n_turns - 1
        ),
    }


def summarize(replays: List[Dict[str, Any]]) -> str:
    failed = [r for r in replays if r["success"] == 0]
    passed = [r for r in replays if r["success"] == 1]

    lines: List[str] = []
    lines.append("# Offline TG-detector validation\n")
    lines.append(f"- total tasks: {len(replays)}")
    lines.append(f"- failed: {len(failed)}  |  passed: {len(passed)}\n")

    # H5: coverage of failures
    failed_with_fire = [r for r in failed if r["first_fire_idx"] is not None]
    failed_fired_before_final = [r for r in failed if r["fired_before_final"]]
    cov_any = len(failed_with_fire) / max(1, len(failed))
    cov_pre_final = len(failed_fired_before_final) / max(1, len(failed))
    lines.append("## H5 — coverage on failed trajectories")
    lines.append(
        f"- fraction of failures with ≥1 detector fire (any turn): "
        f"**{cov_any:.1%}** ({len(failed_with_fire)}/{len(failed)})"
    )
    lines.append(
        f"- fraction firing **before** the final turn (actionable online): "
        f"**{cov_pre_final:.1%}** "
        f"({len(failed_fired_before_final)}/{len(failed)})"
    )
    lines.append(
        f"- **H5 decision gate (≥60% pre-final): "
        f"{'PASS' if cov_pre_final >= 0.60 else 'FAIL'}**\n"
    )

    # First-fire distribution on failures
    if failed_with_fire:
        idxs = [r["first_fire_idx"] for r in failed_with_fire]
        rels = [r["first_fire_idx"] / max(1, r["n_turns"] - 1)
                for r in failed_with_fire]
        lines.append("## First-fire turn index on failures")
        lines.append(
            f"- absolute turn idx: min={min(idxs)}  "
            f"med={statistics.median(idxs):.1f}  max={max(idxs)}"
        )
        lines.append(
            f"- relative position in trajectory: min={min(rels):.2f}  "
            f"med={statistics.median(rels):.2f}  max={max(rels):.2f}"
        )
        lines.append("")

    # False-positive rate on passes
    passed_with_fire = [r for r in passed if r["first_fire_idx"] is not None]
    fpr = len(passed_with_fire) / max(1, len(passed))
    lines.append("## False-positive rate on successful trajectories")
    lines.append(f"- fraction with any detector firing: **{fpr:.1%}** "
                 f"({len(passed_with_fire)}/{len(passed)})")
    lines.append("- (ideal: low. High FPR means recoveries would disrupt "
                 "successful runs.)\n")

    # Detector-level breakdown
    fire_counter = Counter()
    for r in replays:
        for ids in r["fires_per_turn"]:
            fire_counter.update(ids)
    lines.append("## Detector fire counts (all tasks)")
    lines.append("| Detector | fires |")
    lines.append("|---|---|")
    for d in ["D1", "D2", "D3"]:
        lines.append(f"| {d} | {fire_counter.get(d, 0)} |")
    lines.append("")

    # By success/failure, which detector fires first
    lines.append("## First-firing detector on failures (one per failed task)")
    first_on_failed = Counter()
    for r in failed:
        if r["first_fire_idx"] is None:
            first_on_failed["(none)"] += 1
            continue
        first_on_failed[r["fires_per_turn"][r["first_fire_idx"]][0]] += 1
    for k, v in first_on_failed.most_common():
        lines.append(f"- {k}: {v}")
    lines.append("")

    # Per-task detail
    lines.append("## Per-task replay")
    lines.append("| task | succ | turns | first fire (turn / id) | total fires |")
    lines.append("|---|---|---|---|---|")
    for r in sorted(replays, key=lambda r: r["task_id"]):
        ff = r["first_fire_idx"]
        ff_str = (f"{ff} / {r['fires_per_turn'][ff][0]}"
                  if ff is not None else "—")
        lines.append(
            f"| {r['task_id']} | {'✓' if r['success'] else '✗'} | "
            f"{r['n_turns']} | {ff_str} | {r['total_fires']} |"
        )
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("log_file", type=Path,
                   default=HERE / "logs" / "n20-o3mini-med-retail.json",
                   nargs="?")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    with args.log_file.open() as f:
        data = json.load(f)

    replays = [replay_task(t) for t in data]
    report = summarize(replays)
    out_path = args.out or (HERE / "analysis" / args.log_file.stem /
                             "tg_offline_validation.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    print(report)
    print(f"\n(saved to {out_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
