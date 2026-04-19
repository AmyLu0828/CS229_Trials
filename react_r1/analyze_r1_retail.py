"""
Analyze a tau-bench retail ReAct+R1 result JSON for overthinking signals.

Inputs : react_r1/logs/<run>.json   (list of EnvRunResult dicts)
Outputs: react_r1/analysis/<run>/
          ├─ per_task.csv             # one row per task
          ├─ per_turn.csv             # one row per assistant turn
          ├─ signals_summary.md       # human-readable summary
          └─ excerpts/task_<id>.md    # per-task dump with reasoning

Reuses the four overthinking signals from the prior AppWorld analysis (A–D)
and adds reasoning-channel signals (E1–E4) that are only meaningful for
reasoning models like R1.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

# ----- Retail action taxonomy -----

READONLY_ACTIONS = {
    "get_order_details",
    "get_product_details",
    "get_user_details",
    "list_all_product_types",
    "find_user_id_by_email",
    "find_user_id_by_name_zip",
    "calculate",
    "think",
}
MUTATING_ACTIONS = {
    "cancel_pending_order",
    "exchange_delivered_order_items",
    "modify_pending_order_address",
    "modify_pending_order_items",
    "modify_pending_order_payment",
    "modify_user_address",
    "return_delivered_order_items",
    "transfer_to_human_agents",
}
CONVERSATIONAL_ACTIONS = {"respond"}


def action_class(name: str) -> str:
    if name in MUTATING_ACTIONS:
        return "mutating"
    if name in READONLY_ACTIONS:
        return "readonly"
    if name in CONVERSATIONAL_ACTIONS:
        return "respond"
    return "other"


# ----- Parsing agent turns out of a tau-bench trajectory -----

_ACTION_RE = re.compile(r"Action:\s*(\{.*\})\s*$", re.DOTALL)


def extract_action_from_content(content: str) -> dict[str, Any] | None:
    """Parse the last 'Action:' JSON blob out of a ReAct content string."""
    if not content:
        return None
    tail = content.split("Action:")[-1].strip()
    try:
        parsed = json.loads(tail)
        if isinstance(parsed, dict) and "name" in parsed:
            return parsed
    except json.JSONDecodeError:
        return None
    return None


def parse_turns(traj: list[dict], task_id: int) -> list[dict]:
    """Convert a tau-bench traj to per-assistant-turn feature dicts."""
    turns = []
    step_idx = 0
    prev_obs = None
    for i, msg in enumerate(traj):
        if msg.get("role") != "assistant":
            if msg.get("role") == "user":
                prev_obs = msg.get("content") or ""
            continue
        content = msg.get("content") or ""
        action = extract_action_from_content(content)
        if action is None:
            name, args = "malformed", {}
        else:
            name = action.get("name", "malformed")
            args = action.get("arguments", {}) or {}

        args_canonical = json.dumps(args, sort_keys=True, default=str)

        reasoning = msg.get("_reasoning") or ""
        reasoning_tokens = msg.get("_reasoning_tokens")
        completion_tokens = msg.get("_completion_tokens")
        is_error_obs = (
            isinstance(prev_obs, str)
            and (prev_obs.lower().startswith("error")
                 or "error" in prev_obs[:120].lower())
        )

        turns.append({
            "task_id": task_id,
            "step_idx": step_idx,
            "msg_idx": i,
            "action_name": name,
            "action_class": action_class(name),
            "args_canonical": args_canonical,
            "content_char_len": len(content),
            "reasoning_char_len": len(reasoning),
            "reasoning_tokens": reasoning_tokens,
            "completion_tokens": completion_tokens,
            "prev_obs_was_error": bool(is_error_obs),
        })
        step_idx += 1
    return turns


# ----- Signals -----

def signal_A_late_readonly_fraction(turns: list[dict], tail_frac: float = 0.5) -> float:
    """A: fraction of late-trajectory tool calls that are read-only.
    High = information saturation (kept looking things up near the end)."""
    tool_turns = [t for t in turns if t["action_class"] in ("readonly", "mutating")]
    if len(tool_turns) < 4:
        return float("nan")
    tail_start = int(len(tool_turns) * (1 - tail_frac))
    tail = tool_turns[tail_start:]
    if not tail:
        return float("nan")
    return sum(1 for t in tail if t["action_class"] == "readonly") / len(tail)


def signal_B_action_repetition(turns: list[dict]) -> float:
    """B: fraction of turns whose (name, args) matches the immediately
    preceding turn. Captures tight loops."""
    if len(turns) < 2:
        return 0.0
    reps = 0
    for t1, t2 in zip(turns, turns[1:]):
        if (t1["action_name"] == t2["action_name"]
                and t1["args_canonical"] == t2["args_canonical"]):
            reps += 1
    return reps / (len(turns) - 1)


def signal_C_error_rate(turns: list[dict]) -> float:
    """C: fraction of assistant turns immediately preceded by an error obs."""
    if not turns:
        return 0.0
    return sum(1 for t in turns if t["prev_obs_was_error"]) / len(turns)


def signal_D_repeated_respond(turns: list[dict]) -> int:
    """D: count of consecutive 'respond' turns. High counts suggest the agent
    is talking in circles (variant of premature disengagement)."""
    best = 0
    cur = 0
    for t in turns:
        if t["action_class"] == "respond":
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def signal_E_reasoning(turns: list[dict]) -> dict[str, float]:
    """E: reasoning-channel signals for R1-style models."""
    r_tokens = [t["reasoning_tokens"] for t in turns
                if isinstance(t["reasoning_tokens"], (int, float))]
    r_chars = [t["reasoning_char_len"] for t in turns]
    c_tokens = [t["completion_tokens"] for t in turns
                if isinstance(t["completion_tokens"], (int, float))]

    respond_reasoning = [
        t["reasoning_char_len"]
        for t in turns if t["action_class"] == "respond"
    ]
    tool_reasoning = [
        t["reasoning_char_len"]
        for t in turns if t["action_class"] in ("readonly", "mutating")
    ]

    out = {
        "E1_total_reasoning_tokens": float(sum(r_tokens)) if r_tokens else float("nan"),
        "E2_mean_reasoning_tokens_per_turn":
            float(statistics.mean(r_tokens)) if r_tokens else float("nan"),
        "E3_reasoning_to_completion_ratio":
            (sum(r_tokens) / max(1, sum(c_tokens))) if (r_tokens and c_tokens)
            else float("nan"),
        "E4_respond_vs_tool_reasoning_ratio":
            (statistics.mean(respond_reasoning) / max(1, statistics.mean(tool_reasoning)))
            if respond_reasoning and tool_reasoning else float("nan"),
        "total_reasoning_chars": float(sum(r_chars)),
    }
    return out


# ----- Orchestration -----

def analyze_task(result: dict) -> dict[str, Any]:
    task_id = result.get("task_id")
    reward = result.get("reward", 0.0)
    traj = result.get("traj") or []
    turns = parse_turns(traj, task_id)

    action_counts = Counter(t["action_class"] for t in turns)
    row = {
        "task_id": task_id,
        "trial": result.get("trial", 0),
        "reward": reward,
        "success": int(reward == 1.0),
        "num_turns": len(turns),
        "n_respond": action_counts.get("respond", 0),
        "n_readonly": action_counts.get("readonly", 0),
        "n_mutating": action_counts.get("mutating", 0),
        "n_malformed": sum(1 for t in turns if t["action_name"] == "malformed"),
        "A_late_readonly_frac": signal_A_late_readonly_fraction(turns),
        "B_action_repetition": signal_B_action_repetition(turns),
        "C_error_rate": signal_C_error_rate(turns),
        "D_max_consec_respond": signal_D_repeated_respond(turns),
        **signal_E_reasoning(turns),
    }
    return row, turns


def fmt(v: Any) -> str:
    if isinstance(v, float):
        if v != v:
            return "nan"
        return f"{v:.3f}"
    return str(v)


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    keys = list(rows[0].keys())
    lines = [",".join(keys)]
    for r in rows:
        lines.append(",".join(fmt(r.get(k, "")) for k in keys))
    path.write_text("\n".join(lines))


def dump_excerpt(outdir: Path, result: dict, row: dict, turns: list[dict]) -> None:
    task_id = result.get("task_id")
    p = outdir / f"task_{task_id}.md"
    md = [f"# Task {task_id}  (reward={result.get('reward')})"]
    md.append("")
    md.append("## Summary row")
    md.append("```")
    md.extend(f"{k}: {fmt(v)}" for k, v in row.items())
    md.append("```")
    md.append("")
    md.append("## Turns")
    traj = result.get("traj") or []
    assistant_turns = [m for m in traj if m.get("role") == "assistant"]
    for t, msg in zip(turns, assistant_turns):
        md.append(f"### Step {t['step_idx']}  — "
                  f"{t['action_name']} ({t['action_class']})")
        md.append(f"reasoning_tokens={t['reasoning_tokens']}, "
                  f"reasoning_chars={t['reasoning_char_len']}, "
                  f"completion_tokens={t['completion_tokens']}")
        reasoning = msg.get("_reasoning") or ""
        if reasoning:
            md.append("")
            md.append("**Reasoning (truncated to 2000 chars):**")
            md.append("```")
            md.append(reasoning[:2000]
                      + ("\n...[truncated]..." if len(reasoning) > 2000 else ""))
            md.append("```")
        md.append("")
        md.append("**Content:**")
        md.append("```")
        md.append((msg.get("content") or "")[:1500])
        md.append("```")
        md.append("")
    p.write_text("\n".join(md))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log_file", help="path to react_r1/logs/<run>.json")
    ap.add_argument("--outdir", default=None)
    args = ap.parse_args()

    log_path = Path(args.log_file).resolve()
    if not log_path.exists():
        print(f"No such file: {log_path}")
        return 2

    with log_path.open() as f:
        results = json.load(f)

    outdir = Path(args.outdir) if args.outdir else (
        log_path.parent.parent / "analysis" / log_path.stem
    )
    (outdir / "excerpts").mkdir(parents=True, exist_ok=True)

    task_rows: list[dict] = []
    turn_rows: list[dict] = []
    for result in results:
        row, turns = analyze_task(result)
        task_rows.append(row)
        turn_rows.extend(turns)
        dump_excerpt(outdir / "excerpts", result, row, turns)

    write_csv(outdir / "per_task.csv", task_rows)
    write_csv(outdir / "per_turn.csv", turn_rows)

    def nan_mean(xs):
        vs = [x for x in xs if isinstance(x, (int, float)) and x == x]
        return statistics.mean(vs) if vs else float("nan")

    n = len(task_rows)
    solved = sum(r["success"] for r in task_rows)
    summary = []
    summary.append(f"# Summary — {log_path.name}")
    summary.append("")
    summary.append(f"- tasks: {n}")
    summary.append(f"- solved: {solved}  "
                   f"(accuracy = {solved / max(1, n):.3f})")
    summary.append(f"- mean turns: {nan_mean([r['num_turns'] for r in task_rows]):.2f}")
    summary.append(f"- mean #readonly per task: "
                   f"{nan_mean([r['n_readonly'] for r in task_rows]):.2f}")
    summary.append(f"- mean #mutating per task: "
                   f"{nan_mean([r['n_mutating'] for r in task_rows]):.2f}")
    summary.append("")
    summary.append("## Overthinking signals (mean over tasks)")
    for key in ("A_late_readonly_frac", "B_action_repetition", "C_error_rate",
                "D_max_consec_respond",
                "E1_total_reasoning_tokens", "E2_mean_reasoning_tokens_per_turn",
                "E3_reasoning_to_completion_ratio",
                "E4_respond_vs_tool_reasoning_ratio"):
        summary.append(f"- {key}: "
                       f"{nan_mean([r[key] for r in task_rows]):.3f}")
    summary.append("")
    summary.append("## By outcome")
    succ = [r for r in task_rows if r["success"] == 1]
    fail = [r for r in task_rows if r["success"] == 0]
    for label, group in [("successes", succ), ("failures", fail)]:
        summary.append(f"### {label} (n={len(group)})")
        for key in ("num_turns", "B_action_repetition", "C_error_rate",
                    "E2_mean_reasoning_tokens_per_turn",
                    "E3_reasoning_to_completion_ratio"):
            summary.append(f"- {key} mean: "
                           f"{nan_mean([r[key] for r in group]):.3f}")
        summary.append("")

    (outdir / "signals_summary.md").write_text("\n".join(summary))
    print("\n".join(summary))
    print(f"\nWrote: {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
