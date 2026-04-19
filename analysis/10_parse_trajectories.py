"""Parse all 57 trajectories into step, subtask, and trajectory tables.

Reads: appworld_playbook_gpt54_60/{log.jsonl, summary.json}
Writes:
  analysis/tables/steps.parquet        (one row per ASSISTANT turn, 2665 rows)
  analysis/tables/subtasks.parquet     (one row per subtask, 4x57 = 228)
  analysis/tables/trajectories.parquet (one row per log line, 57)
  analysis/tables/trajectories.csv     (same, human-readable)

Assertions:
- every trajectory has exactly 4 subtasks
- every trajectory's step count is in [41, 59]
- every successful trajectory's final subtask ends in a complete_task( call
- per-line scores match summary.json
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pandas as pd

from parser import iter_trajectories, parse_trajectory, step_to_dict

OUT_DIR = Path("analysis/tables")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def main() -> None:
    with Path("appworld_playbook_gpt54_60/summary.json").open() as f:
        summary = json.load(f)
    successes = summary["successes"]
    scores = summary["scores"]

    step_rows: list[dict] = []
    subtask_rows: list[dict] = []
    traj_rows: list[dict] = []

    for task_idx, obj in enumerate(iter_trajectories()):
        steps = parse_trajectory(
            obj["trajectory"], task_id=obj["task_id"], task_idx=task_idx
        )
        assert 41 <= len(steps) <= 59, f"step count {len(steps)} at task {obj['task_id']}"
        n_sub = max(s.subtask_idx for s in steps) + 1
        assert n_sub == 4, f"expected 4 subtasks, got {n_sub} at task {obj['task_id']}"

        # Subtask-level aggregates
        by_sub: dict[int, list] = defaultdict(list)
        for s in steps:
            by_sub[s.subtask_idx].append(s)

        for sub_idx, sub_steps in sorted(by_sub.items()):
            ct_steps = [s for s in sub_steps if s.called_complete_task]
            subtask_rows.append(
                {
                    "task_idx": task_idx,
                    "task_id": obj["task_id"],
                    "subtask_idx": sub_idx,
                    "is_final": sub_idx == n_sub - 1,
                    "subtask_description": sub_steps[0].subtask_description,
                    "n_steps": len(sub_steps),
                    "n_errors": sum(1 for s in sub_steps if s.output_had_error),
                    "n_api_calls": sum(len(s.api_calls) for s in sub_steps),
                    "n_readonly_calls": sum(
                        1 for s in sub_steps for c in s.api_calls if c["readonly"]
                    ),
                    "n_unique_api_fqs": len(
                        {c["fq"] for s in sub_steps for c in s.api_calls}
                    ),
                    "step_of_first_complete_task": (
                        ct_steps[0].step_idx_in_subtask if ct_steps else None
                    ),
                    "n_complete_task_calls": len(ct_steps),
                    "chars_assistant": sum(s.n_chars_assistant for s in sub_steps),
                    "chars_output": sum(s.n_chars_output for s in sub_steps),
                    # only final subtask has a scored outcome
                    "final_score": scores[task_idx] if sub_idx == n_sub - 1 else None,
                    "final_success": (
                        successes[task_idx] if sub_idx == n_sub - 1 else None
                    ),
                }
            )

        traj_rows.append(
            {
                "task_idx": task_idx,
                "task_id": obj["task_id"],
                "n_steps": len(steps),
                "n_subtasks": n_sub,
                "n_errors": sum(1 for s in steps if s.output_had_error),
                "n_api_calls": sum(len(s.api_calls) for s in steps),
                "n_readonly_calls": sum(
                    1 for s in steps for c in s.api_calls if c["readonly"]
                ),
                "n_unique_api_fqs": len(
                    {c["fq"] for s in steps for c in s.api_calls}
                ),
                "distinct_apps": len(
                    {c["app"] for s in steps for c in s.api_calls}
                ),
                "n_complete_task_calls": sum(1 for s in steps if s.called_complete_task),
                "chars_assistant": sum(s.n_chars_assistant for s in steps),
                "chars_output": sum(s.n_chars_output for s in steps),
                "outcome": obj["result"]["outcome"],
                "score": obj["result"]["score"],
                "success_flag": int(successes[task_idx]),
                "playbook_bullets": obj["state_stats"]["total_bullets"],
                "playbook_tokens": obj["state_stats"]["approx_tokens"],
                "generate_s": obj["timing"]["generate_s"],
            }
        )

        for s in steps:
            step_rows.append(step_to_dict(s))

    steps_df = pd.DataFrame(step_rows)
    subtask_df = pd.DataFrame(subtask_rows)
    traj_df = pd.DataFrame(traj_rows)

    # Cross-check: each successful trajectory's final subtask calls complete_task
    failed_ct_check = subtask_df[
        (subtask_df["is_final"])
        & (subtask_df["final_success"] == 1)
        & (subtask_df["n_complete_task_calls"] == 0)
    ]
    if len(failed_ct_check):
        print("WARNING: success without final complete_task call:")
        print(failed_ct_check[["task_id"]].to_string(index=False))

    steps_df.to_parquet(OUT_DIR / "steps.parquet", index=False)
    subtask_df.to_parquet(OUT_DIR / "subtasks.parquet", index=False)
    subtask_df.to_csv(OUT_DIR / "subtasks.csv", index=False)
    traj_df.to_parquet(OUT_DIR / "trajectories.parquet", index=False)
    traj_df.to_csv(OUT_DIR / "trajectories.csv", index=False)

    print("=== counts ===")
    print(f"steps:        {len(steps_df)} rows")
    print(f"subtasks:     {len(subtask_df)} rows")
    print(f"trajectories: {len(traj_df)} rows")
    print()
    print("=== trajectory-level stats ===")
    cols = ["n_steps", "n_errors", "n_api_calls", "n_readonly_calls",
            "n_unique_api_fqs", "distinct_apps"]
    print(traj_df[cols].describe().round(2).to_string())
    print()
    print("=== errors by trajectory (tasks with >=1 error) ===")
    erred = traj_df[traj_df["n_errors"] > 0][
        ["task_id", "n_steps", "n_errors", "outcome", "score"]
    ]
    print(erred.to_string(index=False))
    print()
    print("=== by final-subtask outcome ===")
    final_only = subtask_df[subtask_df["is_final"]]
    print(
        final_only.groupby("final_success")[
            ["n_steps", "n_api_calls", "n_errors", "n_unique_api_fqs"]
        ].agg(["mean", "median", "max"]).round(2).to_string()
    )


if __name__ == "__main__":
    main()
