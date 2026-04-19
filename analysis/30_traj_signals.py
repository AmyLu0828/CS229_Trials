"""Compute the four overthinking signals per subtask and per trajectory.

Signals (see overthinking_analysis_plan.md §2.4):
  A. saturation_ratio           — redundant read-only API calls
     late_readonly_fraction     — read-only calls after the first mutating call
  B. loop_fraction              — steps inside 3-gram repeats or stuck runs
  C. stagnation_fraction        — 5-step windows with no new info / no mutation
  D. novelty_drop               — (novel APIs first half) − (novel APIs second half)
     error_shift                — (error rate second half) − (error rate first half)

Writes:
  analysis/tables/signals_subtask.parquet  (228 rows)
  analysis/tables/signals_traj.parquet     (57 rows)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

from parser import READONLY_PREFIXES, is_mutating

OUT_DIR = Path("analysis/tables")


def is_readonly(api_name: str) -> bool:
    return api_name.startswith(READONLY_PREFIXES)


def signal_A(api_calls_per_step: list[list[dict]],
             errors: list[bool]) -> tuple[float, float, int, int]:
    """Return (saturation_ratio, late_readonly_fraction, redundant, total).

    Redundant: a read-only (api, arg_hash) that already appeared earlier AND
    that earlier call's step did not error.
    Late read-only: read-only calls that occur after the first mutating call.
    """
    seen_ok: set[tuple[str, str]] = set()
    redundant = 0
    total_ro = 0
    first_mutate_step: int | None = None
    late_ro = 0

    for step_i, calls in enumerate(api_calls_per_step):
        for c in calls:
            key = (c["fq"], c["arg_hash"])
            ro = is_readonly(c["api"])
            if ro:
                total_ro += 1
                if key in seen_ok:
                    redundant += 1
                if first_mutate_step is not None and step_i > first_mutate_step:
                    late_ro += 1
            elif is_mutating(c["api"]):
                if first_mutate_step is None:
                    first_mutate_step = step_i
        # Mark all calls in this step as seen if the step did not error
        if not errors[step_i]:
            for c in calls:
                seen_ok.add((c["fq"], c["arg_hash"]))

    saturation_ratio = redundant / max(1, total_ro)
    late_ro_fraction = late_ro / max(1, total_ro) if first_mutate_step is not None else 0.0
    return saturation_ratio, late_ro_fraction, redundant, total_ro


def signal_B(action_sequence: list[str]) -> tuple[float, int, int]:
    """Return (loop_fraction, n_steps_in_loops, n_steps).

    A step is 'in a loop' if it falls inside either
      (a) a 3-gram of actions that appears ≥3 times in the sequence, or
      (b) a run of ≥3 consecutive identical actions.
    """
    n = len(action_sequence)
    if n == 0:
        return 0.0, 0, 0
    in_loop = [False] * n

    # (a) repeated 3-grams
    if n >= 3:
        from collections import Counter, defaultdict
        trigrams = [tuple(action_sequence[i:i+3]) for i in range(n - 2)]
        counts = Counter(trigrams)
        repeated = {tg for tg, c in counts.items() if c >= 3}
        for i, tg in enumerate(trigrams):
            if tg in repeated:
                for j in range(i, i + 3):
                    in_loop[j] = True

    # (b) stuck run ≥3 consecutive identical
    i = 0
    while i < n:
        j = i
        while j + 1 < n and action_sequence[j + 1] == action_sequence[i]:
            j += 1
        if j - i + 1 >= 3:
            for k in range(i, j + 1):
                in_loop[k] = True
        i = j + 1

    steps_in_loops = sum(in_loop)
    return steps_in_loops / n, steps_in_loops, n


_PK_REGEX = re.compile(
    r'("(?:song|album|playlist|note|file|track|user|transaction|'
    r'message|contact|phone_number|email|access_token|id)_id"\s*:\s*\d+'
    r'|"title"\s*:\s*"[^"]+"'
    r'|"name"\s*:\s*"[^"]+"'
    r'|"email"\s*:\s*"[^"]+")',
    re.IGNORECASE,
)


def signal_C(api_calls_per_step: list[list[dict]],
             user_outputs: list[str]) -> tuple[float, int, int]:
    """Return (stagnation_fraction, stagnation_windows, total_windows).

    A 5-step sliding window is a stagnation window if:
      * no mutating API was called in any of those 5 steps
      * no novel API name (first occurrence in the trajectory) appeared
      * no novel primary-key value (song_id/title/...) appeared in the outputs
    """
    n = len(api_calls_per_step)
    if n < 5:
        return 0.0, 0, 0

    seen_api: set[str] = set()
    seen_pk: set[str] = set()
    is_mutating_step = [False] * n
    new_api_step = [False] * n
    new_pk_step = [False] * n
    for i in range(n):
        new_api = False
        for c in api_calls_per_step[i]:
            if is_mutating(c["api"]):
                is_mutating_step[i] = True
            if c["fq"] not in seen_api:
                new_api = True
                seen_api.add(c["fq"])
        new_api_step[i] = new_api

        new_pk = False
        for m in _PK_REGEX.findall(user_outputs[i]):
            if m not in seen_pk:
                new_pk = True
                seen_pk.add(m)
        new_pk_step[i] = new_pk

    total_windows = n - 4
    stagn = 0
    for i in range(total_windows):
        win = range(i, i + 5)
        if any(is_mutating_step[j] for j in win):
            continue
        if any(new_api_step[j] for j in win):
            continue
        if any(new_pk_step[j] for j in win):
            continue
        stagn += 1
    return stagn / max(1, total_windows), stagn, total_windows


def signal_D(api_calls_per_step: list[list[dict]],
             errors: list[bool]) -> tuple[float, float]:
    """Return (novelty_drop, error_shift).

    novelty_drop = frac novel-API-calls first half  −  frac novel-API-calls second half
    error_shift  = error rate second half  −  error rate first half
    """
    n = len(api_calls_per_step)
    if n < 2:
        return 0.0, 0.0
    half = n // 2
    seen_first: set[str] = set()
    for calls in api_calls_per_step[:half]:
        for c in calls:
            seen_first.add(c["fq"])
    first_calls = [c for calls in api_calls_per_step[:half] for c in calls]
    second_calls = [c for calls in api_calls_per_step[half:] for c in calls]

    def novelty_frac(calls: Sequence[dict], already_seen: set[str]) -> float:
        novel = 0
        for c in calls:
            if c["fq"] not in already_seen:
                novel += 1
        return novel / max(1, len(calls))

    novelty_first = novelty_frac(first_calls, set())  # all first-half are novel at time of seeing
    novelty_second = novelty_frac(second_calls, seen_first)
    novelty_drop = novelty_first - novelty_second

    err_first = sum(errors[:half]) / max(1, half)
    err_second = sum(errors[half:]) / max(1, n - half)
    error_shift = err_second - err_first

    return novelty_drop, error_shift


def compute_for_steps(step_rows: pd.DataFrame) -> dict:
    """Given a DataFrame of steps sorted by step_idx within a task/subtask,
    compute all signals."""
    api_calls_per_step: list[list[dict]] = []
    errors: list[bool] = []
    outputs: list[str] = []
    actions: list[str] = []

    for _, r in step_rows.iterrows():
        calls = json.loads(r["api_calls_json"])
        api_calls_per_step.append(calls)
        errors.append(bool(r["output_had_error"]))
        outputs.append(r["user_output"])
        if calls:
            c = calls[0]
            actions.append(f"{c['fq']}|{c['arg_hash']}")
        else:
            actions.append("__no_call__")

    sat_ratio, late_ro, redund, total_ro = signal_A(api_calls_per_step, errors)
    loop_frac, loop_steps, n_steps = signal_B(actions)
    stagn_frac, stagn_windows, total_windows = signal_C(
        api_calls_per_step, outputs
    )
    novelty_drop, error_shift = signal_D(api_calls_per_step, errors)

    return {
        "saturation_ratio": sat_ratio,
        "redundant_ro_calls": redund,
        "total_ro_calls": total_ro,
        "late_readonly_fraction": late_ro,
        "loop_fraction": loop_frac,
        "loop_steps": loop_steps,
        "stagnation_fraction": stagn_frac,
        "stagnation_windows": stagn_windows,
        "total_stag_windows": total_windows,
        "novelty_drop": novelty_drop,
        "error_shift": error_shift,
        "n_steps_used": n_steps,
    }


def main() -> None:
    steps = pd.read_parquet(OUT_DIR / "steps.parquet")
    subtasks = pd.read_parquet(OUT_DIR / "subtasks.parquet")
    traj = pd.read_parquet(OUT_DIR / "trajectories.parquet")

    # Subtask-level signals
    sub_rows = []
    for (task_idx, sub_idx), grp in steps.groupby(["task_idx", "subtask_idx"]):
        grp = grp.sort_values("step_idx")
        sig = compute_for_steps(grp)
        sig.update({"task_idx": task_idx, "subtask_idx": sub_idx})
        sub_rows.append(sig)
    sub_sig = pd.DataFrame(sub_rows)
    sub_sig = sub_sig.merge(subtasks, on=["task_idx", "subtask_idx"], how="left")
    sub_sig.to_parquet(OUT_DIR / "signals_subtask.parquet", index=False)
    sub_sig.to_csv(OUT_DIR / "signals_subtask.csv", index=False)

    # Trajectory-level signals (all 4 subtasks concatenated)
    traj_rows = []
    for task_idx, grp in steps.groupby("task_idx"):
        grp = grp.sort_values("step_idx")
        sig = compute_for_steps(grp)
        sig["task_idx"] = task_idx
        traj_rows.append(sig)
    traj_sig = pd.DataFrame(traj_rows).merge(traj, on="task_idx", how="left")
    traj_sig.to_parquet(OUT_DIR / "signals_traj.parquet", index=False)
    traj_sig.to_csv(OUT_DIR / "signals_traj.csv", index=False)

    print("=== Signal distributions (final subtasks only, n=57) ===")
    fs = sub_sig[sub_sig["is_final"]]
    cols = ["saturation_ratio", "late_readonly_fraction",
            "loop_fraction", "stagnation_fraction",
            "novelty_drop", "error_shift"]
    print(fs[cols].describe().round(3).to_string())
    print()
    print("=== Final-subtask signals grouped by outcome ===")
    print(
        fs.groupby("final_success")[cols].agg(["mean", "median", "max"]).round(3)
        .to_string()
    )
    print()
    print("=== Spearman correlations (final-subtask signal vs final_success) ===")
    from scipy.stats import spearmanr
    for c in cols:
        r, p = spearmanr(fs[c], fs["final_success"])
        print(f"  {c:24s}  rho={r:+.3f}  p={p:.3f}")

    print()
    print("=== Also: final-subtask n_steps vs final_success ===")
    r, p = spearmanr(fs["n_steps"], fs["final_success"])
    print(f"  n_steps vs success: rho={r:+.3f}  p={p:.3f}")


if __name__ == "__main__":
    main()
