"""Shared parsing utilities for ACE-AppWorld trajectories.

A trajectory is a single string with line-level delimiters `USER:` and
`ASSISTANT:`. We turn it into a list of step dicts, one per ASSISTANT turn.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

RUN_DIR = Path("appworld_playbook_gpt54_60")

# Line is *exactly* USER: or ASSISTANT: (with possibly trailing whitespace)
_DELIM = re.compile(r"^(USER|ASSISTANT):\s*$", re.MULTILINE)

# A fenced python block. Non-greedy, DOTALL.
_PY_FENCE = re.compile(r"```python\s*\n(.*?)```", re.DOTALL)

# Any fenced code block (for USER "Output:" blocks, language tag optional).
_ANY_FENCE = re.compile(r"```(?:\w+)?\s*\n(.*?)```", re.DOTALL)

# An API call: apis.<app>.<api>(  -- we grab up to the matching paren naively
# with balanced depth in a small helper below.
_API_HEAD = re.compile(r"\bapis\.([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*\(")

# Heuristic: API is read-only if its name starts with one of these prefixes.
READONLY_PREFIXES = (
    "show_", "search_", "get_", "list_", "find_", "read_",
    "check_", "has_", "is_", "exists_",
)

# Substantive "mutating" prefixes — excludes `login`/`logout`/`complete_task`
# because those are setup / submission, not task-meaningful state changes.
# We use this positive whitelist for the "first mutating action" reference
# point in signal A (late_readonly_fraction) and signal C.
MUTATING_PREFIXES = (
    "send_", "post_", "create_", "add_", "delete_", "remove_", "update_",
    "edit_", "transfer_", "pay_", "request_", "like_", "follow_", "book_",
    "cancel_", "mark_", "reply_", "download_", "upload_",
)


def is_mutating(api_name: str) -> bool:
    return api_name.startswith(MUTATING_PREFIXES)

# Error signal strings (applied to the USER "Output" block that follows a step).
# The ACE-AppWorld REPL emits errors as `Execution failed. Traceback:` with a
# following `Exception:` or specific error class line.
_ERR_EXEC_FAILED = re.compile(r"Execution failed")
_ERR_TRACEBACK = re.compile(r"Traceback:")
_ERR_EXCEPT_LINE = re.compile(r"(?m)^Exception:\s")
_ERR_HTTP_4XX = re.compile(r"status code is 4\d\d")
_ERR_HTTP_5XX = re.compile(r"status code is 5\d\d")
# AppWorld supervisor rejection of complete_task (heuristic, to be audited).
_ERR_SUPERVISOR_REJECT = re.compile(
    r"(Your answer .*? is not correct|answer is incorrect|Task not yet complete|"
    r"You have not completed the task|re-?check|check your answer)",
    re.IGNORECASE,
)


def _balanced_paren(text: str, start: int) -> int:
    """Return index just past the matching `)` for the `(` at `text[start-1]`.

    `start` should point to the char after the opening paren. Returns -1 on
    failure (unbalanced).
    """
    depth = 1
    i = start
    n = len(text)
    in_str: str | None = None
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in ("'", '"'):
            in_str = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def extract_api_calls(code: str) -> list[dict]:
    """Extract `apis.<app>.<api>(...)` invocations from a code string.

    We preserve the raw arg string and a stable hash of it (after collapsing
    whitespace) for near-identical detection.
    """
    calls: list[dict] = []
    for m in _API_HEAD.finditer(code):
        app = m.group(1)
        api = m.group(2)
        start = m.end()
        end = _balanced_paren(code, start)
        if end < 0:
            args_raw = ""
        else:
            args_raw = code[start : end - 1]
        # Canonicalize args for hashing
        canon = re.sub(r"\s+", " ", args_raw).strip()
        arg_hash = hashlib.sha1(canon.encode("utf-8")).hexdigest()[:10]
        calls.append(
            {
                "app": app,
                "api": api,
                "fq": f"{app}.{api}",
                "args_raw": args_raw,
                "args_canon": canon,
                "arg_hash": arg_hash,
                "readonly": api.startswith(READONLY_PREFIXES),
            }
        )
    return calls


def detect_error(user_output: str) -> tuple[bool, list[str]]:
    """Heuristic error detection on a USER "Output:" block."""
    hits: list[str] = []
    if _ERR_EXEC_FAILED.search(user_output):
        hits.append("execution_failed")
    if _ERR_TRACEBACK.search(user_output):
        hits.append("traceback")
    if _ERR_EXCEPT_LINE.search(user_output):
        hits.append("exception_line")
    if _ERR_HTTP_4XX.search(user_output):
        hits.append("http_4xx")
    if _ERR_HTTP_5XX.search(user_output):
        hits.append("http_5xx")
    if _ERR_SUPERVISOR_REJECT.search(user_output):
        hits.append("supervisor_reject")
    return (bool(hits), hits)


@dataclass
class Step:
    task_id: str
    task_idx: int
    step_idx: int
    assistant_text: str
    code: str
    user_output: str
    api_calls: list[dict] = field(default_factory=list)
    output_had_error: bool = False
    error_kinds: list[str] = field(default_factory=list)
    called_complete_task: bool = False
    n_chars_assistant: int = 0
    n_chars_output: int = 0
    subtask_idx: int = -1              # 0..3 (AppWorld bundles 4 subtasks/log-line)
    step_idx_in_subtask: int = -1
    is_final_subtask: bool = False
    subtask_description: str = ""


_SUBTASK_LINE = re.compile(r"(?m)^Task:\s*(.+)$")


def parse_trajectory(traj: str, task_id: str, task_idx: int) -> list[Step]:
    """Split a trajectory into Step objects (one per ASSISTANT turn).

    Each ACE-AppWorld log line contains 4 chained subtasks; this function also
    attaches a `subtask_idx` in {0,1,2,3} (and `is_final_subtask` for idx=3) to
    each step. Subtasks are delimited in the trajectory by `Task:` lines inside
    USER turns and end with an `apis.supervisor.complete_task(...)` invocation.
    """
    matches = list(_DELIM.finditer(traj))
    if not matches:
        return []
    segments: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        role = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(traj)
        content = traj[start:end].strip("\n")
        segments.append((role, content))

    steps: list[Step] = []
    step_idx = 0
    i = 0
    current_subtask = -1
    current_subtask_desc = ""
    while i < len(segments):
        role, content = segments[i]
        if role == "USER":
            task_hits = _SUBTASK_LINE.findall(content)
            if task_hits:
                current_subtask += 1
                current_subtask_desc = task_hits[0].strip()
            i += 1
            continue
        code_blocks = _PY_FENCE.findall(content)
        code = "\n".join(code_blocks)
        user_out = ""
        if i + 1 < len(segments) and segments[i + 1][0] == "USER":
            user_out = segments[i + 1][1]
        err, err_kinds = detect_error(user_out)
        api_calls = extract_api_calls(code)
        steps.append(
            Step(
                task_id=task_id,
                task_idx=task_idx,
                step_idx=step_idx,
                assistant_text=content,
                code=code,
                user_output=user_out,
                api_calls=api_calls,
                output_had_error=err,
                error_kinds=err_kinds,
                called_complete_task="apis.supervisor.complete_task(" in code,
                n_chars_assistant=len(content),
                n_chars_output=len(user_out),
                subtask_idx=max(0, current_subtask),
                step_idx_in_subtask=-1,  # set in a post-pass below
                subtask_description=current_subtask_desc,
            )
        )
        step_idx += 1
        i += 1

    # Derive per-subtask indices and final-subtask flag
    n_subtasks = max((s.subtask_idx for s in steps), default=-1) + 1
    per_sub_counter: dict[int, int] = {}
    for s in steps:
        c = per_sub_counter.get(s.subtask_idx, 0)
        s.step_idx_in_subtask = c
        per_sub_counter[s.subtask_idx] = c + 1
        s.is_final_subtask = s.subtask_idx == n_subtasks - 1

    return steps


def iter_trajectories() -> Iterator[dict]:
    """Yield each raw log line as a dict."""
    with (RUN_DIR / "log.jsonl").open() as f:
        for line in f:
            yield json.loads(line)


def step_to_dict(s: Step) -> dict:
    return {
        "task_id": s.task_id,
        "task_idx": s.task_idx,
        "step_idx": s.step_idx,
        "subtask_idx": s.subtask_idx,
        "step_idx_in_subtask": s.step_idx_in_subtask,
        "is_final_subtask": s.is_final_subtask,
        "subtask_description": s.subtask_description,
        "n_chars_assistant": s.n_chars_assistant,
        "n_chars_output": s.n_chars_output,
        "code": s.code,
        "user_output": s.user_output,
        "api_calls_json": json.dumps(s.api_calls),
        "n_api_calls": len(s.api_calls),
        "apis_called": ";".join(c["fq"] for c in s.api_calls),
        "output_had_error": s.output_had_error,
        "error_kinds": ";".join(s.error_kinds),
        "called_complete_task": s.called_complete_task,
    }
