"""Dump human-readable excerpts of every failure and the 5 longest successes
to analysis/report/excerpts/<task_id>.md for qualitative inspection.

For each trajectory we write:
- Header: task_id, outcome, score, n_steps, signals
- Final-subtask task description
- Per-step concise summary: api_calls + first line of output (up to 300 chars)
- Full code+output of the step containing the final complete_task() call

Writes one .md per trajectory in analysis/report/excerpts/
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from parser import iter_trajectories, parse_trajectory

EXC = Path("analysis/report/excerpts")
EXC.mkdir(parents=True, exist_ok=True)


def dump_one(obj: dict, task_idx: int, signals_row: pd.Series, tag: str) -> None:
    steps = parse_trajectory(obj["trajectory"], obj["task_id"], task_idx)
    ct_steps = [s for s in steps if s.called_complete_task]
    final_sub = [s for s in steps if s.is_final_subtask]
    first_in_final = final_sub[0].step_idx if final_sub else 0

    out = EXC / f"{tag}_{obj['task_id']}.md"
    with out.open("w") as f:
        f.write(f"# {obj['task_id']}  [{obj['result']['outcome']}]  score={obj['result']['score']:.3f}\n\n")
        f.write(f"**Tag:** {tag}\n\n")
        f.write(f"**Final-subtask description:** {final_sub[0].subtask_description if final_sub else '(none)'}\n\n")
        if obj['result'].get('details'):
            f.write(f"**Failure details:** `{obj['result']['details']}`\n\n")
        f.write(f"**Total steps:** {len(steps)};  final subtask starts at step {first_in_final} (in-subtask step 0)\n\n")
        f.write("**Final-subtask signals:**\n\n")
        for c in ["n_steps", "saturation_ratio", "late_readonly_fraction",
                  "novelty_drop", "error_shift"]:
            if c in signals_row:
                f.write(f"- {c}: {signals_row[c]:.3f}\n")
        f.write("\n---\n\n")

        f.write("## Final subtask: per-step summary\n\n")
        f.write("| step | apis called | output snippet |\n|---|---|---|\n")
        for s in final_sub:
            apis = ", ".join(c["fq"] for c in s.api_calls) or "—"
            snip = s.user_output.replace("\n", " ").replace("|", "\\|")
            snip = snip[:140] + ("…" if len(snip) > 140 else "")
            err = " **[ERR]**" if s.output_had_error else ""
            ct = " **[complete_task]**" if s.called_complete_task else ""
            f.write(f"| {s.step_idx_in_subtask} | `{apis}`{ct}{err} | {snip} |\n")
        f.write("\n---\n\n")

        f.write("## Full code+output of final complete_task step\n\n")
        if ct_steps:
            last = ct_steps[-1]
            f.write(f"step {last.step_idx} (in-subtask step {last.step_idx_in_subtask})\n\n")
            f.write("**code**:\n```python\n")
            f.write(last.code.strip()[:2000])
            f.write("\n```\n\n")
            if last.user_output.strip():
                f.write("**output**:\n```\n")
                f.write(last.user_output.strip()[:1200])
                f.write("\n```\n\n")

        f.write("## Full trajectory of final subtask\n\n")
        for s in final_sub:
            f.write(f"### step {s.step_idx} (in-subtask {s.step_idx_in_subtask})\n\n")
            if s.code.strip():
                f.write("```python\n")
                f.write(s.code.strip()[:1500])
                f.write("\n```\n\n")
            if s.user_output.strip():
                f.write("output:\n```\n")
                out_trunc = s.user_output.strip()
                if len(out_trunc) > 1500:
                    out_trunc = out_trunc[:1500] + "\n...[truncated]..."
                f.write(out_trunc)
                f.write("\n```\n\n")


def main() -> None:
    sigs = pd.read_parquet("analysis/tables/signals_subtask.parquet")
    sigs = sigs[sigs["is_final"]]
    traj = pd.read_parquet("analysis/tables/trajectories.parquet")
    objs = list(iter_trajectories())

    # All 8 failures
    fail_idx = traj[traj["success_flag"] == 0]["task_idx"].tolist()
    # 5 longest-final-subtask successes
    succ = (traj.merge(sigs.rename(columns={"n_steps": "n_final_steps"})
                        [["task_idx", "n_final_steps"]], on="task_idx")
                 .query("success_flag == 1"))
    longest_success_idx = (
        succ.sort_values("n_final_steps", ascending=False).head(5)["task_idx"].tolist()
    )
    # 5 successes with highest combined signal (candidate "lucky")
    lucky_scores = (
        sigs.assign(combined=
                    sigs["saturation_ratio"].rank(pct=True)
                    + sigs["late_readonly_fraction"].rank(pct=True)
                    + sigs["novelty_drop"].rank(pct=True))
        .merge(traj[["task_idx", "success_flag"]], on="task_idx")
        .query("success_flag == 1")
        .sort_values("combined", ascending=False)
        .head(5)["task_idx"].tolist()
    )

    seen: set[int] = set()

    for idx in fail_idx:
        if idx in seen: continue
        seen.add(idx)
        sig_row = sigs[sigs["task_idx"] == idx].iloc[0]
        dump_one(objs[idx], idx, sig_row, tag="FAILURE")
    for idx in longest_success_idx:
        if idx in seen: continue
        seen.add(idx)
        sig_row = sigs[sigs["task_idx"] == idx].iloc[0]
        dump_one(objs[idx], idx, sig_row, tag="LONG_SUCCESS")
    for idx in lucky_scores:
        if idx in seen: continue
        seen.add(idx)
        sig_row = sigs[sigs["task_idx"] == idx].iloc[0]
        dump_one(objs[idx], idx, sig_row, tag="LUCKY_SUCCESS")

    print(f"wrote {len(seen)} excerpt files to {EXC}")
    print(f"failures: {len(fail_idx)}")
    print(f"longest successes: {len(longest_success_idx)}")
    print(f"lucky successes: {len(lucky_scores)}")


if __name__ == "__main__":
    main()
