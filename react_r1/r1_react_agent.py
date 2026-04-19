"""
ReAct agent for reasoning models (DeepSeek-R1, OpenAI o-series, Anthropic
extended-thinking) via LiteLLM.

Wraps tau_bench.agents.chat_react_agent.ChatReActAgent but:
  1. Passes provider-specific knobs to enable and surface reasoning tokens:
       - openai o-series:  reasoning_effort="low|medium|high"
       - anthropic:        thinking={"type": "enabled", "budget_tokens": N}
       - openrouter:       extra_body={"include_reasoning": True}
       - together_ai / deepseek: no extra param; reasoning surfaces in
                            message.reasoning_content automatically
  2. Strips any leaked <think>...</think> blocks from message.content
     before the "Action:" parser sees them.
  3. Records reasoning length, tokens, and the full reasoning string (when
     available) on each recorded message dict under:
       _reasoning (text; empty for openai o-series since it isn't exposed)
       _reasoning_char_len
       _reasoning_tokens (count, populated when provider reports it)
       _completion_tokens

Use `ReasoningReActAgent` directly; `R1ReActAgent` is kept as an alias.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_TAU_V1 = Path(__file__).resolve().parent.parent / "external" / "tau-bench-v1"
if _TAU_V1.exists() and str(_TAU_V1) not in sys.path:
    sys.path.insert(0, str(_TAU_V1))

import litellm  # noqa: E402
from litellm import completion  # noqa: E402

from tau_bench.agents.chat_react_agent import ChatReActAgent  # noqa: E402
from tau_bench.types import (  # noqa: E402
    Action,
    RESPOND_ACTION_FIELD_NAME,
    RESPOND_ACTION_NAME,
)

from logprob_utils import (  # noqa: E402
    compute_teca_summary,
    logprobs_to_dicts,
)

# Silently drop params a provider doesn't understand (e.g. temperature on
# o-series, which rejects anything other than default=1).
litellm.drop_params = True


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# OpenAI reasoning-model IDs (prefix match). These ignore `temperature` and
# require `max_completion_tokens` instead of `max_tokens`; LiteLLM handles both
# via drop_params=True.
_OPENAI_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")


def _strip_think(s: str) -> str:
    """Remove leaked <think>...</think> blocks from a content string."""
    if not s:
        return s
    return _THINK_BLOCK.sub("", s).strip()


def _is_openai_reasoning(model: str) -> bool:
    base = model.split("/")[-1]
    return any(base.startswith(p) for p in _OPENAI_REASONING_PREFIXES)


class ReasoningReActAgent(ChatReActAgent):
    """ReAct agent with provider-aware reasoning-token capture.

    Args mirror ChatReActAgent, plus:
      reasoning_effort: "low" | "medium" | "high" (OpenAI o-series only).
      thinking_budget_tokens: int (Anthropic extended-thinking budget).
    """

    def __init__(
        self,
        *args: Any,
        reasoning_effort: str = "high",
        thinking_budget_tokens: int = 8192,
        capture_logprobs: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.reasoning_effort = reasoning_effort
        self.thinking_budget_tokens = thinking_budget_tokens
        self.capture_logprobs = capture_logprobs

    def _completion_kwargs(self) -> Dict[str, Any]:
        """Provider-specific kwargs that enable / expose reasoning."""
        kw: Dict[str, Any] = {}
        prov = (self.provider or "").lower()

        if prov == "openrouter":
            kw["extra_body"] = {"include_reasoning": True}
        elif prov == "openai" and _is_openai_reasoning(self.model):
            # OpenAI o-series: use reasoning_effort; temperature is ignored.
            kw["reasoning_effort"] = self.reasoning_effort
        elif prov in ("anthropic", "bedrock"):
            # Anthropic extended thinking.
            kw["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.thinking_budget_tokens,
            }
        # together_ai / deepseek / vertexai: reasoning surfaces automatically.

        # Optional per-token chosen-logprob capture (currently supported on
        # together_ai DeepSeek-R1; other providers silently ignore).
        if self.capture_logprobs:
            kw["logprobs"] = 1
        return kw

    def generate_next_step(
        self, messages: List[Dict[str, Any]]
    ) -> Tuple[Dict[str, Any], Action, float]:
        kwargs = self._completion_kwargs()
        # Only pass temperature when the provider respects it; o-series rejects
        # non-default temperature. drop_params=True already silences the error,
        # but skipping the arg avoids a LiteLLM warning log.
        if not (self.provider == "openai" and _is_openai_reasoning(self.model)):
            kwargs["temperature"] = self.temperature

        res = completion(
            model=self.model,
            custom_llm_provider=self.provider,
            messages=messages,
            **kwargs,
        )

        message = res.choices[0].message
        msg_dict = message.model_dump()

        # Pull reasoning text from any field a provider might use.
        reasoning_text = (
            msg_dict.get("reasoning")
            or msg_dict.get("reasoning_content")
            or ""
        )
        raw_content = msg_dict.get("content") or ""

        # Some providers leak <think> blocks into content; salvage them as
        # reasoning text if no separate field was returned.
        leaked = _THINK_BLOCK.findall(raw_content)
        if leaked and not reasoning_text:
            reasoning_text = "\n".join(leaked)
        cleaned_content = _strip_think(raw_content)

        # For openai reasoning models, the provider returns only a token count
        # (reasoning_tokens inside completion_tokens_details), not text.
        usage = getattr(res, "usage", None)
        reasoning_tokens: Optional[int] = None
        completion_tokens: Optional[int] = None
        if usage is not None:
            details = getattr(usage, "completion_tokens_details", None)
            if details is not None:
                reasoning_tokens = getattr(details, "reasoning_tokens", None)
            completion_tokens = getattr(usage, "completion_tokens", None)

        # ReAct action parsing: split on "Action:" and JSON-decode the tail.
        action_str = cleaned_content.split("Action:")[-1].strip()
        try:
            action_parsed = json.loads(action_str)
            if not isinstance(action_parsed, dict) or "name" not in action_parsed:
                raise ValueError("bad action shape")
        except (json.JSONDecodeError, ValueError):
            action_parsed = {
                "name": RESPOND_ACTION_NAME,
                "arguments": {RESPOND_ACTION_FIELD_NAME: action_str},
            }
        if "arguments" not in action_parsed:
            action_parsed["arguments"] = {}
        action = Action(
            name=action_parsed["name"], kwargs=action_parsed["arguments"]
        )

        msg_dict["content"] = cleaned_content
        msg_dict["_reasoning"] = reasoning_text
        msg_dict["_reasoning_char_len"] = len(reasoning_text)
        msg_dict["_reasoning_tokens"] = reasoning_tokens
        msg_dict["_completion_tokens"] = completion_tokens

        # Logprobs capture (opt-in).
        if self.capture_logprobs:
            lp_obj = getattr(res.choices[0], "logprobs", None)
            tok_list = logprobs_to_dicts(lp_obj)
            teca = compute_teca_summary(tok_list)
            # Keep the raw per-token stream compact: {token, logprob} only.
            msg_dict["_logprobs"] = tok_list
            msg_dict["_teca"] = teca.to_dict()
            # Backfill reasoning_tokens from logprob boundaries if provider
            # didn't surface it in usage (Together reports 0).
            if not reasoning_tokens and teca.n_reasoning_tokens:
                msg_dict["_reasoning_tokens"] = teca.n_reasoning_tokens

        try:
            cost = res._hidden_params.get("response_cost", 0.0) or 0.0
        except AttributeError:
            cost = 0.0
        return msg_dict, action, cost


# Back-compat alias; original codepath used this name.
R1ReActAgent = ReasoningReActAgent
