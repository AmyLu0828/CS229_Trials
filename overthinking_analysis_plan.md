# Overthinking Analysis Plan — ACE / AppWorld (run: `appworld_playbook_gpt54_60`)

This document is the executable plan for the pilot descriptive analysis of
overthinking in the ACE-AppWorld trajectories currently checked into the
repository. It supersedes the generic procedure in `claude2.md` where the two
disagree; where they do not, `claude2.md` remains the philosophical backstop.

The goal of this pilot is to answer three questions, in order of priority:

1. **Does overthinking occur** in these 57 trajectories? If so, how often?
2. **What are the dominant failure modes**, and in what proportions?
3. **Is there a measurable relationship between trajectory length and task
   success** at the step and API-call level?

The output is a short (5–8 page) descriptive report + a small code artifact
(`analysis/` directory of Python scripts and one notebook) that reproduces
every number in the report from the raw logs.

---

## 0. What we actually have (do not skip this section)

This run lives in `appworld_playbook_gpt54_60/`:

```
appworld_playbook_gpt54_60/
├── config.json                     # gpt-5.4, temperature=0, state_type=playbook
├── summary.json                    # per-task outcomes, scores, windowed accuracy
├── log.jsonl                       # 57 lines, ONE PER TASK (not per step)
└── state_snapshots/
    ├── step_000000.json            # ACE playbook at task 0
    ├── step_000010.json            # ... task 10 ...
    ├── step_000020.json
    ├── step_000030.json
    ├── step_000040.json
    ├── step_000050.json
    └── step_000056.json            # final playbook
```

### 0.1 `log.jsonl` schema (verified by inspection)

Each line is a JSON object with these top-level keys:

| key | meaning |
|---|---|
| `step` | **global task index (0–56)**, NOT intra-task step |
| `task_id` | AppWorld task id, e.g. `50e1ac9_1` |
| `task_description` | natural-language task |
| `task_meta` | `{task_id, description, split}` — `split` is `dev` for all 57 |
| `context_retrieved` | the ACE playbook string passed into this task |
| `trajectory` | **single string** containing the full multi-turn transcript |
| `result` | `{outcome: success|failure, score: float, details: str}` |
| `reflection_text` | the ACE reflector's post-hoc notes about this task |
| `state_stats` | playbook-size bookkeeping (bullets, tokens, updates) |
| `timing` | `{retrieve_s, generate_s, reflect_s, update_s}` (per task, not per step) |

### 0.2 `trajectory` string format

Turns alternate with explicit uppercase delimiters:

```
USER:
<supervisor prompt + task + playbook + any later Output blocks>

ASSISTANT:
<optional natural-language plan>
Code:
```python
<code>
```

USER:
Output:
```
<REPL output>
```

ASSISTANT:
...
```

Empirical counts (from a quick sweep of all 57 tasks):

- `ASSISTANT:` turns per task: **min 41, median 46, max 59** (≈ 46.8 mean)
- ```` ```python ```` blocks per task: min 44, median 49, max 62
- Trajectory length in characters: 38,896 – 119,976 (median ~80k)
- `complete_task(` textual mentions per task: 8–27 (the 8 "floor" is from the
  system prompt mentioning the API name, not real invocations — see §2.1)

**Implication:** every trajectory is already long by math-reasoning standards.
A 45-step trajectory for a task that a human would solve in ≤5 steps is our
prior for "overthinking is plausibly here".

### 0.3 `state_snapshots/` — read this before using the word "state"

These files are **ACE playbook snapshots between tasks**, not intra-task
environment state. They show how the playbook grows over the 57 tasks. They
are useful for the playbook-effect analysis (§4.3) but they do **not** let us
ask "did the agent's state change within a task" — that would require parsing
the trajectory itself, which is what §2 does.

The `claude2.md` "Signal C — stagnation from state snapshots" therefore has
to be redefined at the intra-trajectory level (§2.4).

### 0.4 Sample-size reality check

57 tasks, 49 successes, 8 failures. This is below the 20-failure threshold
`claude2.md` sets for reliable statistics. We will:

- Report all numbers, but never round "4.8%" into "about 5%".
- Use **bootstrap CIs** (10k resamples) for every rate reported.
- Treat the failure analysis as **primarily qualitative**; the quantitative
  headline is the length-vs-success curve over all 57 tasks and the
  distribution of the four overthinking signals, not a statistical test on
  8 failures.
- Flag "statistical power" as an explicit limitation in the report.

If the phenomenon is borderline, the correct next step is to run a larger
ACE-AppWorld sweep before drawing conclusions — not to strain a 57-task set.

---

## 1. Definitions (ACE-AppWorld–specific)

Overthinking here means **the agent already had what it needed, or more action
made things worse**. Five concrete failure modes, adapted from `claude2.md`:

| mode | operational definition in this log format |
|---|---|
| **Information saturation** | same API called with near-identical args after it already returned successfully; or read-only `show_*` calls after the first state-modifying action |
| **Over-verification** | repeated state-inspection calls interleaved with the real work; e.g. listing the contact book three times before sending one message |
| **Exploration divergence** | agent pursues an API / code path orthogonal to the task (e.g. querying a different app, re-checking auth, computing an auxiliary metric) |
| **Looping** | ≥3 consecutive steps calling the same API with only superficial arg changes, typically after an error; or the same 2- or 3-gram of actions repeated ≥3 times |
| **Premature completion → recovery** | `apis.supervisor.complete_task(...)` called once, the environment rejects or warns, agent continues with defensive corrections |

A long trajectory is **not** automatically overthinking. Distinguishing hard
task vs. overthinking is the whole point of the signals in §2.4.

---

## 2. Pipeline

Implement in this order, each module as its own file under `analysis/`.
Every step writes a `.parquet` or `.jsonl` artifact so later steps are
restartable without re-parsing.

```
analysis/
├── 00_audit.py            # sanity checks (§0) → tables/00_audit.csv
├── 10_parse_trajectories.py   # §2.1 → tables/steps.parquet
├── 20_step_features.py        # §2.2 → tables/step_features.parquet
├── 30_traj_signals.py         # §2.3–2.4 → tables/traj_signals.parquet
├── 40_classify.py             # §2.5 → tables/traj_categories.csv
├── 50_qualitative.py          # §2.6 helper: dump human-readable excerpts
├── 60_crosscuts.py            # §2.7 cross-cutting analyses
└── report/
    ├── report.md              # written by hand from the tables/plots
    └── figures/
```

### 2.1 Parse each trajectory into a step table

For each of the 57 tasks, walk the `trajectory` string and emit one row per
`ASSISTANT` turn (= one "step"). Schema:

| column | source |
|---|---|
| `task_id`, `task_idx` | from log line |
| `step_idx` | 0-based index of ASSISTANT turn within the task |
| `assistant_text` | everything between this `ASSISTANT:` and the next delimiter |
| `code` | concatenation of ```` ```python ```` blocks inside the assistant turn (empty string if no code) |
| `user_output` | the following `USER:` turn up to the next `ASSISTANT:` (what the REPL returned) |
| `output_had_error` | bool; see §2.2 below for detection rules |
| `api_calls` | list of `(app_name, api_name, arg_hash)` tuples extracted from `code` |
| `called_complete_task` | bool — the code contains `apis.supervisor.complete_task(` |
| `n_chars_assistant`, `n_chars_output` | crude size proxies |

Parsing rules:

- Split the trajectory on `(?m)^(USER|ASSISTANT):\s*$`. Do **not** split on
  `"A:"` — the real delimiter is the full word.
- Extract code with ```` ```python\n(.*?)``` ```` non-greedy, DOTALL.
- Extract API calls with a simple regex over code: `apis\.(\w+)\.(\w+)\s*\(`.
  For arg similarity, normalize args by keyword-name and literal-value hash
  (ignore variable names). Cheap but good enough for n-gram detection.
- The trajectory ends after the final ASSISTANT turn — `complete_task()` is
  the last code call in a cleanly completed trajectory.

**Verification:** after parsing, for each task assert
`n_steps ∈ [41, 59]` (matches the sweep in §0.2). If not, flag.

### 2.2 Error detection (one dedicated helper)

The traceback regex from `claude2.md` returned 0 hits on these logs, so error
detection has to be tailored. Apply the following rules to `user_output`:

1. Look for the substring `Traceback (most recent call last)` anywhere.
2. Look for lines matching `^\s*[A-Z]\w*Error(:|$)` or
   `^\s*[A-Z]\w*Exception(:|$)`.
3. Look for AppWorld-specific error hints: `ExecutionError`, `api_call_error`,
   keys like `"success": false`, `"error":`, `"message":`, inside the
   fenced `Output:` block.
4. Look for the string `Invalid` or `Failed` in combination with the
   supervisor-response sentinel produced by AppWorld when `complete_task` is
   rejected (inspect a failure trajectory manually and pin down the exact
   marker before finalizing the rule).

Before trusting the rule, **manually audit 10 trajectories** (5 success, 5
failure) and confirm the error-flag matches what a human reader would mark.
This is the single most important sanity check in the pipeline — every signal
downstream depends on it.

### 2.3 Per-trajectory descriptive stats

From the step table, compute one row per trajectory:

- `n_steps`, `n_code_blocks`, `n_api_calls`
- `n_unique_api_calls`, `distinct_apps_touched`
- `n_errors`, `error_rate = n_errors / n_steps`
- `recovery_steps` (steps whose previous step had an error)
- `step_of_first_complete_task`, `n_complete_task_calls`
- `chars_assistant_total`, `chars_output_total`
- join with `summary.json`: `outcome`, `score`, and from `result.details`
  any environment feedback on why a `complete_task` was rejected

Plot immediately:

1. Histogram of `n_steps`, colored by success/failure.
2. Histogram of `n_api_calls`, colored by success/failure.
3. Scatter of `n_steps` vs. `score`.

If successful trajectories are already bimodal — a clean-fast cluster and a
stretched-long cluster — overthinking is visible before any of the §2.4
signals are computed.

### 2.4 The four overthinking signals

Compute each as a single number per trajectory. Save alongside the descriptive
stats. None of them is definitive on its own; the joint distribution is what
matters.

**Signal A — Information saturation.**

For each trajectory, build the ordered list of
`(api_name, arg_hash)` tuples for *read-only* API calls (heuristic:
`show_*`, `get_*`, `list_*`, or any call that does not appear in a curated
"mutating" list — spot-check this list by hand). Count:

- `redundant_calls` = number of read-only API calls whose `(api, arg_hash)`
  already appeared earlier in the trajectory and returned without error.
- `late_readonly_calls` = number of read-only calls that occur **after** the
  first mutating call.

Report `saturation_ratio = redundant_calls / max(1, n_api_calls)` and
`late_readonly_fraction`.

**Signal B — Action repetition / looping.**

On the ordered action sequence (one element per ASSISTANT turn, e.g.
the first API called in that turn plus its `arg_hash`):

- Count 2-gram and 3-gram repetitions (same n-gram ≥3 times in the
  trajectory).
- Detect **stuck runs**: subsequences where the same `(api, arg_hash)` appears
  in ≥3 consecutive steps.

Report `loop_fraction = steps_inside_any_detected_loop_or_run / n_steps`.

**Signal C — Progress stagnation (redefined).**

`claude2.md`'s snapshot-based stagnation does not apply because snapshots are
inter-task, not intra-task (§0.3). Replace it with a trajectory-internal
proxy:

- For each sliding window of 5 consecutive steps, compute the size of the
  **new-information set** acquired in that window: new API names seen, new
  primary-key values observed in outputs (IDs, emails, numbers, filenames,
  etc. — extract with a generic key/value regex from fenced outputs).
- A window is a **stagnation window** if the new-information set size is 0
  and no mutating API was called.

Report `stagnation_fraction = stagnation_windows / total_windows`.

**Signal D — Late-trajectory behavior.**

Split each trajectory in half (by step count). Compute for each half:

- fraction of novel API calls (first time that `(api, arg_hash)` appears in
  the trajectory)
- error rate
- mean assistant-turn length in characters

Report `novelty_drop = novel_first_half - novel_second_half` and
`error_shift = error_rate_second_half - error_rate_first_half`.

For every signal, report the distribution across all 57 trajectories and the
Spearman correlation with `score` (treating failure as score<1.0) and with the
binary `success` flag. Include bootstrap 95% CIs.

### 2.5 Classify each trajectory

Using the four signals, bin trajectories into:

1. **Clean success** — succeeded, all four signals ≤ median.
2. **Lucky success** — succeeded, ≥2 signals in the top quartile.
3. **Overthinking failure** — failed, ≥2 signals in the top quartile.
4. **Clean failure** — failed, all four signals ≤ median.
5. **Ambiguous** — anything else.

Report the counts. Then repeat with thresholds at top-20% and top-33% to show
the category proportions are not an artifact of the quartile cutoff (sensitivity
analysis).

**The overthinking-failure count out of 8 failures is the headline number for
this pilot.** State it with a bootstrap CI and resist the temptation to
translate it to "about X%" when X is fragile at this sample size.

### 2.6 Qualitative inspection

Read all 8 failures + the 5 longest successes + the 5 success trajectories
with the highest combined signal (likely the "lucky successes"). For each:

- Write 3–5 sentences: task goal, what went wrong or what looked wasteful,
  and which of the five modes from §1 it matches (one primary + any
  secondary).
- Estimate **earliest-possible-completion step**: the smallest `step_idx` at
  which the agent *had enough info to call `complete_task` correctly*. This is
  a judgment call; write one sentence of justification. Compute
  `wasted_steps = n_steps - earliest_possible_completion_step`.
- Save each excerpt to `analysis/report/excerpts/<task_id>.md` with the
  relevant span of trajectory quoted verbatim (≤60 lines).

Report: mode proportions, mean/median `wasted_steps`, and 3 short
representative excerpts pasted into the report.

### 2.7 Cross-cutting analyses

Four additional plots/tables:

**2.7.1 Length-vs-success curve.** Bin `n_steps` into quintiles; plot success
rate per bin with Wilson CIs. Overlay the mean + 1σ band. This is the
ACE-AppWorld analog of Zhou et al.'s Figure 1.

**2.7.2 Task-difficulty stratification.** AppWorld tasks carry a
`difficulty` level in benchmark metadata. `task_meta` here only has
`split=dev`, so cross-reference the public AppWorld metadata for each
`task_id` to attach `difficulty ∈ {normal, challenge}`. Repeat 2.7.1
stratified. If the `difficulty` metadata is not readily available, skip and
flag; do **not** invent a difficulty proxy.

**2.7.3 Cross-snapshot answer stability.** Pick trajectories with an identifiable
"answer variable" (e.g. the `answer = ...` assignment before
`complete_task`). Walk the trajectory and record the value that
*would have been passed* to `complete_task` at each step where a candidate
answer exists. Count the number of **flip events** (value changes from one
snapshot to the next). Report distribution; correlate with failure.

**2.7.4 Playbook effect.** Join each trajectory with the playbook state it
was given (`context_retrieved` field, plus `state_stats.total_bullets` and
`state_stats.approx_tokens` from `state_snapshots/step_XXXXXX.json`). Plot
each overthinking signal against playbook size. If the correlation is
negative, ACE is partly suppressing overthinking — that itself is a
publishable finding and worth calling out.

---

## 3. Deliverables

A single report, `analysis/report/report.md`, with these sections:

1. **Setup** — data paths, model, temperature, exclusions.
2. **Top-line numbers** — n tasks, success rate, mean/median steps,
   overthinking-failure count with CI.
3. **Signal distributions** — four histograms + Spearman correlations.
4. **Category breakdown** — the five-bucket table with sensitivity analysis.
5. **Failure mode breakdown** — proportions + 3 representative excerpts.
6. **Length-vs-success** — 2.7.1 and 2.7.2.
7. **Answer stability** — 2.7.3.
8. **Playbook effect** — 2.7.4.
9. **Preliminary conclusions** — what does and does not replicate Zhou et al.
   in this agent-tool-use setting; which signal is most useful; is building a
   detector / intervention worth doing.
10. **Limitations** — sample size, single-model, dev split only, error
    detection heuristic, judgment calls in qualitative pass.

Every number in the report must be a cell in a `.parquet`/`.csv` under
`analysis/tables/`, and every figure a file under `analysis/report/figures/`
regenerable from a single `make report` target.

---

## 4. Rules of engagement

- **No hallucinated fields.** If a field doesn't exist in the logs, don't
  invent it — either compute a proxy and call it a proxy, or skip.
- **No fabricated statistics.** 57 trajectories is small. Report bootstrap
  CIs, not p-values pretending to be decisive.
- **No prescriptive claims in the pilot.** This report says "overthinking
  happens / doesn't / sometimes"; it does **not** propose detectors,
  interventions, or baselines-to-beat. That is the next project if and only
  if this one clears the bar.
- **Code is authoritative; prose is the summary.** All signals are computed
  mechanically in `analysis/`. The report cites the table/figure; it does
  not re-compute or re-describe from memory.
- **Stop and ask for clarification if** any of the following happen during
  implementation:
  - the parser finds <41 or >59 ASSISTANT turns in some trajectory (schema
    drift);
  - the error-detection helper disagrees with the manual audit on ≥2 of the
    10 audit samples;
  - the success/failure counts in `summary.json` disagree with what's
    derivable from the trajectories (e.g., a trajectory with `result.outcome
    = "success"` but no successful `complete_task` call).

---

## 5. Success bar for the pilot

The pilot succeeds when the report answers — with statistics backed by the
tables — all five of:

1. Does overthinking occur in ACE-AppWorld?
2. What does it look like most often (dominant mode)?
3. How often does it cost the task (overthinking-failure count / total
   failures)?
4. Which of the four signals tracks it best?
5. Is the phenomenon worth building detectors for, given the sample size and
   signal strength seen here?

"Yes, and it's clearly present" and "no, this setup barely elicits it" are
**both** valid outcomes. Either way, we have a direction for the next phase.
