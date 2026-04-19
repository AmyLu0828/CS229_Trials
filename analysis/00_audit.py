"""Step 0 audit: verify log schema, cross-check summary.json, emit a tiny CSV.

Reads: appworld_playbook_gpt54_60/{log.jsonl, summary.json, config.json}
Writes: analysis/tables/00_audit.csv
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

RUN_DIR = Path("appworld_playbook_gpt54_60")
OUT_DIR = Path("analysis/tables")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def main() -> None:
    with (RUN_DIR / "summary.json").open() as f:
        summary = json.load(f)
    with (RUN_DIR / "config.json").open() as f:
        config = json.load(f)

    expected_keys = {
        "step", "task_id", "task_description", "task_meta",
        "context_retrieved", "trajectory", "result",
        "reflection_text", "state_stats", "timing",
    }

    rows = []
    with (RUN_DIR / "log.jsonl").open() as f:
        for line_no, raw in enumerate(f):
            obj = json.loads(raw)
            traj = obj["trajectory"]
            row = {
                "line_no": line_no,
                "step_global": obj["step"],
                "task_id": obj["task_id"],
                "split": obj["task_meta"].get("split"),
                "outcome": obj["result"]["outcome"],
                "score": obj["result"]["score"],
                "n_assistant_turns": traj.count("ASSISTANT:"),
                "n_user_turns": traj.count("USER:"),
                "n_python_fences": traj.count("```python"),
                "traj_chars": len(traj),
                "keys_ok": set(obj.keys()) == expected_keys,
                "generate_s": obj["timing"]["generate_s"],
                "playbook_bullets": obj["state_stats"]["total_bullets"],
                "playbook_tokens": obj["state_stats"]["approx_tokens"],
            }
            rows.append(row)

    df = pd.DataFrame(rows)

    assert len(df) == summary["n_tasks"], (
        f"log line count {len(df)} != summary.n_tasks {summary['n_tasks']}"
    )

    outcome_counts = df["outcome"].value_counts().to_dict()
    assert outcome_counts.get("success", 0) == summary["outcome_counts"]["success"]
    assert outcome_counts.get("failure", 0) == summary["outcome_counts"]["failure"]

    assert (df["score"].values == pd.Series(summary["scores"]).values).all(), \
        "per-line scores disagree with summary.json"

    df.to_csv(OUT_DIR / "00_audit.csv", index=False)

    print("=== Audit ===")
    print(f"Run:   {config['run_id']}")
    print(f"Model: {config['model']}  temperature={config['temperature']}")
    print(f"State: {config['state_type']}  snapshot_interval={config['snapshot_interval']}")
    print()
    print(f"n_tasks:   {len(df)}")
    print(f"outcomes:  {outcome_counts}")
    print(f"accuracy:  {summary['accuracy']:.4f}   avg_score: {summary['avg_score']:.4f}")
    print(f"schema OK: {bool(df['keys_ok'].all())}")
    print()
    print("assistant turns per task: "
          f"min={df['n_assistant_turns'].min()} "
          f"median={int(df['n_assistant_turns'].median())} "
          f"max={df['n_assistant_turns'].max()} "
          f"mean={df['n_assistant_turns'].mean():.2f}")
    print("code fences per task:     "
          f"min={df['n_python_fences'].min()} "
          f"median={int(df['n_python_fences'].median())} "
          f"max={df['n_python_fences'].max()}")
    print("trajectory chars:         "
          f"min={df['traj_chars'].min()} "
          f"median={int(df['traj_chars'].median())} "
          f"max={df['traj_chars'].max()}")
    print()
    print(f"wrote {OUT_DIR / '00_audit.csv'}")


if __name__ == "__main__":
    main()
