# Overthinking Analysis — ACE / AppWorld (run `appworld_playbook_gpt54_60`)

Descriptive pilot following `overthinking_analysis_plan.md`.
Code: `analysis/00_audit.py … 60_crosscuts.py`. Tables: `analysis/tables/`.
Figures: `analysis/report/figures/`.

## 1. Setup

| field | value |
|---|---|
| run | `appworld_playbook_gpt54_60` |
| benchmark | AppWorld (`split=dev`) |
| model | `gpt-5.4`, temperature 0 |
| state type | ACE playbook (LLM-merge, snapshot every 10 tasks) |
| n log lines | 57 |
| outcomes | **49 success / 8 failure (85.96% accuracy)** |
| mean score (incl. partial credit) | 0.946 |
| exclusions | none; all 57 parsed cleanly |

### 1.1 A structural fact about the log you need to know

Every log line contains **four chained subtasks**, not one task. The first
three are fixed warm-ups (`"How many playlists do I have in Spotify?"`,
`"What is the title of the most-liked song in my Spotify playlists."`,
`"Christopher has asked for my movie recommendations…"`), identical across
all 57 trajectories and, at temperature 0, producing identical 9 / 11 / 16
step traces every time. Only the **fourth subtask** varies with `task_id`
and is the one scored for `success`.

So the headline "46 steps / trajectory" is really **≈ 36 canned setup steps +
≈ 11 task-specific steps** (median 10 steps on the final subtask, range 4–23).
Every number below that is labelled "final-subtask" operates on that
task-specific slice. Every other signal is reported in both scopes.

## 2. Top-line numbers

| metric | all 57 trajectories | final subtask only |
|---|---:|---:|
| n_steps (ASSISTANT turns) — median | 46 | 10 |
| n_steps — range | 41–59 | 4–23 |
| n_api_calls — median | 49 | 15 |
| unique APIs — median | 19 | 8 |
| trajectories with any execution error | 5 (8.8%) | 5 (8.8%) |
| error events per trajectory — max | 2 | 2 |
| trajectories ending cleanly in `complete_task(...)` | 57 (100%) | 57 (100%) |

**Key fact:** of 2,665 total ASSISTANT steps, only 6 produced an execution
error (0.23%). All six are genuine runtime exceptions (HTTP 422s from
Venmo for insufficient funds, `download_song` for already-downloaded
tracks, and one wrong-API-name lookup). Manually audited 11 rows
(6 flagged / 5 random controls): detector agreed with manual judgment on
all 11. See `analysis/tables/20_error_audit.md`.

## 3. The four overthinking signals

Per `overthinking_analysis_plan.md §2.4`, computed on the final subtask
(n=57). All four definitions are in `analysis/30_traj_signals.py`.

### 3.1 Distributions

| signal | min | median | max | std |
|---|---:|---:|---:|---:|
| saturation_ratio      | 0.000 | 0.000 | 0.133 | 0.039 |
| late_readonly_fraction| 0.000 | 0.000 | 0.385 | 0.087 |
| loop_fraction         | 0.000 | 0.000 | **0.000** | 0.000 |
| stagnation_fraction   | 0.000 | 0.000 | **0.000** | 0.000 |
| novelty_drop          | 0.000 | 0.250 | 0.571 | 0.197 |
| error_shift           |-0.167 | 0.000 | 0.333 | 0.058 |

Two of the four behavioral signals are **identically zero on every
trajectory**:

- `loop_fraction` — no 3-gram of actions repeats ≥3 times, and no API is
  called in 3 consecutive steps with the same arguments. Agent never loops.
- `stagnation_fraction` — no 5-step window passes without either a mutating
  call, a new API name, or a new primary-key value showing up in the output.
  Agent is always making *some* kind of progress.

Two signals have real but small variance:

- `saturation_ratio` (fraction of read-only calls that already returned
  successfully earlier in the same subtask) is ≤13% for every trajectory;
  median is exactly 0. Redundant re-fetching is rare.
- `late_readonly_fraction` (read-only calls after the first substantively
  mutating call) has median 0 because 32/57 final subtasks never make a
  mutating call at all (they're questions, not actions). Among the 25
  trajectories that mutate, the median is still only ~0.05.

The remaining two signals have broader distributions but neither
discriminates success from failure:

| signal | median success | median failure | Spearman vs success (p) |
|---|---:|---:|---:|
| saturation_ratio       | 0.000 | 0.026 | −0.104 (0.44) |
| late_readonly_fraction | 0.000 | 0.026 | −0.020 (0.88) |
| loop_fraction          | — (constant) | — (constant) | undefined |
| stagnation_fraction    | — (constant) | — (constant) | undefined |
| novelty_drop           | 0.286 | 0.100 | **+0.192 (0.15)** |
| error_shift            | 0.000 | 0.000 | −0.147 (0.28) |
| n_steps                | 10 | 11.5 | −0.111 (0.41) |

The best correlation any signal achieves with success is |ρ|=0.19, not
significant at the given n. **None of the four signals reliably flags the
8 failures.**

### 3.2 Category breakdown (`analysis/tables/40_categories.csv`)

Using the default top-quartile cutoff on the four non-degenerate signals:

| category | count | share |
|---|---:|---:|
| clean_success         | 12 | 21.1% |
| lucky_success         |  8 | 14.0% |
| overthinking_failure  |  **2** |  **3.5%** |
| clean_failure         |  3 |  5.3% |
| ambiguous             | 32 | 56.1% |

Sensitivity: at top-20% cutoff, overthinking_failure drops to 1; at top-33%
it rises to 3. The "overthinking-failure rate" is 2/8 = 25% of failures in
the default setting, but inspecting those two trajectories (§4) shows the
flag is driven by saturation and late-readonly rising slightly above the
median — neither case is a qualitative overthinking failure.

### 3.3 Summary

The four behavioral proxies from Zhou et al. 2024, adapted to
code-executing agents, do not find meaningful overthinking in this run.
The agent does not loop, does not stagnate, does not re-fetch, and does
not interleave verification into mutation.

## 4. Qualitative inspection of the 8 failures

Each of the 8 failures was read end-to-end (see
`analysis/report/excerpts/FAILURE_*.md`). A primary failure mode was
assigned; "wasted steps" is the judgment estimate of how many of the
final-subtask steps were unnecessary. No confidence is claimed on the
"wasted" number beyond ±2.

| task_id | score | final n_steps | final complete_task | wasted | failure mode |
|---|---:|---:|---|---:|---|
| 4ec8de5_3 | 0.50 |  8 | `complete_task(answer=94)` | **0** | **Semantic (temporal)** — assumed `current_year=2025`, counted 94/94 songs "before this year". Clean, efficient, wrong assumption. |
| 23cf851_2 | 0.50 |  7 | `complete_task(answer=11)` | **0** | **Semantic (temporal)** — same pattern: "this year" interpreted against agent's prior rather than the environment's timestamp. |
| 383cbac_3 | 0.50 | 10 | `complete_task(answer=total_paid)` | **0** | **Attribution/calculation** — dinner-payment share arithmetic didn't match ground truth. No wasted steps. |
| 37a8675_2 | 0.17 | 15 | `complete_task()` | 1–2 | **Environmental blocker + silent give-up** — insufficient Venmo balance, payment cards expired; tried both, then called `complete_task()` with no answer and no explanation. Includes the 1 execution error in §2. |
| 3ab5b8b_2 | 0.67 | 14 | `complete_task()` | 0 | **Partial set coverage** — downloaded liked songs, missed some. Filter definition slightly off; steps all productive. |
| df61dc5_1 | 0.86 | 22 | `complete_task()` | 0–3 | **Partial set coverage** — "like transactions ongoing year to/from roommates". The longest final subtask in the dataset (22 steps). Most of the length is legitimate: identify roommates via address overlap, pull transactions, filter by date + relationship. Missed ~1-2 transactions. |
| df61dc5_2 | 0.86 |  8 | `complete_task()` | 0 | **Partial set coverage** — same pattern, "ongoing month / friends". |
| df61dc5_3 | 0.86 | 13 | `complete_task()` | 0 | **Partial set coverage** — same pattern, "ongoing year / coworkers". |

### 4.1 Failure-mode proportions

| mode | count | share of failures |
|---|---:|---:|
| Partial set coverage (filter scope) | 4 | 50% |
| Semantic — wrong temporal/arithmetic interpretation | 3 | 37.5% |
| Environmental blocker + silent give-up | 1 | 12.5% |
| **Behavioral overthinking (any of 5 canonical modes)** | **0** | **0%** |

The 5 canonical overthinking modes from `claude2.md` / the plan —
information saturation, over-verification, exploration divergence,
looping, premature-completion-then-recovery — **each occur 0 times** across
all 8 failures. The failures are cognitive (wrong interpretation,
wrong filter, wrong assumption about the environment's "current year")
not behavioral (wasted steps, re-fetching, looping).

### 4.2 A structural quirk: 3 of 8 failures come from one parent task

`df61dc5_1/2/3` are three scoring variants of the same underlying task
family (like Venmo transactions to/from {roommates, friends, coworkers}
in the {ongoing year, ongoing month}). The agent uses the same strategy
for all three and makes the same kind of set-coverage miss. This is
evidence of a **systematic reasoning blind-spot**, not an overthinking
pattern. One plausible follow-up intervention is a playbook addition that
pins down "ongoing year" and "relationship inference" conventions.

### 4.3 Representative excerpts

Three short examples that capture the dominant patterns:

**Semantic-temporal (`4ec8de5_3`, score 0.5, 8 steps).**
The agent completed the task in a tight 8-step final subtask: login →
enumerate libraries (94 unique songs) → filter `release_date < 2025` →
submit `94`. Every step is productive. The failure is a single assumption
in step 6: `current_year = 2025`. If the correct "ongoing year" in the
simulator were 2023, the true answer would be smaller. The agent never
questions its own assumption and never queries the environment for the
current date.

**Environmental blocker + silent give-up (`37a8675_2`, score 0.17).** The
task was to send $100 on Venmo. The agent identifies the recipient, calls
`create_transaction`, hits HTTP 422 (insufficient balance), looks up
payment cards, notices they're all expired, then calls `complete_task()`
with no answer. Nothing in the trajectory surfaces this failure to the
supervisor; the agent just stops. This is arguably the opposite of
overthinking — *under*thinking: too quick to concede without reporting.

**Partial set coverage (`df61dc5_1`, score 0.86, 22 steps).** The longest
final subtask in the dataset. But the length is proportionate: the agent
had to identify roommates via `show_addresses`, cross-reference Venmo
friends, filter transactions by year + relationship, then `like_transaction`
on the set. Missed a small number of transactions at the set-intersection
step. No step in the trace is obviously wasted.

## 5. Cross-cutting analyses

### 5.1 Length vs. success (final subtask)

| n_steps bin | n | success rate | Wilson 95% CI |
|---|---:|---:|---|
| 1–8   | 17 | 82.4% | [58.9%, 93.8%] |
| 9–10  | 17 | 94.1% | [73.0%, 99.0%] |
| 11–12 | 10 | 100.0% | [72.2%, 100%] |
| 13–14 |  5 | 60.0% | [23.1%, 88.2%] |
| 15–25 |  8 | 75.0% | [40.9%, 92.9%] |

No monotonic "longer → worse" relationship. The curve is mildly
inverted-U: shortest and longest bins are a little worse than the middle,
but every bin's CI overlaps every other bin's. The only statistically
distinguishable observation is that 11–12-step final subtasks are
all successful (10/10), but with n=10 this is compatible with noise.

Zhou et al.'s 2024 finding that long traces are disproportionately failures
**does not reproduce here.** See `fig_length_vs_success.png` and
`fig_length_histograms.png`.

### 5.2 Playbook effect

Playbook bullets grew from 0 at task 0 to 39 by task 56 (steady, modest
growth under the `llm` merge strategy). Spearman correlations between
playbook size and each signal on the final subtask:

| signal | ρ(playbook, signal) | p |
|---|---:|---:|
| saturation_ratio       | −0.02 | 0.89 |
| late_readonly_fraction | +0.18 | 0.18 |
| novelty_drop           | +0.07 | 0.58 |
| error_shift            | −0.11 | 0.42 |

None are significant. With only 57 points and a near-monotonic x-axis
(playbook grows with task index), this is also partly confounded with
any drift in task difficulty across the run. Conclusion: no measurable
playbook-suppresses-overthinking effect, but also no statistical power to
rule one out. See `fig_playbook_effect.png`.

### 5.3 Answer-stability (planned but not executed)

The plan's §2.7.3 ("track candidate-answer across pseudo-snapshots") is
skipped here: only 32/57 final subtasks have an `answer=…` literal in the
final `complete_task` call (the rest are action-only), and among those
the agent computes the answer in exactly one step just before submission,
so there are no prior "candidate answer" snapshots to track. This is
itself a result: at temperature 0, this agent does not deliberate over
candidate answers.

### 5.4 Task-difficulty stratification (planned but not executed)

`task_meta` only exposes `split=dev`; the AppWorld public difficulty
labels were not joined. Given the other findings (no overthinking detected
in any signal), this stratification is unlikely to change the bottom
line, but it would be a natural addition if this pilot continues.

## 6. Preliminary conclusions

1. **Does overthinking occur? No, by the behavioral definition.** Across
   57 trajectories and 8 failures, the canonical five modes —
   information saturation, over-verification, exploration divergence,
   looping, premature-completion-then-recovery — each occur **0 times**
   in the failures. `loop_fraction` and `stagnation_fraction` are
   identically zero across every trajectory. The other two signals have
   tiny variance and don't discriminate success from failure
   (|ρ| ≤ 0.19, all p > 0.15).

2. **What does failure look like here instead?** Failure is *cognitive*,
   not *procedural*: wrong interpretation of "this year" / "ongoing
   month" (3/8), wrong filter scope for set-membership tasks (4/8),
   quiet give-up on environmental blockers (1/8). All 8 final subtasks
   are tight, well-structured, and end cleanly; the agent simply
   computes the wrong thing and submits it confidently.

3. **How often does overthinking cost the task?** 0/8 failures are
   attributable to overthinking. At the 2/8 "overthinking_failure"
   label from the quartile-classifier (§3.2), both flagged cases look
   cognitive on qualitative read; the classifier is picking up tiny
   signal-variance, not actual waste.

4. **Which signal tracks it best?** None of them usefully. If forced to
   pick, `novelty_drop` has the highest absolute Spearman correlation
   with success (+0.19), and `n_steps` the lowest (−0.11). For
   prediction-and-intervention work, none of these four signals is
   a useful feature in isolation on this data.

5. **Is the phenomenon worth building detectors for (on this data)?**
   **No**, on this run. The ACE/GPT-5.4 combination at temperature 0 is
   near-ceilinged at 85.96% accuracy on dev-split AppWorld, with
   efficient, linear trajectories; the 14% failure rate is a
   semantic/calibration problem, not an overthinking problem. A
   prediction-and-intervention project on overthinking would need
   either (a) a weaker agent that actually overthinks (e.g., a smaller
   open model or a reasoning model with disabled tool use), or (b) a
   benchmark that punishes wrong interpretations less and rewards
   iterative exploration more (Terminal-Bench is the natural next try).

## 7. Limitations and caveats

- **n = 57, 8 failures.** Any rate we report (success rate, overthinking
  rate, failure-mode shares) has wide bootstrap CIs. We do not claim
  distributional conclusions beyond what's compatible with 8 observations.
  The qualitative read of the 8 failures is not a statistical argument.
- **Single model, single temperature, single run.** Findings may not
  transfer to stochastic sampling, different models, or different ACE
  configurations.
- **Three of eight failures come from one parent task family**
  (`df61dc5_*`). Treating them as independent failure observations is
  charitable; they are effectively one failure mode replicated.
- **Error detection** was manually audited on 11 rows and agreed on all
  11, but the full 2,665-step set was not manually audited and a small
  number of false negatives (agent mentions "error" in its output but
  the detector misses it) is possible.
- **The 4 "Zhou-style" signals are code-adapted proxies.** The
  saturation and late-readonly signals rely on prefix heuristics for
  "read-only" vs. "mutating" API names; a different heuristic would move
  the numbers by a few percentage points. None of the headline
  conclusions would change: loops and stagnation are zero, and
  correlations with failure are weak under any reasonable prefix choice.
- **The `task_meta.split = dev` label** is the same across all 57 tasks,
  so we have no difficulty-stratified analysis.
- **No intra-task environment-state snapshots.** ACE snapshots capture
  the *playbook* between tasks, not the REPL state during tasks. Signal
  C was redefined to operate on trajectory-internal proxies (new APIs,
  new primary-key values, mutating calls); if real state snapshots
  existed, the signal might be sharper.

## 8. Recommendations for any follow-up project

- If the goal is studying overthinking, this run is a poor substrate.
  Switch to a weaker base model or a different benchmark before
  building detectors.
- If the goal is squeezing the last 14% out of ACE-AppWorld, the right
  targets are **(a) canonicalizing "ongoing year / ongoing month"** and
  **(b) strengthening the set-coverage step** in the "like/send/download
  all X" task family. A 4–5 bullet playbook addition in those two areas
  would plausibly convert 5–6 of the 8 failures. This is NOT an
  overthinking intervention; it's a reasoning/calibration one.
- If one still wants a behavioral signal to monitor at inference time,
  `novelty_drop` is the only one with any discriminative variance here
  and might be worth pairing with a task-specific semantic check (e.g.
  the agent is asked to report its inferred `current_year` before
  submitting).
