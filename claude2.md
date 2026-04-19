# Instructions for Overthinking Analysis on ACE-AppWorld Trajectories

## Context you need before starting

You are analyzing trajectory logs produced by the ACE (Agentic Context Engineering) framework running on the AppWorld benchmark. Your job is to produce a first-pass, descriptive analysis of whether and how "overthinking" occurs in these agent trajectories.

### What "overthinking" means in this setting

Overthinking in ACE-AppWorld is *not* the same as overthinking in long chain-of-thought reasoning. This is an agent writing Python code, not a model emitting long prose. The relevant failure modes here are behavioral:

1. **Information saturation** — the agent already has the information it needs but keeps fetching more (re-calling APIs that return what it already has, re-querying data it already collected).
2. **Over-verification** — the agent inserts redundant confirmation steps before every action (repeatedly checking state, logging out and back in, re-fetching the contact list three times).
3. **Exploration divergence** — the agent starts pursuing an API or code path that is tangential to the task (the Lagos-population-UN-vs-WorldBank pattern, adapted for apps).
4. **Looping** — the agent repeatedly tries similar operations with slight variations, usually after hitting an error, without converging.
5. **Premature completion then recovery** — the agent calls complete_task() with a wrong answer, realizes after seeing the test feedback it was wrong, and continues with defensive corrections.

Critically, "overthinking" requires that **the agent already had what it needed** or **more action made things worse**. A long trajectory on a genuinely hard task is not overthinking — it's appropriate effort. Your analysis must distinguish these cases.

## Your analysis goal

Produce a structured descriptive report that answers three questions:

1. Does overthinking occur in these trajectories? If so, in what proportion?
2. What are the dominant failure modes, and in what proportions?
3. Is there a measurable relationship between trajectory length and task success (the "marginal utility" question from Zhou et al. applied at step level)?

## The analysis procedure

Work through these steps in order. Do not skip ahead — each builds on the previous.

### Step 1: Inventory and sanity check

Before any analysis, survey what you have. For each trajectory directory:

- Read `summary.json` to get: task_id, model, total_steps, TGC, SGC, success/fail.
- Read `config.json` to get: which model was used, ACE hyperparameters, playbook state.
- Count the number of entries in `log.jsonl`.
- Confirm `state_snapshots/` contains snapshots at the expected cadence.

Produce a table: one row per trajectory with columns (task_id, steps, tokens_generated, TGC, SGC, completed_cleanly). "Completed cleanly" means the agent called `complete_task()` voluntarily rather than hitting a step limit or error termination.

Flag any trajectories that are malformed (truncated logs, missing snapshots, parse errors) and exclude them from subsequent analysis. Report how many were excluded.

### Step 2: Parse each trajectory into steps

For each trajectory, iterate through `log.jsonl` and for each step extract:

- `step_index` — position in the trajectory
- `thought` or equivalent reasoning text, if logged separately
- `code` — the Python code the Generator wrote
- `output` — the REPL output after executing the code
- `api_calls` — list of APIs called in this step (parse from the code)
- `error` — whether the execution raised an error (look for tracebacks in the output)
- `tokens_in` and `tokens_out` if logged

If the log format differs from the above, adapt but document what fields you used.

Produce per-trajectory step-level tables. This is the substrate for everything else.

### Step 3: Compute trajectory-level descriptive statistics

For the full set of trajectories, compute:

- Distribution of trajectory lengths (in steps and in tokens)
- Distribution stratified by success vs. failure
- Mean/median/max number of API calls per trajectory
- Most-called APIs across all trajectories
- Fraction of steps that produced an execution error
- Mean number of error-recovery steps (steps immediately following an error)

Plot a histogram of trajectory lengths, colored by success/failure. This alone will tell you a lot: if failed trajectories are systematically longer than successful ones at matched task difficulty, that's initial evidence of overthinking.

### Step 4: Measure the overthinking signature

This is the core analytical step. For each trajectory, compute four signals. Note each has to be adapted to the AppWorld code-execution setting — there is no "candidate answer" running throughout like in math reasoning.

**Signal A — Information saturation (redundant API calls).**

For each trajectory, count:
- The number of times an API was called where that exact API with identical or nearly-identical arguments had been called earlier in the trajectory and returned successfully.
- The number of "read-only" API calls (e.g., `show_api_doc`, `show_app_descriptions`, listing/getting operations) that occur *after* the first state-modifying action.

Report: `saturation_ratio = redundant_calls / total_api_calls`.

A high ratio, especially for failures, is evidence of saturation overthinking.

**Signal B — Action repetition / looping.**

For each trajectory, compute the action sequence — the list of (api_name, simplified_args) tuples in order. Then detect:
- N-gram repetition (same 2-gram or 3-gram of actions appearing ≥3 times)
- "Stuck loops" — subsequences where the same API is called ≥3 consecutive times with only superficial argument changes

Report: `loop_fraction = steps_in_detected_loops / total_steps`.

**Signal C — Progress stagnation.**

A proxy for "the agent isn't getting anywhere": over windows of 5 consecutive steps, does the state snapshot actually change? Specifically:
- Parse state_snapshots at their recorded cadence.
- For each pair of consecutive snapshots, compute a rough diff (new variables created, new API calls made, new data fetched).
- Flag windows where the diff is near-empty but the step counter advanced.

Report: `stagnation_fraction = stagnation_windows / total_windows`.

This signal is coarser because snapshots are every 10 steps, but it's a useful cross-check on signals A and B.

**Signal D — Late-trajectory behavior.**

Compare the behavior pattern in the *second half* of the trajectory to the *first half*. Specifically:
- Are new APIs being discovered (first half should have more novel API calls)?
- Is the code producing new outputs (second half often repeats patterns)?
- Is the error rate different?

This signal looks for the pattern where the agent front-loads useful work and back-loads unproductive work — a hallmark of overthinking.

For each signal, report the distribution across trajectories. Then report Spearman correlations between each signal and trajectory success (binary).

### Step 5: Classify each trajectory

Using the signals from Step 4, assign each trajectory to one of five categories. These adapt Zhou et al.'s four-category scheme to the agent setting:

1. **Clean success** — succeeded with low signals on all four overthinking measures.
2. **Lucky success** — succeeded despite high signals on ≥2 overthinking measures. (The agent overthought but got the answer anyway.)
3. **Overthinking failure** — failed, and the failure is preceded by or associated with high overthinking signals. The task appeared achievable earlier in the trajectory.
4. **Clean failure** — failed with low signals on all overthinking measures. The agent tried reasonable things and just couldn't do the task.
5. **Ambiguous** — doesn't fit cleanly in the above.

Thresholds for "high" signal: use the upper quartile of each signal's distribution as the initial cutoff. These are arbitrary; report what threshold was used and do a sensitivity analysis at two other cutoffs.

Report the proportion of trajectories in each category. **The overthinking-failure rate is the headline number for the pilot.** If it's above ~10%, the phenomenon is clearly present and the project is worth pursuing. If below ~5%, the setup is not eliciting enough overthinking.

### Step 6: Qualitative inspection of overthinking failures

Randomly sample 10-20 trajectories from category 3 (overthinking failures). For each:

- Read the full log.
- Write a 2-4 sentence summary of what the agent was trying to do and where it went wrong.
- Classify the specific failure mode using the five categories from the "What overthinking means" section above (saturation, over-verification, exploration divergence, looping, premature-completion-recovery).
- Estimate the earliest step at which the agent *could* have successfully completed the task with the information it already had.

Report:
- Proportions across failure modes (similar to Zhou et al.'s Table 7).
- Representative examples — 3 short excerpts showing the most common pattern.
- Average "wasted steps" — total_steps minus earliest-possible-completion-step.

### Step 7: Cross-cutting analyses

Run these checks to characterize the phenomenon more precisely.

**Length vs. success.** Plot success rate (y) vs. trajectory length bin (x). Fit a curve. Does success rate monotonically decrease with length, or does it peak at some middle length and then decline? A non-monotonic curve is the ACE-AppWorld analog of Zhou et al.'s Figure 1.

**Task-difficulty stratification.** AppWorld tasks come in difficulty levels (normal vs. challenge). Repeat the length-vs-success analysis stratified by difficulty. Zhou et al.'s finding was that optimal budget varies 7.5× across difficulty — test whether the same structure appears here.

**Cross-snapshot answer stability.** For each pair of consecutive state snapshots, ask: if we forced the agent to call `complete_task()` with a best-guess answer at each snapshot, would the answer have changed? This requires some heuristic — for tasks with an obvious answer variable, track that variable across snapshots; for others, you may need to skip. This is the closest analog to Zhou et al.'s flip events in this setting.

**Playbook effect.** Trajectories with a longer, more-developed ACE playbook should (per the ACE paper) overthink less. Test: correlate playbook size (from config.json) with overthinking signals from Step 4. If the correlation is negative, ACE itself is partly mitigating overthinking, which is a finding worth noting.

### Step 8: Report

Write a structured report with the following sections:

1. **Setup**: number of trajectories analyzed, models, any exclusions.
2. **Top-line numbers**: success rate, mean length, overthinking-failure rate.
3. **Signal analysis**: distributions of the four signals, correlations with success.
4. **Category breakdown**: proportions across the 5 trajectory categories.
5. **Failure mode breakdown**: proportions from the qualitative inspection.
6. **Length-vs-success curve**: with and without difficulty stratification.
7. **Representative examples**: 3 short, anonymized excerpts.
8. **Preliminary conclusions**: what failure modes are most common, which signals track them best, what this implies for a prediction-and-intervention follow-up project.
9. **Limitations and caveats**: what this analysis can and cannot conclude.

## Specific instructions for how to behave during analysis

### Be honest about uncertainty

Every signal above is a proxy. A "redundant API call" might actually be intentional re-fetching when state has changed. A "loop" might be legitimate retry logic. An "overthinking failure" might actually be a model-capability failure dressed up as overthinking. Flag these ambiguities.

Do not round a 4.8% overthinking-failure rate up to "about 5%, which meets the threshold." Report the number, report the threshold, report the conclusion.

### Do not hallucinate

If the log format differs from what's assumed here, stop and describe what's actually there. Do not fabricate fields or infer ones that aren't present.

If a trajectory doesn't parse cleanly, say so and exclude it rather than guessing.

If the number of trajectories is too small for the statistics (e.g., fewer than 30), report the numbers but explicitly flag statistical power as a limitation.

### Use code, not prose, for the heavy lifting

The analysis should be driven by actual computation on the logs, not by reading and summarizing. Write Python that:
- Walks the trajectory directories.
- Parses each log.jsonl.
- Computes the signals mechanically.
- Aggregates into tables and plots.
- Produces the statistics that feed the report.

The prose report should summarize what the computation found, not replace it. Include the code (or reference to it) in the analysis artifacts.

### Keep the first pass descriptive

This is a pilot analysis. Do not propose models, detectors, or interventions yet — those come after we know the phenomenon exists and what it looks like. The output of this analysis should be a yes-or-no on "does overthinking happen here?" plus a qualitative characterization. It should not contain any claim of the form "our method beats baseline X."

### When to escalate

Stop and ask for clarification rather than guessing if:

- The log.jsonl format doesn't match the expected (thought, code, output) structure.
- There are fewer than 20 trajectories available (too few for meaningful statistics).
- The trajectories appear to come from a mix of models or task types that shouldn't be pooled.
- The success-rate numbers in summary.json disagree with what you compute from the log.
- Any of Steps 1-3 produces a result that's internally inconsistent (e.g., step counts that don't match between summary and log).

## What success looks like

At the end of this analysis, someone reading the report should be able to answer:

- Does overthinking happen in ACE-AppWorld? (yes / no / partially, with a number)
- What does it look like most often? (dominant failure mode with a percentage)
- How often does it cost the agent the task? (overthinking-failure rate)
- Is there a measurable signal for it? (which of the four proxies correlated best)
- Is the phenomenon worth building detectors for? (a recommendation based on the numbers)

If the report gives clear answers to these five questions, backed by statistics from the logs, the pilot is successful — regardless of whether the phenomenon turns out to be common or rare. Both outcomes are informative.

## One more note

If the pilot shows the phenomenon is strongly present (overthinking-failure rate >10%, or clear length-vs-success inverse relationship), the next phase is:
- Build detectors that can predict overthinking from features of the first K steps.
- Build interventions (forced completion, redirection, loop-breaking).
- Measure the effect of interventions on success rate and compute.

If the phenomenon is weakly present or absent, the next phase is to either:
- Try a different benchmark (Terminal-Bench is the natural alternative) or
- Study why ACE specifically is resistant to overthinking (which would itself be a paper).

Either way, the pilot is the gate. Run it rigorously.