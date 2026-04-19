# Zero-cost uncertainty signals — ranked

Signals computed on the existing n20 baseline-medium log. No new API calls.

- Total assistant turns: 251
- Failed tasks: 13/20

## Per-signal predictive power

- `rho(S, rt_next)`: Spearman vs NEXT turn's reasoning tokens (turn-level difficulty)
- `pb(S, fail)`: point-biserial vs trajectory failure (drift-level signal)

| rank | signal | ρ(S, rt_next) | pb(S, fail) | combined |
|---|---|---|---|---|
| 1 | `S14_cond_count` | +0.015 | -0.456 | 0.471 |
| 2 | `S1_respond_streak` | -0.211 | +0.239 | 0.450 |
| 3 | `S18_alt_markers` | -0.182 | +0.108 | 0.290 |
| 4 | `S8_last_obs_len` | +0.120 | -0.143 | 0.263 |
| 5 | `S6_distinct_tools` | -0.050 | -0.212 | 0.263 |
| 6 | `S15_alt_count` | +0.083 | +0.178 | 0.260 |
| 7 | `S3_turns_ratio` | -0.203 | -0.025 | 0.229 |
| 8 | `S21_rt_over_ct_prev` | +0.198 | -0.023 | 0.221 |
| 9 | `S5_mutating_attempted` | -0.193 | -0.027 | 0.220 |
| 10 | `S19_question_density` | -0.112 | +0.093 | 0.204 |
| 11 | `S4_tool_errors` | -0.004 | +0.192 | 0.196 |
| 12 | `S11_user_clarif` | +0.098 | +0.096 | 0.195 |
| 13 | `S13_instr_len` | +0.057 | +0.124 | 0.181 |
| 14 | `S17_hedge` | -0.059 | -0.068 | 0.127 |
| 15 | `S12_error_kw` | -0.077 | +0.048 | 0.125 |
| 16 | `S10_user_frustration` | -0.059 | +0.055 | 0.114 |
| 17 | `S16_rt_prev` | +0.037 | -0.064 | 0.101 |
| 18 | `S20_ct_prev` | +0.021 | -0.077 | 0.098 |
| 19 | `S7_info_overlap` | +0.038 | +0.008 | 0.045 |
| 20 | `S2_action_repetition` | +0.000 | +0.000 | 0.000 |

## Leave-one-task-out logistic (top-5 signals: ['S14_cond_count', 'S1_respond_streak', 'S18_alt_markers', 'S8_last_obs_len', 'S6_distinct_tools'])

- per-task failure-prediction AUC: **0.560**
  - 0.5 = random; >=0.70 = useful; >=0.80 = strong

## Interpretation guide

- |ρ| ≥ 0.25 → usable turn-level difficulty signal
- |pb| ≥ 0.20 → usable trajectory-level drift signal
- combined ≥ 0.40 → good dual-purpose signal