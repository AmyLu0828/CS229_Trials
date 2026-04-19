# Adaptive Turn Compute + Trajectory Guardrail (ATC+TG)

**Preliminary experiment plan for a training-free, inference-only, model-agnostic method that reduces test-time compute and improves accuracy of reasoning-model agents on multi-turn tool-use benchmarks.**

## 1. Motivation

Observation from our n20 run (o3-mini, ReAct, tau-bench retail, `reasoning_effort=medium`):

- **82% of output tokens are hidden reasoning**, but reasoning density E2/E3 does **not** discriminate wins from failures.
- **Per-turn reasoning varies 0–3,100 tokens**, yet `reasoning_effort` is a *global* knob.
- **38% of failures abuse `transfer_to_human_agents`** as an escape hatch.
- **Signal D (max consecutive `respond` turns)** cleanly separates success (mean 2.7) from failure (mean 5.0).

Two distinct inefficiencies:

| Inefficiency | Time scale | Remedy |
|---|---|---|
| Fixed global effort for turns of wildly different difficulty | per turn | **Adaptive Turn Compute (ATC)** — prospective allocation |
| Trajectories get stuck in detectable patterns and don't recover | across turns | **Trajectory Guardrail (TG)** — retrospective correction |

## 2. Research question

> Can a training-free, external-model-free method that (a) allocates reasoning compute per-turn via cheap uncertainty probes (ATC), and (b) detects-and-corrects trajectory-level drift (TG), match a fixed high-reasoning baseline's accuracy at lower compute, and/or exceed it at matched compute?

This extends the CS229 adaptive-compute hypothesis from the *per-question* Best-of-N level to the *per-turn* and *per-trajectory* levels, in an agentic tool-use setting.

## 3. Method

### 3.1 SATC — Self-Reported-Confidence Adaptive Turn Compute (PRIMARY)

**Rationale for choosing SATC over probe-based ATC**: in a remote-API agentic setting, K=3 probes per turn cost ~3× the wall-clock of a single commit. In per-question BoN the probes *are* candidate answers (free), but in per-turn agentic compute the probes are thrown away on every non-easy turn. SATC uses one forward pass per turn and extracts the difficulty signal from the commit itself.

For each agent turn *t*:

```
1. Instruct the model (via system-prompt addendum) to emit a
   `confidence` integer 0-10 inside the Action JSON, alongside name/arguments.
2. Pick effort(t) from the *previous* turn's confidence:
     effort(1)   = medium                           # bootstrap
     effort(t+1) = low     if conf(t) >= τ_conf_hi  # easy streak
                 = high    if conf(t) <= τ_conf_lo  # hard streak
                 = medium  otherwise
3. Single completion call at effort(t). Parse Action JSON, extract
   confidence, strip before passing action to the env.
4. Store confidence as state for turn t+1.
```

Hyperparameters: `τ_conf_hi=7`, `τ_conf_lo=3`, `default_conf=5`. Default effort on turn 1 is `medium`.

**Cost per turn:** 1 call (= baseline), plus ~10–20 extra output tokens for the confidence field. **No probe overhead.**

**Internal only**: no second LLM, no verifier, no training. The model's own self-reported confidence is the signal.

**Failure modes to watch for**:
- Miscalibration: model always reports 9–10 → signal degenerates to fixed-low. Fallback: invert threshold logic or pivot to Alternative 3.
- Non-compliance: model omits the confidence field → treated as `default_conf=5`. If >30% omit, treat as signal failure and pivot.
- Inflation: confidence rises on hard turns because the model is overconfident. Diagnose via correlation with ground-truth success in 7.1.

### 3.2 Alternatives (ablations / follow-ups, not run in the preliminary)

**Alternative 1 — K-probe ATC** (original design, now de-prioritized).
Draw K=3 probes at `effort=low`, compute action-entropy u(t), dispatch to low/medium/high. Pros: crisp statistical interpretation. Cons: 3–4× wall-clock on remote APIs. Implementation stays in `atc_agent.py` for later comparison.

**Alternative 2 — Reactive ATC (R-ATC)** (probe-free, lagged reasoning-token signal).
Use previous turn's `reasoning_tokens` count + `respond_streak` + `tool_error_in_last_obs` as a deterministic difficulty score; map to effort tier. Zero extra calls, zero extra tokens. Signal is post-hoc, relies on turn-to-turn locality. Good if SATC miscalibrates.

**Alternative 3 — Token-entropy ATC**.
For providers that expose per-token logprobs (DeepSeek R1, Claude, open models — not o-series), use mean token entropy of the committed generation as the difficulty signal for turn t+1. Zero extra calls. Not usable with o3-mini.

**Alternative 4 — K-probe ATC with parallel probes**.
Same as Alt 1 but fire K probes concurrently via threads. Wall-clock drops to ~1 call, token cost unchanged. Reinstates ATC as a candidate if SATC fails and we move to a non-o-series model.

### 3.2 TG — Trajectory Guardrail

Online drift detection + context-only recovery, wrapped around the solve loop.

**Detectors** (all pure Python over trajectory prefix; zero extra model calls):

| ID | Trigger condition |
|---|---|
| D1 — respond-loop | last `k` (default 3) assistant turns are all `respond` AND (no tool call in last `k` turns, OR 5-gram Jaccard between last 2 respond contents > 0.5) |
| D2 — premature escalation | current action is `transfer_to_human_agents` AND no mutating action attempted yet in trajectory |
| D3 — action repetition | same `(action_name, args_hash)` called ≥ 2× within last 4 turns |
| D4 — probe oscillation | ATC probe-entropy `u(t) > τ_high` for 2 consecutive turns |

**Recovery policies** (context-only, training-free):

| ID | Action |
|---|---|
| R1 — context nudge | Append a fixed system message: *"You have spent N consecutive turns without a tool call. If a tool would move the task forward, call it; otherwise provide a final conclusion."* |
| R2 — effort escalation | Bump ATC's next-turn effort to `high` |
| R3 — escalation block | Reject `transfer_to_human_agents`; re-prompt with schema listing non-escalation mutating actions |
| R4 — backtrack | Discard last `k` assistant turns, re-plan from earlier state with R2 |

**Detector → recovery mapping**:

| Detector | First firing | Repeat firing |
|---|---|---|
| D1 | R1 | R2 |
| D2 | R3 | R3 + log |
| D3 | R1 | R4 |
| D4 | R2 | R4 |

**Generality commitment**: detectors and recovery templates contain no domain-specific content. No retail-specific rules.

## 4. Hypotheses

| # | Hypothesis | Acceptance threshold |
|---|---|---|
| H1 | SATC matches B-med accuracy at fewer reasoning tokens | M-satc accuracy ≥ B-med accuracy, E1(M-satc) ≤ 0.8 × E1(B-med) |
| H2 | SATC exceeds B-med accuracy at matched compute | accuracy difference at matched E1 ≥ 10 pp |
| H3 | SATC increases *within-task* per-turn reasoning variance | var(rt within task) for M-satc > 2× var for B-med |
| **H4** | Self-reported confidence correlates with turn success | Spearman ρ(conf(t), committed-action-is-correct(t)) ≥ 0.3 on hindsight-labeled turns |
| **H5** | Drift detectors fire on most failed trajectories in hindsight | ≥ 60% of failed tasks have a detector fire before their final turn |
| **H4b** | Compliance: model emits parseable confidence | ≥ 70% of turns contain a valid 0-10 confidence field |
| H6 | TG improves accuracy without added compute | B-med+TG accuracy ≥ B-med accuracy + 10 pp, E1 ≤ 1.05 × E1(B-med) |
| H7 | SATC and TG compose additively | Accuracy(M-satc+TG) − Accuracy(M-satc) ≥ 0.7 × (Accuracy(B-med+TG) − Accuracy(B-med)) |
| H8 | Generality | On a second benchmark (airline), same detectors fire at ≥ 0.7× the retail rate |

**H5 is a gate**: run step 7.1 first; only proceed to API-spending conditions if H5 passes.

## 5. Experimental conditions

Same 20 tau-bench retail tasks (task_ids 0–19, test split, deterministic), gpt-4o-mini user simulator, max_num_steps=30.

| Run | Method | Config | ~Cost | Notes |
|---|---|---|---|---|
| B-low | fixed | effort=low | $0.60 | cheap floor |
| B-med | fixed | effort=medium | done | reuse existing n20 |
| B-med+TG | fixed + TG | effort=medium, TG on | $1.50 | isolates TG lift |
| **M-satc** | **SATC only** | **τ_hi=7, τ_lo=3** | **$1.50** | **isolates SATC** |
| **M-satc+TG** | **full method (PRIMARY)** | **SATC + TG** | **$1.50** | **the headline** |

Minimum viable set: **B-low, B-med (done), B-med+TG, M-satc, M-satc+TG**. Total ~$5 + already-done n20.

(Alternative M-atc, M-atc+TG stay implemented in `atc_agent.py` for later ablation; not run in the preliminary.)

## 6. Primary metrics

Per task:
- `reward` (accuracy)
- `E1_total_reasoning_tokens`
- `per_turn_reasoning_variance` = std of `_reasoning_tokens` across turns
- `n_detector_fires` — how many times any TG detector fired
- `n_recoveries_applied` — how many times a recovery policy ran

Aggregate:
- Accuracy (solved / total)
- Mean E1 across tasks
- Mean within-task variance
- 2D Pareto plot: (mean E1, accuracy) — one dot per run

Headline plot:
```
accuracy ▲
         │      ● M-atc+TG       ← target: strictly up-and-left
         │   ● B-med+TG            of the fixed-effort frontier
         │             ● M-atc
         │  B-low ●  ───────  ● B-med  ───────  ● B-high
         └────────────────────────────────────────▶ mean E1
```

## 7. Execution plan

### 7.1 Offline validation of detectors (GATE — no API cost)

1. Implement D1–D3 as pure functions on a trajectory prefix.
2. Replay every n20 trajectory turn-by-turn through the detectors.
3. For each **failed** task, record the earliest turn index at which any detector fired.
4. Report: fraction of failures with at least one firing pre-final-turn; distribution of fire turns; false-positive rate on successful tasks.

Proceed only if **≥ 60% of failures** have at least one detector fire before the agent's final action.

### 7.2 Implement SATCAgent (PRIMARY)

Subclass `ReasoningReActAgent`. Override `generate_next_step`:

1. Determine effort(t) from stored `_last_confidence`:
   - First turn: effort = `medium`
   - conf(t-1) >= 7: effort = `low`
   - conf(t-1) <= 3: effort = `high`
   - else: effort = `medium`
2. One completion call at effort(t). Parse Action JSON.
3. Extract `confidence` field (top-level first, then `arguments.confidence`).
   Strip it before constructing the `Action` sent to the env.
4. Default confidence to 5 if missing or un-parseable.
5. Update `_last_confidence = conf(t)` for the next turn.
6. Record `_satc_confidence`, `_satc_effort`, `_satc_conf_source` on the message dict.

Prompt addendum (appended to the base ReAct prompt):

> After choosing your next action, rate your confidence in it as an integer from 0 (pure guess) to 10 (certain this is correct). Include it as a top-level `"confidence"` field in the Action JSON, alongside `"name"` and `"arguments"`. Be honest — low confidence is a signal that more reasoning is needed on the next turn, not a failure.

### 7.2b Implement ATCAgent (already done, kept for later ablation)

K-probe variant in `atc_agent.py`. Not run in preliminary.

### 7.3 Implement TG solve-loop wrapper

New module `tg_solver.py`. `solve_with_tg(env, agent, max_num_steps)`:

1. Maintain per-trajectory drift state (last actions, respond streak, mutating-attempted flag).
2. Before each `agent.generate_next_step`, run detectors on prefix.
3. If fired, apply recovery policy (inject nudge message / bump effort flag / ...).
4. After each action, check D2 (action about to be `transfer_to_human_agents`) and apply R3 if triggered.
5. Log `_detector_fires` and `_recoveries_applied` on the result.

### 7.4 Run the minimum viable set

In order:
1. B-low (20 tasks, effort=low, no TG)
2. M-atc (ATC, no TG)
3. B-med+TG (effort=medium, TG on)
4. M-atc+TG (full method)

Each run follows the existing pipeline: `run_*.py` → log JSON → `analyze_*.py` → CSV + markdown.

### 7.5 Analyze and produce figures

- Update `make_figures.py` to overlay the 5 runs' points on a (mean E1, accuracy) plot.
- Add a table comparing detector fire counts across conditions.
- Rebuild per-turn timeline figures for M-atc+TG to show the redirected trajectories.

## 8. What falsifies the method

| Outcome | Interpretation |
|---|---|
| H5 fails at the offline gate | detectors can't catch failures in time; redesign detectors OR drop TG before any API spend |
| H4b fails (<70% parseable) | model won't emit confidence reliably; pivot to Alternative 2 (R-ATC) using lagged reasoning-token counts |
| H4 fails (ρ ≈ 0) | confidence is uncalibrated; pivot to Alternative 2 or 3 |
| B-low accuracy ≥ B-med accuracy | global effort knob is misset; SATC's allocation advantage is harder to show |
| M-satc matches B-med on both axes | adaptation adds nothing over fixed; try the K-probe ATC ablation or drop the allocator |
| M-satc+TG ≈ B-med+TG | SATC adds nothing on top of TG; simplify to TG-only |
| M-satc+TG ≈ M-satc | TG adds nothing on top of SATC; simplify to SATC-only |

Any of these is a clean, publishable negative result.

## 9. If preliminary passes

- Scale to full 115-task tau-bench retail split.
- Replicate on tau-bench airline and, if time allows, BFCL.
- Ablation: drop each TG detector individually, measure accuracy loss.
- Ablation: vary K ∈ {1, 3, 5, 7} and chart compute-vs-accuracy trade-off.
- Swap o3-mini → o4-mini, Claude Sonnet 4.5 extended-thinking, DeepSeek R1 to verify model-agnosticism.

## 10. File map

```
react_r1/
├── METHOD_PLAN.md                   # this file
├── r1_react_agent.py                # existing: ReasoningReActAgent
├── satc_agent.py                    # NEW: SATCAgent (PRIMARY method)
├── atc_agent.py                     # DONE: K-probe ATC (alternative / ablation)
├── tg_detectors.py                  # DONE: D1-D3 detector functions
├── tg_solver.py                     # DONE: solve_with_tg wrapping the env loop
├── run_r1_retail.py                 # existing: --method {fixed,satc,atc} + --tg
├── analyze_r1_retail.py             # existing: extend to log confidence + fires
├── make_figures.py                  # existing: add Pareto overlay
├── offline_validate_tg.py           # DONE: H5 gate script
└── logs/
    ├── n20-o3mini-med-retail.json   # existing (= B-med)
    ├── n20-o3mini-low-retail.json   # to produce (B-low)
    ├── n20-o3mini-med-tg.json       # to produce (B-med+TG)
    ├── n20-o3mini-satc.json         # to produce (M-satc)
    └── n20-o3mini-satc-tg.json      # to produce (M-satc+TG)
```
