"""Manual audit of the error-detection heuristic.

Dumps the output blocks that were flagged as errors, plus a few control
samples that were NOT flagged, into a single markdown file so a human can
verify the detector. Per the plan, if ≥2 of 10 audited samples disagree
with manual judgment, the detector needs rework.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pandas as pd

OUT = Path("analysis/tables/20_error_audit.md")


def main() -> None:
    steps = pd.read_parquet("analysis/tables/steps.parquet")

    flagged = steps[steps["output_had_error"]]
    not_flagged = steps[~steps["output_had_error"]].sample(n=5, random_state=0)

    with OUT.open("w") as f:
        f.write("# Error-detection audit\n\n")
        f.write(f"Flagged rows: {len(flagged)}  (of {len(steps)} total steps)\n\n")
        f.write(f"Tasks with flagged rows: {flagged['task_id'].nunique()}\n\n")
        f.write("## Flagged output blocks (all)\n\n")
        for _, row in flagged.iterrows():
            f.write(
                f"### task={row['task_id']} step={row['step_idx']} "
                f"(subtask {row['subtask_idx']}, in-subtask step {row['step_idx_in_subtask']})\n\n"
            )
            f.write(f"error_kinds: `{row['error_kinds']}`\n\n")
            f.write("**code**:\n```python\n")
            f.write(row["code"].strip()[:400])
            f.write("\n```\n\n")
            f.write("**output (first 600 chars)**:\n```\n")
            f.write(row["user_output"].strip()[:600])
            f.write("\n```\n\n---\n\n")

        f.write("## Random non-flagged samples (manual spot check)\n\n")
        for _, row in not_flagged.iterrows():
            f.write(
                f"### task={row['task_id']} step={row['step_idx']}\n\n"
            )
            f.write("**output (first 400 chars)**:\n```\n")
            f.write(row["user_output"].strip()[:400])
            f.write("\n```\n\n---\n\n")

    print(f"wrote {OUT}")
    print(f"flagged steps: {len(flagged)} across {flagged['task_id'].nunique()} tasks")
    print("error_kinds distribution:")
    for k, v in flagged["error_kinds"].value_counts().items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
