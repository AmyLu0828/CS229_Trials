# Summary — smoke-o3mini-med-retail.json

- tasks: 2
- solved: 1  (accuracy = 0.500)
- mean turns: 17.00
- mean #readonly per task: 4.50
- mean #mutating per task: 2.50

## Overthinking signals (mean over tasks)
- A_late_readonly_frac: 0.583
- B_action_repetition: 0.000
- C_error_rate: 0.075
- D_max_consec_respond: 2.000
- E1_total_reasoning_tokens: 8480.000
- E2_mean_reasoning_tokens_per_turn: 518.629
- E3_reasoning_to_completion_ratio: 0.793
- E4_respond_vs_tool_reasoning_ratio: 0.000

## By outcome
### successes (n=1)
- num_turns mean: 14.000
- B_action_repetition mean: 0.000
- C_error_rate mean: 0.000
- E2_mean_reasoning_tokens_per_turn mean: 630.857
- E3_reasoning_to_completion_ratio mean: 0.818

### failures (n=1)
- num_turns mean: 20.000
- B_action_repetition mean: 0.000
- C_error_rate mean: 0.150
- E2_mean_reasoning_tokens_per_turn mean: 406.400
- E3_reasoning_to_completion_ratio mean: 0.768
