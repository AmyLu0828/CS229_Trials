"""
Generate overthinking-analysis figures from an analyzer output directory.

Usage:
    python react_r1/make_figures.py react_r1/logs/<run>.json

Writes PNGs into react_r1/analysis/<run>/figures/.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
ANALYSIS_ROOT = HERE / "analysis"


def _parse_action(content: str) -> str | None:
    if "Action:" not in content:
        return None
    tail = content.split("Action:", 1)[-1].strip()
    try:
        j = json.loads(tail)
        return j.get("name")
    except (json.JSONDecodeError, ValueError):
        return None


def _action_class(name: str | None) -> str:
    mutating = {
        "cancel_pending_order",
        "exchange_delivered_order_items",
        "modify_pending_order_address",
        "modify_pending_order_items",
        "modify_pending_order_payment",
        "modify_user_address",
        "return_delivered_order_items",
        "transfer_to_human_agents",
    }
    readonly = {
        "get_order_details",
        "get_product_details",
        "get_user_details",
        "list_all_product_types",
        "find_user_id_by_email",
        "find_user_id_by_name_zip",
        "calculate",
        "think",
    }
    if name in mutating:
        return "mutating"
    if name in readonly:
        return "readonly"
    if name == "respond":
        return "respond"
    return "other"


def _assistant_turns(traj: list[dict]) -> list[dict]:
    return [m for m in traj if m.get("role") == "assistant"]


def fig1_overview(per_task: list[dict], outpath: Path) -> None:
    fig, axes = plt.subplots(2, 2, figsize=(12, 9))
    fig.suptitle("o3-mini ReAct × tau-bench retail (N=20, medium reasoning)",
                 fontsize=13, fontweight="bold")

    succ_color, fail_color = "#2a9d8f", "#e76f51"

    def color(r): return succ_color if int(r["success"]) == 1 else fail_color

    # (a) per-task reasoning tokens bar, sorted by task_id
    ax = axes[0, 0]
    rows = sorted(per_task, key=lambda r: int(r["task_id"]))
    xs = np.arange(len(rows))
    e1 = [float(r["E1_total_reasoning_tokens"]) for r in rows]
    colors = [color(r) for r in rows]
    ax.bar(xs, e1, color=colors, edgecolor="black", linewidth=0.4)
    ax.set_xticks(xs)
    ax.set_xticklabels([r["task_id"] for r in rows], fontsize=8)
    ax.set_xlabel("task id")
    ax.set_ylabel("reasoning tokens per task (E1)")
    ax.set_title("(a) Hidden reasoning budget per task\n"
                 "green = solved, red = failed",
                 fontsize=10)
    ax.grid(True, axis="y", alpha=0.3)

    # (b) D_max_consec_respond: success vs failure strip+box
    ax = axes[0, 1]
    succ = [float(r["D_max_consec_respond"])
            for r in per_task if int(r["success"]) == 1]
    fail = [float(r["D_max_consec_respond"])
            for r in per_task if int(r["success"]) == 0]
    bp = ax.boxplot([succ, fail], positions=[0, 1], widths=0.45,
                    patch_artist=True, showmeans=True,
                    meanprops=dict(marker="D", markerfacecolor="white",
                                   markeredgecolor="black", markersize=7))
    for patch, c in zip(bp["boxes"], [succ_color, fail_color]):
        patch.set_facecolor(c)
        patch.set_alpha(0.55)
    # jittered points
    rng = np.random.default_rng(0)
    for i, grp in enumerate([succ, fail]):
        jx = i + (rng.random(len(grp)) - 0.5) * 0.22
        ax.scatter(jx, grp, s=42, color=[succ_color, fail_color][i],
                   edgecolor="black", linewidth=0.5, zorder=3)
    ax.set_xticks([0, 1])
    ax.set_xticklabels([f"success (n={len(succ)})", f"failure (n={len(fail)})"])
    ax.set_ylabel("max consecutive respond turns (D)")
    ax.set_title("(b) Key discriminator: respond-loops\n"
                 "failures talk ~2× as long without acting",
                 fontsize=10)
    ax.grid(True, axis="y", alpha=0.3)

    # (c) Scatter: tool_calls (readonly+mutating) vs respond, by outcome
    ax = axes[1, 0]
    for r in per_task:
        tools = int(float(r["n_readonly"])) + int(float(r["n_mutating"]))
        resp = int(float(r["n_respond"]))
        ax.scatter(resp, tools, s=80, color=color(r),
                   edgecolor="black", linewidth=0.6, alpha=0.85,
                   label=None)
    # diagonal: equal talk vs action
    lim = max(max(int(float(r["n_respond"])) for r in per_task),
              max(int(float(r["n_readonly"])) + int(float(r["n_mutating"]))
                  for r in per_task))
    ax.plot([0, lim], [0, lim], "--", color="gray", alpha=0.5,
            label="equal talk/action")
    ax.set_xlabel("# respond turns (talking)")
    ax.set_ylabel("# tool calls (readonly + mutating)")
    ax.set_title("(c) Act vs talk — successes act more\n"
                 "failures cluster below the diagonal",
                 fontsize=10)
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(True, alpha=0.3)

    # (d) Histogram of E3 reasoning/completion ratio
    ax = axes[1, 1]
    e3 = [float(r["E3_reasoning_to_completion_ratio"]) for r in per_task]
    bins = np.linspace(0.7, 0.95, 13)
    ax.hist([v for v, r in zip(e3, per_task) if int(r["success"]) == 1],
            bins=bins, alpha=0.7, label="success", color=succ_color,
            edgecolor="black", linewidth=0.4)
    ax.hist([v for v, r in zip(e3, per_task) if int(r["success"]) == 0],
            bins=bins, alpha=0.7, label="failure", color=fail_color,
            edgecolor="black", linewidth=0.4)
    ax.axvline(np.mean(e3), color="black", linestyle="--",
               label=f"mean={np.mean(e3):.2f}")
    ax.set_xlabel("reasoning tokens / completion tokens (E3)")
    ax.set_ylabel("# tasks")
    ax.set_title("(d) 82% of output is hidden reasoning\n"
                 "…and the ratio is identical in wins and losses",
                 fontsize=10)
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(True, alpha=0.3)

    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(outpath, dpi=140, bbox_inches="tight")
    plt.close(fig)


def fig2_per_turn_timelines(data: list[dict], outpath: Path) -> None:
    """Per-turn reasoning tokens + action class for three representative tasks."""
    wanted = [
        (4, "FAIL — policy-rigid respond loop\n(0 tool calls in 12 turns)"),
        (19, "FAIL — high-effort wandering\n(11,136 reasoning tokens, failed)"),
        (2, "SUCCESS — high-effort productive\n(10,368 reasoning tokens, solved)"),
    ]
    fig, axes = plt.subplots(len(wanted), 1, figsize=(12, 8), sharex=False)

    action_colors = {
        "respond": "#e76f51",
        "readonly": "#457b9d",
        "mutating": "#2a9d8f",
        "other": "#999999",
    }

    for ax, (task_id, title) in zip(axes, wanted):
        rec = next((r for r in data if r["task_id"] == task_id), None)
        if rec is None:
            ax.set_visible(False)
            continue
        asst = _assistant_turns(rec["traj"])
        turns = np.arange(1, len(asst) + 1)
        rt = [m.get("_reasoning_tokens") or 0 for m in asst]
        cls = [_action_class(_parse_action(m.get("content") or ""))
               for m in asst]
        bar_colors = [action_colors.get(c, "#777777") for c in cls]
        ax.bar(turns, rt, color=bar_colors, edgecolor="black", linewidth=0.5)
        ax.set_title(f"task {task_id}: {title}", fontsize=10)
        ax.set_xlabel("agent turn")
        ax.set_ylabel("reasoning tokens")
        ax.set_xticks(turns)
        ax.grid(True, axis="y", alpha=0.3)

    from matplotlib.patches import Patch
    legend_elems = [
        Patch(facecolor=action_colors["respond"], edgecolor="black",
              label="respond (talk)"),
        Patch(facecolor=action_colors["readonly"], edgecolor="black",
              label="readonly tool"),
        Patch(facecolor=action_colors["mutating"], edgecolor="black",
              label="mutating tool"),
    ]
    fig.legend(handles=legend_elems, loc="upper center",
               bbox_to_anchor=(0.5, 1.02), ncol=3, frameon=False, fontsize=10)
    fig.suptitle("Per-turn reasoning vs action type — three trajectory archetypes",
                 fontsize=12, fontweight="bold", y=1.06)
    fig.tight_layout()
    fig.savefig(outpath, dpi=140, bbox_inches="tight")
    plt.close(fig)


def fig3_action_composition(per_task: list[dict], outpath: Path) -> None:
    """Stacked bars of avg action-type counts, success vs failure."""
    fig, ax = plt.subplots(figsize=(7.5, 5))
    groups = [("success", 1), ("failure", 0)]
    category_keys = [
        ("respond (talk)", "n_respond", "#e76f51"),
        ("readonly tool", "n_readonly", "#457b9d"),
        ("mutating tool", "n_mutating", "#2a9d8f"),
    ]
    xs = np.arange(len(groups))
    bottoms = np.zeros(len(groups))
    for label, field, color in category_keys:
        vals = []
        for _, succ_flag in groups:
            subset = [float(r[field]) for r in per_task
                      if int(r["success"]) == succ_flag]
            vals.append(np.mean(subset) if subset else 0.0)
        bars = ax.bar(xs, vals, bottom=bottoms, color=color,
                      edgecolor="black", linewidth=0.5, label=label)
        for rect, v in zip(bars, vals):
            if v >= 0.5:
                ax.text(rect.get_x() + rect.get_width() / 2,
                        rect.get_y() + v / 2,
                        f"{v:.1f}",
                        ha="center", va="center", fontsize=10,
                        color="white", fontweight="bold")
        bottoms += np.array(vals)
    ax.set_xticks(xs)
    ax.set_xticklabels([f"{g[0]} (n={sum(1 for r in per_task if int(r['success'])==g[1])})"
                         for g in groups])
    ax.set_ylabel("mean # turns per task")
    ax.set_title("Action composition: successes DO more, failures TALK more",
                 fontsize=11, fontweight="bold")
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(outpath, dpi=140, bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("log_file", type=Path)
    args = p.parse_args()

    run_name = args.log_file.stem
    analysis_dir = ANALYSIS_ROOT / run_name
    per_task_csv = analysis_dir / "per_task.csv"
    if not per_task_csv.exists():
        print(f"ERROR: {per_task_csv} not found. Run analyze_r1_retail.py first.",
              file=sys.stderr)
        return 2
    figs_dir = analysis_dir / "figures"
    figs_dir.mkdir(parents=True, exist_ok=True)

    with per_task_csv.open() as f:
        per_task = list(csv.DictReader(f))
    with args.log_file.open() as f:
        data = json.load(f)

    fig1_overview(per_task, figs_dir / "overview.png")
    fig2_per_turn_timelines(data, figs_dir / "per_turn_timelines.png")
    fig3_action_composition(per_task, figs_dir / "action_composition.png")

    print(f"Wrote:")
    for name in ["overview.png", "per_turn_timelines.png",
                 "action_composition.png"]:
        print(f"  {figs_dir / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
