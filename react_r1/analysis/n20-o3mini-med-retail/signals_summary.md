# Summary — n20-o3mini-med-retail.json

- tasks: 20
- solved: 7  (accuracy = 0.350)
- mean turns: 12.55
- mean #readonly per task: 3.05
- mean #mutating per task: 0.85

## Overthinking signals (mean over tasks)
- A_late_readonly_frac: 0.677
- B_action_repetition: 0.000
- C_error_rate: 0.003
- D_max_consec_respond: 4.200
- E1_total_reasoning_tokens: 6211.200
- E2_mean_reasoning_tokens_per_turn: 515.985
- E3_reasoning_to_completion_ratio: 0.822
- E4_respond_vs_tool_reasoning_ratio: 0.000

## By outcome
### successes (n=7)
- num_turns mean: 13.143
- B_action_repetition mean: 0.000
- C_error_rate mean: 0.000
- E2_mean_reasoning_tokens_per_turn mean: 535.072
- E3_reasoning_to_completion_ratio mean: 0.821

### failures (n=13)
- num_turns mean: 12.231
- B_action_repetition mean: 0.000
- C_error_rate mean: 0.004
- E2_mean_reasoning_tokens_per_turn mean: 505.707
- E3_reasoning_to_completion_ratio mean: 0.822
