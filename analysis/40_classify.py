"""Five-bucket classification of each log line.

Categories (applied to each trajectory using the final-subtask signals):
  1. clean_success  — succeeded, all signals at or below median
  2. lucky_success  — succeeded, but ≥2 signals in the top quartile
  3. overthinking_failure — failed, ≥2 signals in the top quartile
  4. clean_failure  — failed, all signals at or below median
  5. ambiguous      — everything else

Because `loop_fraction` and `stagnation_fraction` are constant (0) across all
57 trajectories, they can't contribute to classification and are excluded.
Remaining signals used: saturation_ratio, late_readonly_fraction,
novelty_drop, error_shift (absolute value).

Sensitivity analysis: classification repeated at top-20% and top-33% cutoffs.

Writes: analysis/tables/40_categories.csv
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path("analysis/tables/40_categories.csv")
SUMMARY = Path("analysis/tables/40_summary.md")

SIGNALS = [
    "saturation_ratio",
    "late_readonly_fraction",
    "novelty_drop",
    "abs_error_shift",
]


def classify(df: pd.DataFrame, top_frac: float = 0.25) -> pd.Series:
    """Return the category for each row given a top-fraction cutoff."""
    cutoffs = {c: df[c].quantile(1 - top_frac) for c in SIGNALS}
    medians = {c: df[c].median() for c in SIGNALS}

    labels: list[str] = []
    for _, row in df.iterrows():
        high = sum(1 for c in SIGNALS if row[c] > cutoffs[c])
        low = all(row[c] <= medians[c] for c in SIGNALS)
        is_success = row["final_success"] == 1

        if is_success and low:
            labels.append("clean_success")
        elif is_success and high >= 2:
            labels.append("lucky_success")
        elif not is_success and high >= 2:
            labels.append("overthinking_failure")
        elif not is_success and low:
            labels.append("clean_failure")
        else:
            labels.append("ambiguous")
    return pd.Series(labels, index=df.index)


def main() -> None:
    fs = pd.read_parquet("analysis/tables/signals_subtask.parquet")
    fs = fs[fs["is_final"]].copy()
    fs["abs_error_shift"] = fs["error_shift"].abs()

    results: dict[str, pd.Series] = {}
    for top_frac in (0.20, 0.25, 0.33):
        results[f"top_{int(top_frac*100)}pct"] = classify(fs, top_frac)
    out = fs[["task_idx", "task_id", "final_success", "n_steps"] + SIGNALS].copy()
    for k, v in results.items():
        out[k] = v.values

    out.to_csv(OUT, index=False)

    with SUMMARY.open("w") as f:
        f.write("# Trajectory category breakdown\n\n")
        f.write(
            "Categories assigned using the four non-degenerate final-subtask "
            "signals (saturation, late-readonly, novelty-drop, |error-shift|). "
            "`loop_fraction` and `stagnation_fraction` are zero for all 57 "
            "trajectories and cannot contribute.\n\n"
        )
        for label, col in [("Top quartile (q=0.25)", "top_25pct"),
                           ("Top quintile (q=0.20)", "top_20pct"),
                           ("Top tercile  (q=0.33)", "top_33pct")]:
            f.write(f"## {label}\n\n")
            counts = out[col].value_counts().reindex(
                ["clean_success", "lucky_success",
                 "overthinking_failure", "clean_failure",
                 "ambiguous"], fill_value=0
            )
            f.write("| category | count | share |\n")
            f.write("|---|---:|---:|\n")
            n = len(out)
            for k, v in counts.items():
                f.write(f"| {k} | {v} | {v/n*100:.1f}% |\n")
            f.write("\n")

    print(out.groupby("top_25pct").size().to_string())
    print()
    print(f"wrote {OUT} and {SUMMARY}")
    # Print the headline: overthinking-failure count at the default cutoff
    q25 = out["top_25pct"].value_counts().to_dict()
    n_fail = (out["final_success"] == 0).sum()
    print()
    print(f"Overthinking-failure count: {q25.get('overthinking_failure', 0)} / {n_fail} failures  "
          f"(= {q25.get('overthinking_failure', 0)/n_fail*100:.1f}% of failures)")


if __name__ == "__main__":
    main()
