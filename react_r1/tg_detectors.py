"""
Trajectory Guardrail (TG) drift detectors — pure Python, no model calls.

Each detector takes a trajectory prefix (list of messages as stored in the
tau-bench EnvRunResult.traj) plus the next-proposed action and returns either
None (no drift) or a dict {id, reason, turn_index} describing the firing.

Design commitments:
  - No embedding or LLM calls. Similarity uses n-gram Jaccard.
  - No domain-specific (retail/airline) strings or action names in detector
    logic; only structural properties of the ReAct trajectory.
  - Detectors operate on the prefix *as of turn t*, so they can be used
    online (inside the solve loop) or offline (replayed on a finished run).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

# Action-type taxonomy used ONLY for distinguishing "talk" vs "tool" turns.
# The set of tool names is not enumerated here — anything non-respond is a
# tool call from TG's point of view.
RESPOND_ACTIONS = {"respond"}
ESCALATION_ACTIONS = {"transfer_to_human_agents"}


_ACTION_RE = re.compile(r"Action:\s*(\{.*\})\s*$", re.DOTALL)


def parse_action(content: str) -> Optional[Dict[str, Any]]:
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


def _args_hash(action: Dict[str, Any]) -> str:
    args = action.get("arguments") or action.get("kwargs") or {}
    try:
        blob = json.dumps(args, sort_keys=True, ensure_ascii=False)
    except TypeError:
        blob = str(args)
    return hashlib.md5(blob.encode()).hexdigest()[:10]


def _assistant_turns(traj: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [m for m in traj if m.get("role") == "assistant"]


def _action_sequence(traj: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for m in _assistant_turns(traj):
        p = parse_action(m.get("content") or "")
        if p is not None:
            out.append(p)
    return out


def _name_seq(actions: List[Dict[str, Any]]) -> List[str]:
    return [a.get("name", "") for a in actions]


def _respond_content(action: Dict[str, Any]) -> str:
    args = action.get("arguments") or action.get("kwargs") or {}
    return str(args.get("content") or "")


def _ngram_jaccard(a: str, b: str, n: int = 5) -> float:
    """Character n-gram Jaccard similarity, no model calls."""
    if not a or not b:
        return 0.0
    def grams(s: str) -> set[str]:
        s = s.lower().strip()
        return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


# ---------- Detectors ----------


def detect_D1_respond_loop(
    actions: List[Dict[str, Any]],
    k: int = 3,
    sim_threshold: float = 0.2,
) -> Optional[Dict[str, Any]]:
    """Fires when the last k assistant actions are all 'respond' AND the
    max pairwise n-gram Jaccard similarity among those contents is
    >= sim_threshold (filters out genuine multi-step clarification
    exchanges where paraphrase is low).

    Sensitivity sweep on the n20 log (see sweep_d1.py) picked k=3,
    sim=0.2: 61.5% coverage on failures, 57% FPR on passes.
    Tighter alternatives: (k=3, sim=0.3): 54% / 43%. (k=4, sim=0): 38% / 29%.
    """
    if len(actions) < k:
        return None
    tail = actions[-k:]
    if any(a.get("name") not in RESPOND_ACTIONS for a in tail):
        return None
    contents = [_respond_content(a) for a in tail]
    max_sim = 0.0
    for i in range(len(contents)):
        for j in range(i + 1, len(contents)):
            s = _ngram_jaccard(contents[i], contents[j])
            if s > max_sim:
                max_sim = s
    if max_sim < sim_threshold:
        return None
    return {
        "id": "D1",
        "reason": "respond_loop",
        "streak": k,
        "max_pair_sim": round(max_sim, 3),
    }


def detect_D2_premature_escalation(
    actions: List[Dict[str, Any]],
    proposed_action: Optional[Dict[str, Any]],
    mutating_names: Optional[set[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Fires when the proposed action is an escalation AND no mutating action
    has been attempted yet in the trajectory.

    mutating_names is passed in so TG stays tool-name-agnostic. Pass None to
    use a heuristic: 'any action that is not respond, not escalation, and
    not a read-only lookup by common prefix'.
    """
    if proposed_action is None:
        return None
    if proposed_action.get("name") not in ESCALATION_ACTIONS:
        return None
    attempted = [a.get("name") for a in actions]
    if mutating_names is not None:
        any_mutating = any(n in mutating_names for n in attempted)
    else:
        # Heuristic fallback: treat non-respond, non-escalation,
        # non-'get_*'/'find_*'/'list_*' as mutating attempts.
        def is_mut(n: str) -> bool:
            if n in RESPOND_ACTIONS or n in ESCALATION_ACTIONS:
                return False
            return not (n.startswith("get_") or n.startswith("find_")
                         or n.startswith("list_") or n in {"calculate", "think"})
        any_mutating = any(is_mut(n or "") for n in attempted)
    if any_mutating:
        return None
    return {
        "id": "D2",
        "reason": "premature_escalation",
        "attempted_mutating": False,
        "prior_action_count": len(attempted),
    }


def detect_D3_action_repetition(
    actions: List[Dict[str, Any]],
    window: int = 4,
    min_repeats: int = 2,
) -> Optional[Dict[str, Any]]:
    """Fires when the same (action_name, args_hash) appears >= min_repeats
    times in the last `window` actions.
    """
    if len(actions) < min_repeats:
        return None
    tail = actions[-window:]
    counts: Dict[Tuple[str, str], int] = {}
    for a in tail:
        key = (a.get("name", ""), _args_hash(a))
        counts[key] = counts.get(key, 0) + 1
    for (name, h), c in counts.items():
        if c >= min_repeats and name not in RESPOND_ACTIONS:
            return {
                "id": "D3",
                "reason": "action_repetition",
                "name": name,
                "repeats": c,
                "window": window,
            }
    return None


# ---------- Orchestrator ----------


def run_detectors(
    traj_prefix: List[Dict[str, Any]],
    proposed_action: Optional[Dict[str, Any]] = None,
    mutating_names: Optional[set[str]] = None,
    d1_k: int = 3,
    d3_window: int = 4,
) -> List[Dict[str, Any]]:
    """Return the list of all detectors that fire at the current prefix.
    Used both online (during solve) and offline (replaying a finished traj).
    """
    actions = _action_sequence(traj_prefix)
    fires: List[Dict[str, Any]] = []
    f1 = detect_D1_respond_loop(actions, k=d1_k)
    if f1:
        fires.append(f1)
    f2 = detect_D2_premature_escalation(actions, proposed_action, mutating_names)
    if f2:
        fires.append(f2)
    f3 = detect_D3_action_repetition(actions, window=d3_window)
    if f3:
        fires.append(f3)
    return fires
