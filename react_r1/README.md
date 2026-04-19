# ReAct + DeepSeek-R1 on τ-bench retail — overthinking probe

This directory runs a classical ReAct agent backed by **DeepSeek-R1** (via
OpenRouter) on the **τ-bench retail** domain, and analyzes the resulting
trajectories for overthinking — reusing the Signals A–D from the AppWorld
ACE analysis and adding reasoning-channel signals E1–E4 that are only
meaningful for a thinking model.

## Why this configuration

| Knob                  | Setting                          | Rationale                                                                 |
|-----------------------|----------------------------------|---------------------------------------------------------------------------|
| Benchmark             | τ-bench retail                   | Multi-turn, irreversible actions, published reasoning-model numbers       |
| Agent strategy        | ReAct (text-only Thought/Action) | Classical, model-agnostic, lets us see R1's emitted reasoning directly    |
| Model                 | `deepseek/deepseek-r1` via OpenRouter | Frontier reasoning model; fully open reasoning channel                  |
| User simulator        | `openai/gpt-4o-mini` via OpenRouter | Cheap, stable user role-play                                          |
| Decoding              | temperature=0.0 (model-default)  | Comparable to your ACE run                                                |

τ-bench v1's ReAct agent already prompts the model in the canonical
"Thought: / Action: {JSON}" format. Our subclass (`r1_react_agent.py`)
adds `include_reasoning=True` to each LiteLLM call so R1's hidden chain of
thought is captured in the trajectory separately from the parsed Action.

## Prerequisites

1. Install (done once, already done in this workspace):
   - `external/tau2-bench/.venv/` via `uv sync` from `sierra-research/tau2-bench`
   - `tau-bench-v1` pip-installed into that venv (`sierra-research/tau-bench`)
2. OpenRouter API key.

## Cost

At OpenRouter's R1 prices (input $0.70/M, output $2.50/M), a typical retail
task burns ~50–100k output tokens (most of them reasoning). Expected bill:

| Scope                        | Agent cost | User-sim cost | Total   |
|------------------------------|-----------:|--------------:|--------:|
| 2-task smoke test            | ~$0.15     | ~$0.01        | ~$0.20  |
| 20-task dev slice            | ~$1.50     | ~$0.10        | ~$2     |
| 115-task full retail         | ~$8–12     | ~$0.50        | ~$10–15 |

Numbers are order-of-magnitude; R1's reasoning length varies 3x across tasks.

## Run

```bash
cd /Users/amy/Desktop/CS229_trial1
export OPENROUTER_API_KEY=sk-or-...

# Smoke test (recommended first): 2 tasks
external/tau2-bench/.venv/bin/python react_r1/run_r1_retail.py --num-tasks 2

# Dev slice: 20 tasks
external/tau2-bench/.venv/bin/python react_r1/run_r1_retail.py --num-tasks 20

# Full retail (115 tasks, one trial)
external/tau2-bench/.venv/bin/python react_r1/run_r1_retail.py --num-tasks -1

# Pass^k (4 trials) on just 20 tasks, so we can compute stability
external/tau2-bench/.venv/bin/python react_r1/run_r1_retail.py \
    --num-tasks 20 --num-trials 4 --max-concurrency 4
```

Outputs land in `react_r1/logs/<run-name>.json`.

## Analyze

```bash
external/tau2-bench/.venv/bin/python react_r1/analyze_r1_retail.py \
    react_r1/logs/<run-name>.json
```

Produces `react_r1/analysis/<run-name>/`:

- `per_task.csv`       — one row per task with all signals
- `per_turn.csv`       — one row per assistant turn (for scatter plots)
- `signals_summary.md` — human-readable summary, split by success/failure
- `excerpts/task_<id>.md` — full reasoning dump for each task, for qualitative
                            review

## Overthinking signals

**A — Late-trajectory read-only fraction** *(unchanged from AppWorld analysis)*:
among tool calls in the last 50 % of the trajectory, fraction that are
read-only (`get_*`, `find_*`, `list_*`, `calculate`, `think`). High = still
gathering info instead of acting = Cuadron et al.'s *Analysis Paralysis*.

**B — Action repetition** *(unchanged)*:
fraction of assistant turns whose `(action_name, args)` is identical to the
preceding turn. Measures tight loops; a variant of *Rogue Actions* when the
agent re-tries without incorporating the observation.

**C — Error rate** *(adapted)*:
fraction of assistant turns immediately preceded by a user/tool observation
that begins with "Error". R1 ignoring error feedback and repeating = rogue
actions.

**D — Max consecutive `respond` turns** *(new)*:
longest run of back-to-back `respond` actions without any tool use. τ-bench
penalizes this (the user simulator can spin forever). Also a form of
*Premature Disengagement*.

**E1 — Total reasoning tokens per task.** Raw compute in the thinking
channel.

**E2 — Mean reasoning tokens per turn.** Per-step thinking budget.

**E3 — Reasoning : completion ratio.** How much the model thinks relative
to how much it says. ThinkBrake-style signal.

**E4 — `respond`-turn vs. tool-turn reasoning ratio.** If R1 thinks
dramatically longer on `respond` turns than on tool-call turns, that's the
"2+3=?" pattern — deliberating over what to say after the work is done.

## Comparing to the ACE/GPT-5.4 result

The ACE/AppWorld run had no reasoning channel (temperature 0, non-thinking
model), so Signal E is novel here. Expected direction: R1 should have
*much* higher E2 and E3 than the ACE baseline, and the correlation of
`E2 × C_error_rate` with failure should be steeper than for GPT-5.4.
Cuadron et al. predict reasoning models have 3× the overthinking rate of
non-reasoning models; this is the natural test of that on a new benchmark.
