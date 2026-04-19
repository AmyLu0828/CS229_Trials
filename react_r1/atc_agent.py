"""
Adaptive Turn Compute (ATC) ReAct agent.

Per turn:
  1. Draw K probes at effort="low" (cheap).
  2. Compute action-entropy u(t) over parsed (action_name, args_hash).
  3. Dispatch:
       - u < tau_low  : commit majority-vote probe  (no extra call)
       - u > tau_high : one pass at effort="high"   (1 extra call)
       - otherwise    : one pass at effort="medium" (1 extra call)

Records on the committed message dict:
  _atc_u         : entropy of probe actions
  _atc_K         : number of probes
  _atc_branch    : "accept_probe" | "medium" | "high"
  _atc_probe_tokens   : total reasoning tokens spent on probes
  _atc_probe_actions  : list of {name, args_hash} from the K probes

No external model is used. Only the agent model itself. No training.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from typing import Any, Dict, List, Tuple

from r1_react_agent import ReasoningReActAgent, _is_openai_reasoning
import litellm
from litellm import completion

from tau_bench.types import Action, RESPOND_ACTION_NAME, RESPOND_ACTION_FIELD_NAME

litellm.drop_params = True


def _action_key(action_parsed: Dict[str, Any]) -> Tuple[str, str]:
    name = action_parsed.get("name", "")
    args = action_parsed.get("arguments") or {}
    try:
        blob = json.dumps(args, sort_keys=True, ensure_ascii=False)
    except TypeError:
        blob = str(args)
    return name, hashlib.md5(blob.encode()).hexdigest()[:10]


def _entropy(labels: List[Tuple[str, str]]) -> float:
    """Normalized Shannon entropy over discrete labels in [0, 1]."""
    if not labels:
        return 0.0
    counts = Counter(labels)
    n = sum(counts.values())
    if len(counts) == 1:
        return 0.0
    h = 0.0
    for c in counts.values():
        p = c / n
        h -= p * math.log(p)
    return h / math.log(len(counts))


class ATCAgent(ReasoningReActAgent):
    """ReAct agent that adaptively picks reasoning_effort per turn using
    cheap low-effort probes to estimate uncertainty."""

    def __init__(
        self,
        *args: Any,
        K: int = 3,
        tau_low: float = 0.0,
        tau_high: float = 0.9,
        probe_effort: str = "low",
        medium_effort: str = "medium",
        high_effort: str = "high",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.K = K
        self.tau_low = tau_low
        self.tau_high = tau_high
        self.probe_effort = probe_effort
        self.medium_effort = medium_effort
        self.high_effort = high_effort

    def _single_pass(
        self, messages: List[Dict[str, Any]], effort: str
    ) -> Tuple[Dict[str, Any], Action, float, Dict[str, Any]]:
        """One completion call at a specific reasoning_effort.

        Returns (msg_dict, Action, cost, parsed_action_dict).
        """
        kwargs: Dict[str, Any] = {}
        prov = (self.provider or "").lower()
        if prov == "openai" and _is_openai_reasoning(self.model):
            kwargs["reasoning_effort"] = effort
        else:
            if prov == "openrouter":
                kwargs["extra_body"] = {"include_reasoning": True}
            elif prov in ("anthropic", "bedrock"):
                kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": self.thinking_budget_tokens,
                }
            kwargs["temperature"] = self.temperature

        res = completion(
            model=self.model,
            custom_llm_provider=self.provider,
            messages=messages,
            **kwargs,
        )
        msg = res.choices[0].message
        msg_dict = msg.model_dump()

        raw_content = msg_dict.get("content") or ""
        from r1_react_agent import _strip_think, _THINK_BLOCK
        leaked = _THINK_BLOCK.findall(raw_content)
        reasoning_text = (
            msg_dict.get("reasoning")
            or msg_dict.get("reasoning_content")
            or ("\n".join(leaked) if leaked else "")
        )
        cleaned = _strip_think(raw_content)

        usage = getattr(res, "usage", None)
        reasoning_tokens = None
        completion_tokens = None
        if usage is not None:
            det = getattr(usage, "completion_tokens_details", None)
            if det is not None:
                reasoning_tokens = getattr(det, "reasoning_tokens", None)
            completion_tokens = getattr(usage, "completion_tokens", None)

        action_str = cleaned.split("Action:")[-1].strip()
        try:
            action_parsed = json.loads(action_str)
            if not isinstance(action_parsed, dict) or "name" not in action_parsed:
                raise ValueError("bad action shape")
        except (json.JSONDecodeError, ValueError):
            action_parsed = {
                "name": RESPOND_ACTION_NAME,
                "arguments": {RESPOND_ACTION_FIELD_NAME: action_str},
            }
        action_parsed.setdefault("arguments", {})
        action = Action(
            name=action_parsed["name"], kwargs=action_parsed["arguments"]
        )

        msg_dict["content"] = cleaned
        msg_dict["_reasoning"] = reasoning_text
        msg_dict["_reasoning_char_len"] = len(reasoning_text)
        msg_dict["_reasoning_tokens"] = reasoning_tokens
        msg_dict["_completion_tokens"] = completion_tokens
        msg_dict["_effort"] = effort

        try:
            cost = res._hidden_params.get("response_cost", 0.0) or 0.0
        except AttributeError:
            cost = 0.0
        return msg_dict, action, cost, action_parsed

    def generate_next_step(
        self, messages: List[Dict[str, Any]]
    ) -> Tuple[Dict[str, Any], Action, float]:
        # Optional per-turn override via messages[-1]["_atc_override_effort"]
        override = None
        if messages and isinstance(messages[-1], dict):
            override = messages[-1].get("_atc_override_effort")

        if override in ("low", "medium", "high"):
            msg_dict, action, cost, _ = self._single_pass(messages, override)
            msg_dict["_atc_branch"] = f"override_{override}"
            msg_dict["_atc_K"] = 0
            msg_dict["_atc_u"] = None
            return msg_dict, action, cost

        # --- Probe phase ---
        probes = []
        probe_total_cost = 0.0
        probe_total_reasoning = 0
        for _ in range(self.K):
            _m, _a, _c, parsed = self._single_pass(messages, self.probe_effort)
            probes.append(parsed)
            probe_total_cost += _c
            rt = _m.get("_reasoning_tokens") or 0
            probe_total_reasoning += rt

        labels = [_action_key(p) for p in probes]
        u = _entropy(labels)

        # --- Dispatch ---
        if u <= self.tau_low:
            # Majority-vote the probes and commit — no extra call.
            top_label = Counter(labels).most_common(1)[0][0]
            # Pick the first probe matching the top label as the committed one.
            chosen_parsed = next(p for p in probes if _action_key(p) == top_label)
            # Build a synthetic msg_dict that reports the probe's action.
            from tau_bench.types import Action as _A  # local alias
            action = _A(
                name=chosen_parsed["name"],
                kwargs=chosen_parsed.get("arguments", {}),
            )
            msg_dict = {
                "role": "assistant",
                "content": "Action:\n" + json.dumps({
                    "name": chosen_parsed["name"],
                    "arguments": chosen_parsed.get("arguments", {}),
                }),
                "_reasoning": "",
                "_reasoning_char_len": 0,
                "_reasoning_tokens": 0,  # committed call added 0 tokens
                "_completion_tokens": 0,
                "_effort": self.probe_effort,
                "_atc_branch": "accept_probe",
            }
            final_cost = probe_total_cost
        elif u >= self.tau_high:
            msg_dict, action, cost, _ = self._single_pass(messages, self.high_effort)
            msg_dict["_atc_branch"] = "high"
            final_cost = probe_total_cost + cost
        else:
            msg_dict, action, cost, _ = self._single_pass(messages, self.medium_effort)
            msg_dict["_atc_branch"] = "medium"
            final_cost = probe_total_cost + cost

        msg_dict["_atc_u"] = u
        msg_dict["_atc_K"] = self.K
        msg_dict["_atc_probe_tokens"] = probe_total_reasoning
        msg_dict["_atc_probe_actions"] = [
            {"name": p.get("name"), "args_hash": _action_key(p)[1]} for p in probes
        ]
        return msg_dict, action, final_cost
