"""
Trajectory-Guardrail solve loop.

Wraps an agent's per-turn step with online drift detection (D1/D2/D3) and
context-only recovery (R1: nudge, R2: escalate effort, R3: reject escalation).
Purely internal — no extra model, no training.

Usage:
    agent = ReasoningReActAgent(...) or ATCAgent(...)
    result = solve_with_tg(env, agent, max_num_steps=30, tg_on=True)
    # result.messages carries the full traj with _tg_* metadata.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from tau_bench.types import Action, SolveResult, RESPOND_ACTION_NAME

from tg_detectors import (
    run_detectors,
    parse_action,
    ESCALATION_ACTIONS,
)

# Retail-specific mutating set; for airline, pass a different set.
RETAIL_MUTATING = {
    "cancel_pending_order",
    "exchange_delivered_order_items",
    "modify_pending_order_address",
    "modify_pending_order_items",
    "modify_pending_order_payment",
    "modify_user_address",
    "return_delivered_order_items",
    "transfer_to_human_agents",
}

NUDGE_TEMPLATE_D1 = (
    "[TG nudge] You have produced {k} consecutive assistant turns without a "
    "tool call. If a tool call would move the task forward, call it now. "
    "If the task is done or blocked, say so and call transfer_to_human_agents "
    "only as a last resort."
)

NUDGE_TEMPLATE_D3 = (
    "[TG nudge] You have repeated the action '{name}' {n} times with the "
    "same arguments in the last few turns. If it is not producing progress, "
    "vary the action or its arguments, or conclude the task."
)

REJECT_TEMPLATE_D2 = (
    "[TG intervention] You attempted to escalate to a human agent without "
    "first trying any mutating action for this task. Reconsider: inspect the "
    "available mutating tools in the system prompt (e.g. modify_*, cancel_*, "
    "exchange_*, return_*) and decide whether one of them applies. If none "
    "apply, state explicitly which policy blocks the request before escalating."
)


@dataclass
class TGState:
    mutating_names: set = field(default_factory=lambda: set(RETAIL_MUTATING))
    d1_fires: int = 0
    d2_fires: int = 0
    d3_fires: int = 0
    recoveries: List[Dict[str, Any]] = field(default_factory=list)
    prev_fired_d1: bool = False
    prev_fired_d3: bool = False


def _attempt_tg_before(
    messages: List[Dict[str, Any]], state: TGState
) -> Optional[Dict[str, Any]]:
    """Run D1/D3 on the prefix (before the agent's next call). If fired,
    return a nudge message to append and return None-action (agent will
    re-generate). Returns None if nothing fires.
    """
    fires = run_detectors(
        traj_prefix=messages,
        proposed_action=None,
        mutating_names=state.mutating_names,
    )
    # keep only D1 / D3 here; D2 is checked on the proposed action
    relevant = [f for f in fires if f["id"] in ("D1", "D3")]
    if not relevant:
        state.prev_fired_d1 = False
        state.prev_fired_d3 = False
        return None

    # Choose primary: D3 > D1 (repetition is stronger signal than just respond-streak).
    primary = next((f for f in relevant if f["id"] == "D3"), relevant[0])
    if primary["id"] == "D1":
        state.d1_fires += 1
        # R1 first time, R2 (effort escalation) on repeat firing
        if state.prev_fired_d1:
            nudge = NUDGE_TEMPLATE_D1.format(k=primary["streak"]) + (
                " Also: increase your reasoning for this turn."
            )
            recovery_id = "R2"
        else:
            nudge = NUDGE_TEMPLATE_D1.format(k=primary["streak"])
            recovery_id = "R1"
        state.prev_fired_d1 = True
    else:  # D3
        state.d3_fires += 1
        nudge = NUDGE_TEMPLATE_D3.format(
            name=primary["name"], n=primary["repeats"]
        )
        recovery_id = "R1"
        state.prev_fired_d3 = True

    return {
        "nudge": {"role": "user", "content": nudge},
        "detector": primary,
        "recovery_id": recovery_id,
    }


def _attempt_tg_on_action(
    messages: List[Dict[str, Any]], proposed_action: Dict[str, Any], state: TGState
) -> Optional[Dict[str, Any]]:
    """Run D2 on the proposed action. If fired, return a re-prompt message."""
    if proposed_action is None:
        return None
    if proposed_action.get("name") not in ESCALATION_ACTIONS:
        return None
    fires = run_detectors(
        traj_prefix=messages,
        proposed_action=proposed_action,
        mutating_names=state.mutating_names,
    )
    d2 = next((f for f in fires if f["id"] == "D2"), None)
    if not d2:
        return None
    state.d2_fires += 1
    return {
        "reprompt": {"role": "user", "content": REJECT_TEMPLATE_D2},
        "detector": d2,
        "recovery_id": "R3",
    }


def solve_with_tg(
    env: Any,
    agent: Any,
    task_index: Optional[int] = None,
    max_num_steps: int = 30,
    tg_on: bool = True,
    max_tg_reprompts_per_turn: int = 1,
) -> SolveResult:
    """Drop-in replacement for agent.solve() with optional TG wrapping."""
    response = env.reset(task_index=task_index)
    reward = 0.0
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": agent.prompt},
        {"role": "user", "content": response.observation},
    ]
    total_cost = 0.0
    info: Dict[str, Any] = {}
    state = TGState()

    step = 0
    while step < max_num_steps:
        step += 1

        if tg_on:
            pre = _attempt_tg_before(messages, state)
            if pre is not None:
                # Append the nudge as a user message; the agent's next call
                # sees it in context and has a chance to act differently.
                nudge_msg = pre["nudge"]
                nudge_msg["_tg_nudge"] = True
                nudge_msg["_tg_detector"] = pre["detector"]
                nudge_msg["_tg_recovery"] = pre["recovery_id"]
                messages.append(nudge_msg)
                state.recoveries.append(
                    {"step": step, "detector": pre["detector"],
                     "recovery_id": pre["recovery_id"]}
                )

        message, action, cost = agent.generate_next_step(messages)

        if tg_on:
            proposed = {
                "name": action.name,
                "arguments": action.kwargs,
            }
            reprompt = _attempt_tg_on_action(messages, proposed, state)
            if reprompt is not None:
                # Inject reprompt and call agent once more before taking the step.
                # We do this at most `max_tg_reprompts_per_turn` times.
                for _ in range(max_tg_reprompts_per_turn):
                    # Do NOT commit the original escalation message; discard it.
                    rp_msg = reprompt["reprompt"]
                    rp_msg["_tg_reprompt"] = True
                    rp_msg["_tg_detector"] = reprompt["detector"]
                    rp_msg["_tg_recovery"] = reprompt["recovery_id"]
                    messages.append(rp_msg)
                    state.recoveries.append(
                        {"step": step, "detector": reprompt["detector"],
                         "recovery_id": reprompt["recovery_id"]}
                    )
                    message, action, c2 = agent.generate_next_step(messages)
                    total_cost += c2
                    # If the re-prompt succeeded (no longer escalation), break.
                    if action.name not in ESCALATION_ACTIONS:
                        break
                    # Still escalating — fall through; we've already bumped d2_fires once.

        total_cost += cost
        response = env.step(action)
        obs = response.observation
        reward = response.reward
        info = {**info, **response.info.model_dump()}
        if action.name != RESPOND_ACTION_NAME:
            obs = "API output: " + obs

        message_with_tg = dict(message)
        messages.append(message_with_tg)
        messages.append({"role": "user", "content": obs})

        if response.done:
            break

    # Annotate final messages with TG summary in info.
    info["_tg_stats"] = {
        "d1_fires": state.d1_fires,
        "d2_fires": state.d2_fires,
        "d3_fires": state.d3_fires,
        "recoveries": state.recoveries,
        "tg_on": tg_on,
    }

    return SolveResult(messages=messages, reward=reward, info=info)
