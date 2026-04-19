"""
Self-reported-confidence Adaptive Turn Compute (SATC).

Primary adaptive-compute method for this experiment. The agent emits its own
confidence (0-10) inside the Action JSON on every turn. That confidence sets
the reasoning_effort for the *next* turn:

    conf(t-1) >= tau_hi  ->  effort(t) = low
    conf(t-1) <= tau_lo  ->  effort(t) = high
    otherwise            ->  effort(t) = medium

Exactly 1 API call per turn — same cost as the fixed-effort baseline. The
adaptation is in the effort knob, not in call count.

Assumes the agent is prompted to emit "confidence" alongside "name" and
"arguments" in the Action JSON. We append that instruction to the ReAct
prompt automatically on construction.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from r1_react_agent import (
    ReasoningReActAgent,
    _is_openai_reasoning,
    _strip_think,
    _THINK_BLOCK,
)
import litellm
from litellm import completion

from tau_bench.types import Action, RESPOND_ACTION_NAME, RESPOND_ACTION_FIELD_NAME

litellm.drop_params = True


SATC_PROMPT_ADDENDUM = """

REQUIRED additional output field: self-reported confidence.

After choosing your next action, include a top-level integer field
"confidence" in the Action JSON (alongside "name" and "arguments"), where:

  0-2  = pure guess. You are not sure this action is correct, the right
         tool, or will produce useful output. Likely wrong.
  3-4  = weak guess. You picked this because something needed to happen
         but you are not confident it is the right move.
  5-6  = uncertain. Plausible but you can see alternatives; a mistake in
         the arguments or tool choice is possible.
  7-8  = confident. You have a clear reason this action is correct, given
         the context, tools, and policy so far.
  9-10 = near-certain. The action is the unique correct next step and
         both the tool name and every argument are determined by the
         context; no reasonable agent would pick differently.

Calibration rules you MUST follow:
- If you are asking the user a clarification question, set confidence <= 5.
  Asking is an admission that you cannot yet commit to a correct action.
- If the user's instruction is ambiguous, or you had to guess any argument
  value (ID, payment method, product option), set confidence <= 6.
- If policy might forbid this action and you have not verified, set
  confidence <= 6.
- If the same action has already been tried in this trajectory, lower your
  confidence by at least 2.
- Confidence of 9 or 10 is only justified when every argument is derived
  directly from verified information in this trajectory.

Do NOT default to 10. Most turns of a well-run task are 6-8. A trajectory
where every turn is 10 is almost certainly miscalibrated.

Examples:
Action:
{"name": "find_user_id_by_name_zip",
 "arguments": {"first_name": "Mei", "last_name": "Chen", "zip": "02139"},
 "confidence": 9}

Action:
{"name": "respond",
 "arguments": {"content": "Could you confirm whether you prefer a refund or an exchange?"},
 "confidence": 4}

Action:
{"name": "modify_pending_order_items",
 "arguments": {"order_id": "#W123", "item_ids": ["a"], "new_item_ids": ["b"], "payment_method_id": "credit_card_9"},
 "confidence": 6}
"""


def _extract_confidence(action_parsed: Dict[str, Any]) -> Tuple[Optional[int], str]:
    """Pull the confidence field from a parsed action.
    Returns (value_or_None, source) where source ∈ {'top', 'args', 'missing'}.
    """
    if "confidence" in action_parsed:
        v = action_parsed.pop("confidence")
        try:
            return int(v), "top"
        except (ValueError, TypeError):
            return None, "top_bad"
    args = action_parsed.get("arguments") or {}
    if isinstance(args, dict) and "confidence" in args:
        v = args.pop("confidence")
        try:
            return int(v), "args"
        except (ValueError, TypeError):
            return None, "args_bad"
    return None, "missing"


class SATCAgent(ReasoningReActAgent):
    """ReAct agent that selects per-turn reasoning_effort from the previous
    turn's self-reported confidence."""

    def __init__(
        self,
        *args: Any,
        tau_conf_hi: int = 7,
        tau_conf_lo: int = 3,
        default_conf: int = 5,
        start_effort: str = "medium",
        low_effort: str = "low",
        mid_effort: str = "medium",
        high_effort: str = "high",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.tau_conf_hi = tau_conf_hi
        self.tau_conf_lo = tau_conf_lo
        self.default_conf = default_conf
        self.start_effort = start_effort
        self.low_effort = low_effort
        self.mid_effort = mid_effort
        self.high_effort = high_effort

        # Reset per-trajectory state on every solve() call.
        self._last_confidence: Optional[int] = None

        # Append the confidence-emission instruction to the ReAct prompt.
        if SATC_PROMPT_ADDENDUM not in self.prompt:
            self.prompt = self.prompt + SATC_PROMPT_ADDENDUM

    def _choose_effort(self) -> str:
        if self._last_confidence is None:
            return self.start_effort
        if self._last_confidence >= self.tau_conf_hi:
            return self.low_effort
        if self._last_confidence <= self.tau_conf_lo:
            return self.high_effort
        return self.mid_effort

    def solve(self, *args: Any, **kwargs: Any):
        # Reset conversation-local state so multi-task runs don't leak.
        self._last_confidence = None
        return super().solve(*args, **kwargs)

    def generate_next_step(
        self, messages: List[Dict[str, Any]]
    ) -> Tuple[Dict[str, Any], Action, float]:
        effort = self._choose_effort()

        kw: Dict[str, Any] = {}
        prov = (self.provider or "").lower()
        if prov == "openai" and _is_openai_reasoning(self.model):
            kw["reasoning_effort"] = effort
        else:
            if prov == "openrouter":
                kw["extra_body"] = {"include_reasoning": True}
            elif prov in ("anthropic", "bedrock"):
                kw["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": self.thinking_budget_tokens,
                }
            kw["temperature"] = self.temperature

        res = completion(
            model=self.model,
            custom_llm_provider=self.provider,
            messages=messages,
            **kw,
        )

        msg = res.choices[0].message
        msg_dict = msg.model_dump()
        raw_content = msg_dict.get("content") or ""
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

        # Extract + STRIP confidence before building the Action sent to env.
        conf, conf_src = _extract_confidence(action_parsed)
        if conf is None:
            effective_conf = self.default_conf
        else:
            effective_conf = max(0, min(10, int(conf)))

        action = Action(
            name=action_parsed["name"], kwargs=action_parsed["arguments"]
        )

        msg_dict["content"] = cleaned
        msg_dict["_reasoning"] = reasoning_text
        msg_dict["_reasoning_char_len"] = len(reasoning_text)
        msg_dict["_reasoning_tokens"] = reasoning_tokens
        msg_dict["_completion_tokens"] = completion_tokens
        msg_dict["_effort"] = effort
        msg_dict["_satc_confidence_raw"] = conf
        msg_dict["_satc_confidence"] = effective_conf
        msg_dict["_satc_conf_source"] = conf_src
        msg_dict["_satc_effort"] = effort
        msg_dict["_satc_prev_confidence"] = self._last_confidence

        try:
            cost = res._hidden_params.get("response_cost", 0.0) or 0.0
        except AttributeError:
            cost = 0.0

        # Update state for the NEXT turn.
        self._last_confidence = effective_conf

        return msg_dict, action, cost
