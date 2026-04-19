"""
Categorize each failed task's failure mode by diffing the agent's action trace
against the ground-truth action list stored in info.reward_info.actions.

Outputs a markdown table + per-task diagnostic block.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

MUTATING = {
    "cancel_pending_order",
    "exchange_delivered_order_items",
    "modify_pending_order_address",
    "modify_pending_order_items",
    "modify_pending_order_payment",
    "modify_user_address",
    "return_delivered_order_items",
    "transfer_to_human_agents",
}
READONLY = {
    "get_order_details",
    "get_product_details",
    "get_user_details",
    "list_all_product_types",
    "find_user_id_by_email",
    "find_user_id_by_name_zip",
    "calculate",
    "think",
}


def parse_action(content: str) -> dict | None:
    if not content or "Action:" not in content:
        return None
    tail = content.split("Action:", 1)[-1].strip()
    try:
        j = json.loads(tail)
        if isinstance(j, dict) and "name" in j:
            return j
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def agent_actions(traj: list[dict]) -> list[dict]:
    """Return the parsed {name, arguments} the agent invoked, skipping malformed."""
    out = []
    for m in traj:
        if m.get("role") != "assistant":
            continue
        p = parse_action(m.get("content") or "")
        if p is not None:
            out.append(p)
    return out


def name_seq(actions: list[dict]) -> list[str]:
    return [a.get("name") for a in actions]


def kw_of(a: dict) -> dict:
    return a.get("arguments") or a.get("kwargs") or {}


def categorize(task_id: int,
               traj: list[dict],
               gt_actions: list[dict],
               max_consec_respond: int,
               ) -> dict[str, Any]:
    """Return a dict with diagnosis fields for a single failed task."""
    ag = agent_actions(traj)
    ag_names = [a.get("name") for a in ag]
    gt_names = [a.get("name") for a in gt_actions]

    ag_mut = [a for a in ag if a.get("name") in MUTATING]
    gt_mut = [a for a in gt_actions if a.get("name") in MUTATING]
    ag_mut_names = [a.get("name") for a in ag_mut]
    gt_mut_names = [a.get("name") for a in gt_mut]

    # Argument diff on mutating calls (best-effort by matching names in order)
    arg_mismatches: list[str] = []
    for i, gmut in enumerate(gt_mut):
        if i < len(ag_mut) and ag_mut[i].get("name") == gmut.get("name"):
            gk = kw_of(gmut)
            ak = kw_of(ag_mut[i])
            diff_keys = []
            for k in gk:
                if gk.get(k) != ak.get(k):
                    diff_keys.append(
                        f"{k} gt={_shorten(gk[k])} agent={_shorten(ak.get(k))}"
                    )
            if diff_keys:
                arg_mismatches.append(
                    f"{gmut['name']}: " + "; ".join(diff_keys)
                )

    # Primary category (priority ordered)
    category: str
    if not ag_mut and gt_mut:
        if max_consec_respond >= 6:
            category = "A. Policy-rigid respond-loop (never acts)"
        else:
            category = "B. Never commits mutating action"
    elif ag_mut and not gt_mut:
        category = "C. Wrongly commits when GT requires no mutation"
    elif len(ag_mut) < len(gt_mut):
        category = "D. Incomplete (committed some but not all mutations)"
    elif ag_mut_names != gt_mut_names:
        category = "E. Wrong mutating action chosen"
    elif arg_mismatches:
        category = "F. Right action, wrong arguments"
    else:
        category = "G. Actions match GT yet reward=0 (subtle arg / ordering issue)"

    initial_user = next(
        (m["content"] for m in traj if m.get("role") == "user"), ""
    )
    last_user = next(
        (m["content"] for m in reversed(traj) if m.get("role") == "user"), ""
    )

    return {
        "task_id": task_id,
        "category": category,
        "initial_user": _shorten(initial_user, 220),
        "last_user": _shorten(last_user, 180),
        "gt_mut_names": gt_mut_names,
        "agent_mut_names": ag_mut_names,
        "agent_action_seq": ag_names,
        "arg_mismatches": arg_mismatches,
        "n_agent_actions": len(ag),
        "n_gt_actions": len(gt_actions),
    }


def _shorten(v: Any, n: int = 60) -> str:
    s = str(v)
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("log_file", type=Path)
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    with args.log_file.open() as f:
        data = json.load(f)

    # Load D from per_task.csv for max_consec_respond.
    analysis_dir = (
        Path(__file__).resolve().parent / "analysis" / args.log_file.stem
    )
    per_task_csv = analysis_dir / "per_task.csv"
    d_by_task: dict[int, int] = {}
    if per_task_csv.exists():
        import csv
        with per_task_csv.open() as f:
            for row in csv.DictReader(f):
                d_by_task[int(row["task_id"])] = int(
                    float(row["D_max_consec_respond"])
                )

    failed = [r for r in data if r["reward"] == 0.0]
    diagnoses: list[dict[str, Any]] = []
    for r in failed:
        gt = r.get("info", {}).get("reward_info", {}).get("actions", []) or []
        diag = categorize(
            task_id=r["task_id"],
            traj=r["traj"],
            gt_actions=gt,
            max_consec_respond=d_by_task.get(r["task_id"], 0),
        )
        diagnoses.append(diag)

    # Category tally
    from collections import Counter
    cat_ct = Counter(d["category"] for d in diagnoses)

    out_lines: list[str] = []
    out_lines.append(f"# Failure-mode analysis — {args.log_file.name}\n")
    out_lines.append(f"Failed tasks: **{len(failed)}**\n")
    out_lines.append("## Failure-mode distribution\n")
    out_lines.append("| Category | count |")
    out_lines.append("|---|---|")
    for cat, n in sorted(cat_ct.items(), key=lambda kv: -kv[1]):
        out_lines.append(f"| {cat} | {n} |")
    out_lines.append("")

    out_lines.append("## Per-task diagnoses\n")
    for d in sorted(diagnoses, key=lambda d: (d["category"], d["task_id"])):
        out_lines.append(f"### task {d['task_id']} — {d['category']}")
        out_lines.append(f"- user request: {d['initial_user']}")
        out_lines.append(
            f"- GT mutating actions: `{d['gt_mut_names']}`"
        )
        out_lines.append(
            f"- agent mutating actions: `{d['agent_mut_names']}`"
        )
        out_lines.append(
            f"- agent action sequence "
            f"({d['n_agent_actions']} calls vs {d['n_gt_actions']} GT): "
            f"`{d['agent_action_seq']}`"
        )
        if d["arg_mismatches"]:
            out_lines.append("- argument mismatches on matched actions:")
            for m in d["arg_mismatches"]:
                out_lines.append(f"    - {m}")
        out_lines.append("")

    text = "\n".join(out_lines)
    out_path = args.out or (analysis_dir / "failure_modes.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text)
    print(text)
    print(f"\n(saved to {out_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
