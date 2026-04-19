"""Cross-cutting analyses: length-vs-success curve, playbook effect, and
all figures for the report.

Outputs (to analysis/report/figures/):
  fig_length_histograms.png       — final-subtask n_steps by outcome
  fig_length_vs_success.png       — binned success rate with Wilson CI
  fig_signals_by_outcome.png      — signal distributions, side-by-side
  fig_playbook_effect.png         — signals vs playbook size
  fig_signal_scatter.png          — signals vs final_success (scatter)
  fig_subtask_order.png           — n_steps and error_rate by subtask order

And one CSV: analysis/tables/60_crosscut_stats.csv
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

FIG = Path("analysis/report/figures")
FIG.mkdir(parents=True, exist_ok=True)
TAB = Path("analysis/tables")


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    center = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = (z / (1 + z * z / n)) * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, center - half), min(1.0, center + half))


def main() -> None:
    subs = pd.read_parquet(TAB / "signals_subtask.parquet")
    fs = subs[subs["is_final"]].copy()
    traj = pd.read_parquet(TAB / "trajectories.parquet")

    # === Figure 1: length histograms ===
    fig, ax = plt.subplots(1, 2, figsize=(10, 4))
    bins = np.arange(3, 26)
    ax[0].hist(
        [fs[fs["final_success"] == 1]["n_steps"],
         fs[fs["final_success"] == 0]["n_steps"]],
        bins=bins, stacked=True, label=["success (49)", "failure (8)"],
        color=["#4c72b0", "#c44e52"], edgecolor="white",
    )
    ax[0].set_xlabel("final-subtask n_steps")
    ax[0].set_ylabel("count")
    ax[0].set_title("Distribution of final-subtask length\n(colored by outcome)")
    ax[0].legend()

    bins2 = np.arange(40, 62)
    ax[1].hist(
        [traj[traj["success_flag"] == 1]["n_steps"],
         traj[traj["success_flag"] == 0]["n_steps"]],
        bins=bins2, stacked=True, label=["success", "failure"],
        color=["#4c72b0", "#c44e52"], edgecolor="white",
    )
    ax[1].set_xlabel("full-trajectory n_steps (all 4 subtasks)")
    ax[1].set_ylabel("count")
    ax[1].set_title("Distribution of trajectory length")
    ax[1].legend()
    plt.tight_layout()
    plt.savefig(FIG / "fig_length_histograms.png", dpi=130)
    plt.close()

    # === Figure 2: length vs success, binned ===
    bins = [0, 8, 10, 12, 14, 25]
    fs["len_bin"] = pd.cut(fs["n_steps"], bins=bins, right=True,
                            labels=[f"{bins[i]+1}-{bins[i+1]}" for i in range(len(bins)-1)])
    agg = fs.groupby("len_bin").agg(
        n=("final_success", "size"),
        k=("final_success", "sum"),
    ).reset_index()
    agg["rate"] = agg["k"] / agg["n"]
    agg[["lo", "hi"]] = agg.apply(lambda r: pd.Series(wilson_ci(int(r["k"]), int(r["n"]))), axis=1)

    fig, ax = plt.subplots(figsize=(6.5, 4))
    x = np.arange(len(agg))
    ax.errorbar(x, agg["rate"],
                yerr=[agg["rate"] - agg["lo"], agg["hi"] - agg["rate"]],
                fmt="o-", color="#4c72b0", capsize=4, lw=2, markersize=8)
    for xi, n in zip(x, agg["n"]):
        ax.text(xi, 0.04, f"n={n}", ha="center", fontsize=9, color="gray")
    ax.set_xticks(x)
    ax.set_xticklabels(agg["len_bin"])
    ax.set_xlabel("final-subtask n_steps (binned)")
    ax.set_ylabel("success rate (Wilson 95% CI)")
    ax.set_ylim(0, 1.05)
    ax.set_title("Length vs. success rate on the final subtask")
    ax.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    plt.savefig(FIG / "fig_length_vs_success.png", dpi=130)
    plt.close()

    # === Figure 3: signals by outcome (boxplot) ===
    sigs = ["saturation_ratio", "late_readonly_fraction", "novelty_drop", "error_shift"]
    fig, axes = plt.subplots(1, 4, figsize=(14, 4))
    for ax, c in zip(axes, sigs):
        data = [fs[fs["final_success"] == 1][c].values,
                fs[fs["final_success"] == 0][c].values]
        bp = ax.boxplot(data, labels=["success", "failure"],
                         patch_artist=True, widths=0.6)
        for patch, col in zip(bp["boxes"], ["#4c72b0", "#c44e52"]):
            patch.set_facecolor(col)
            patch.set_alpha(0.5)
        ax.set_title(c, fontsize=10)
        ax.grid(axis="y", alpha=0.3)
    plt.suptitle("Final-subtask overthinking signals by outcome", y=1.02)
    plt.tight_layout()
    plt.savefig(FIG / "fig_signals_by_outcome.png", dpi=130)
    plt.close()

    # === Figure 4: playbook effect ===
    fs = fs.merge(traj[["task_idx", "playbook_bullets", "playbook_tokens"]],
                  on="task_idx", how="left")
    fig, axes = plt.subplots(1, 4, figsize=(14, 3.5))
    for ax, c in zip(axes, sigs):
        ax.scatter(fs["playbook_bullets"], fs[c],
                    c=["#c44e52" if s == 0 else "#4c72b0"
                       for s in fs["final_success"]],
                    alpha=0.7, s=28)
        r, p = spearmanr(fs["playbook_bullets"], fs[c])
        ax.set_xlabel("playbook bullets")
        ax.set_ylabel(c, fontsize=9)
        ax.set_title(f"{c}\nSpearman r={r:+.2f}, p={p:.2f}", fontsize=9)
        ax.grid(alpha=0.3)
    plt.suptitle("Overthinking signals vs. ACE playbook size", y=1.02)
    plt.tight_layout()
    plt.savefig(FIG / "fig_playbook_effect.png", dpi=130)
    plt.close()

    # === Figure 5: subtask ordering — are later subtasks longer or error-ier? ===
    sub_agg = subs.groupby("subtask_idx").agg(
        n_steps_mean=("n_steps", "mean"),
        n_steps_median=("n_steps", "median"),
        err_rate=("n_errors", lambda s: (s > 0).mean()),
        api_calls_mean=("n_api_calls", "mean"),
    ).reset_index()
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.5))
    ax[0].bar(sub_agg["subtask_idx"], sub_agg["n_steps_mean"],
              color="#4c72b0", alpha=0.8)
    ax[0].set_xlabel("subtask index (0-3)")
    ax[0].set_ylabel("mean n_steps")
    ax[0].set_title("Subtask length by order")
    ax[0].set_xticks([0, 1, 2, 3])
    ax[1].bar(sub_agg["subtask_idx"], sub_agg["err_rate"],
              color="#c44e52", alpha=0.8)
    ax[1].set_xlabel("subtask index (0-3)")
    ax[1].set_ylabel("fraction of subtasks with any error")
    ax[1].set_title("Subtask error-bearing rate by order")
    ax[1].set_xticks([0, 1, 2, 3])
    plt.tight_layout()
    plt.savefig(FIG / "fig_subtask_order.png", dpi=130)
    plt.close()

    # === CSV dump of key stats ===
    stats_rows = []
    for c in sigs + ["n_steps"]:
        r, p = spearmanr(fs[c], fs["final_success"])
        stats_rows.append({
            "metric": c,
            "median_success": fs[fs["final_success"] == 1][c].median(),
            "median_failure": fs[fs["final_success"] == 0][c].median(),
            "max_success": fs[fs["final_success"] == 1][c].max(),
            "max_failure": fs[fs["final_success"] == 0][c].max(),
            "spearman_vs_success": r,
            "spearman_p": p,
        })
    for c in sigs:
        r, p = spearmanr(fs["playbook_bullets"], fs[c])
        stats_rows.append({
            "metric": f"{c}__vs_playbook",
            "median_success": np.nan,
            "median_failure": np.nan,
            "max_success": np.nan,
            "max_failure": np.nan,
            "spearman_vs_success": r,
            "spearman_p": p,
        })
    stats = pd.DataFrame(stats_rows)
    stats.to_csv(TAB / "60_crosscut_stats.csv", index=False)

    # === Headline print ===
    print("=== length-vs-success by bin ===")
    print(agg.to_string(index=False))
    print()
    print("=== subtask ordering ===")
    print(sub_agg.round(3).to_string(index=False))
    print()
    print(f"figures saved under {FIG}")


if __name__ == "__main__":
    main()
