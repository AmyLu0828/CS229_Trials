# CLAUDE.md

This file provides guidance to Claude Code (or any AI coding assistant) when working with this repository.

## Project overview

**Adaptive Compute Allocation for Small Language Models**

A CS229 research prototype that tests whether small LLMs (0.5B–3B parameters) can approximate larger models at inference time by spending test-time compute adaptively — more samples on hard questions, fewer on easy ones — instead of uniformly.

The core hypothesis: fixed-N Best-of-N sampling wastes compute because most questions don't need N samples to resolve. An adaptive allocator that uses the model's own uncertainty signals to choose N per question should match fixed-N accuracy at lower average compute, or exceed fixed-N accuracy at matched average compute.

This project is inference-only. No model training. No fine-tuning. Everything runs on a single free-tier Colab T4 GPU (16GB VRAM).

## Core concepts

Before writing any code, the assistant should understand these terms precisely:

- **Greedy decoding**: one forward pass, take argmax at each step. Deterministic. One "try" per question. Used as the minimum-compute baseline.
- **Best-of-N (BoN)**: sample N completions with temperature > 0, select one answer per a selection rule. Standard baseline. Cost scales linearly in N.
- **Self-consistency (SC)**: a Best-of-N variant where the selection rule is majority vote over the extracted final answers (not reasoning chains). Our primary BoN-style baseline.
- **Adaptive compute allocation (our method)**: choose N per question based on a cheap uncertainty signal computed from a small probe sample. Formalized in the Method section below.
- **Compute budget**: measured in FLOPs per question, approximated as (number of generated tokens) × (model parameter count × 2). We do not need exact FLOPs — we need a consistent proxy that is monotonic in real cost.

## Repository layout (expected)

```
adaptive-compute/
├── CLAUDE.md                    # this file
├── README.md                    # human-facing summary
├── requirements.txt
├── configs/
│   ├── models.yaml              # model names, HF paths, sizes, token/param counts
│   └── experiments.yaml         # grid definitions
├── src/
│   ├── __init__.py
│   ├── models.py                # unified HF model wrapper
│   ├── datasets.py              # GSM8K / MATH-500 loaders + answer extraction
│   ├── sampling.py              # greedy, BoN, SC sampling primitives
│   ├── selection.py             # majority vote, confidence-weighted vote
│   ├── uncertainty.py           # uncertainty signal computations
│   ├── allocator.py             # the adaptive N-selection policy (our method)
│   ├── evaluator.py             # accuracy + compute bookkeeping
│   └── cache.py                 # disk cache for model outputs (CRITICAL — see below)
├── experiments/
│   ├── 01_baseline_grid.py      # greedy + fixed-N BoN across models
│   ├── 02_uncertainty_probe.py  # measure correlation of signals with correctness
│   ├── 03_adaptive_method.py    # run our method, compare to baselines
│   └── 04_crossover_analysis.py # plot FLOPs-matched accuracy curves
├── notebooks/
│   └── analysis.ipynb           # final plots and tables
├── results/
│   ├── raw/                     # per-question output jsonl files
│   └── figures/                 # generated plots
└── tests/
    ├── test_sampling.py
    ├── test_selection.py
    └── test_allocator.py
```

## Models

All models must fit in a T4's 16GB VRAM with room for generation. Load in bfloat16.

Primary grid:

| Model | HF path | Params | Notes |
|-------|---------|--------|-------|
| Qwen2.5-0.5B-Instruct | Qwen/Qwen2.5-0.5B-Instruct | 0.5B | smallest, fastest, most headroom for BoN |
| Qwen2.5-1.5B-Instruct | Qwen/Qwen2.5-1.5B-Instruct | 1.5B | sweet spot for T4 |
| Qwen2.5-3B-Instruct | Qwen/Qwen2.5-3B-Instruct | 3B | largest that fits with BoN batch > 1 |

The "large model" we want to approximate is Qwen2.5-3B with greedy decoding. The small models (0.5B, 1.5B) are the ones we give adaptive BoN to.

Do NOT download models into the repo. Use Hugging Face cache (`HF_HOME=/content/hf_cache` on Colab).

## Datasets

- **GSM8K** (`openai/gsm8k`, config `main`, split `test`) — primary benchmark. 1,319 grade-school math problems. Answer extraction: final number after `####` in reference, regex final number in generation.
- **MATH-500** (`HuggingFaceH4/MATH-500`) — harder math. Used as a difficulty-scaling test set.
- **Optional**: a synthetic arithmetic task we generate ourselves with tunable difficulty (operand count, digit count). Useful for ablations where we need to control difficulty continuously.

Use 200 problems per benchmark for the main grid to keep compute tractable. Use 500 for the final headline result. Pick problems deterministically (first 200/500 after a seeded shuffle).

## Method: adaptive compute allocation

### The algorithm in one paragraph

For each question, we first draw a small probe set of `k` samples (e.g., k=4) at fixed temperature. From these we compute an uncertainty signal `u(q)`. We then map `u(q)` to a total sample budget `N(q)`: questions that look easy (low uncertainty — probe samples agree) get `N(q) = k` and we stop. Questions that look hard (high uncertainty — probe samples disagree) get more samples up to `N_max`. We then majority-vote over the full sample set and return the answer.

### Pseudocode

```
function AdaptiveBoN(question q, model M, k=4, N_max=64, τ_low, τ_high):
    # Step 1: probe
    probe_samples = sample(M, q, n=k, temperature=0.7)
    u = uncertainty(probe_samples)       # e.g. normalized entropy over extracted answers

    # Step 2: choose budget
    if u < τ_low:
        N = k                            # easy — stop
    elif u > τ_high:
        N = N_max                        # hard — max out
    else:
        N = interpolate(u, τ_low, τ_high, k, N_max)   # proportional

    # Step 3: top up
    if N > k:
        extra_samples = sample(M, q, n=N-k, temperature=0.7)
        all_samples = probe_samples + extra_samples
    else:
        all_samples = probe_samples

    # Step 4: aggregate
    return majority_vote(extract_answers(all_samples))
```

### Uncertainty signals (implement all three, compare)

1. **Answer-entropy** (primary, cheapest): after extracting the final answer from each of the `k` probe samples, compute the normalized entropy of the answer distribution. If all k agree → entropy 0 → easy. If all k disagree → max entropy → hard.

2. **Token-level entropy** (secondary): mean entropy over the generated tokens of one probe sample. Noisier but doesn't require parseable final answers.

3. **Self-reported confidence** (optional extension): prompt the model to output "confidence: X/10" alongside the answer; use X. Fragile for small models but worth testing.

Start with signal 1. It is the cheapest to compute, requires no extra forward passes, and is interpretable.

### The mapping `u → N`

Implement both variants:

- **Heuristic (phase 1)**: two thresholds τ_low, τ_high and linear interpolation. Tune on a held-out split of 50 problems.
- **Learned (phase 2, optional)**: fit a small predictor (scikit-learn logistic regression or a 2-layer MLP) that maps probe-derived features (entropy, probe accuracy vs. ground truth during tuning, prompt length) to a recommended N.

The heuristic version is sufficient for the core result. The learned version is a stretch goal.

## Baselines (all must be implemented)

1. **Greedy**: 1 sample, temperature 0. Minimum compute.
2. **Fixed BoN / SC at N ∈ {4, 8, 16, 32, 64}**: the main comparison set. Our method must beat at least the fixed-N that matches its average compute.
3. **Fixed BoN at matched compute**: specifically, for each question we set N such that the total compute equals our method's average. This controls for compute at the per-question level.
4. **Oracle adaptive** (upper bound, not a real baseline): for each question, assume we know the optimal N in hindsight. Never achievable, but shows the ceiling.

### The headline comparison

Our method wins if, on the same test set:

- At matched average FLOPs per question, our method has higher accuracy than fixed-N BoN
- OR at matched accuracy, our method has lower average FLOPs

Plot both: (accuracy vs. average FLOPs) Pareto curves per method on the same axes.

## Metrics

- **Accuracy** (pass@1, with the chosen selection rule determining what that single answer is)
- **Average FLOPs per question** (proxy: mean generated tokens × model parameter count × 2)
- **Variance of FLOPs across questions** (our method should have high variance — spending more on hard questions; baseline has low variance)
- **Compute savings at matched accuracy**: ratio of baseline FLOPs to our FLOPs when both hit the same accuracy
- **Accuracy lift at matched compute**: difference in accuracy when both spend the same average FLOPs

## Implementation rules

### Caching (critical)

Every model generation should be cached to disk keyed by `(model_name, prompt_hash, temperature, seed, max_tokens)`. Without caching, rerunning experiments is prohibitively expensive. Use a simple sqlite or jsonl-per-question cache. The cache is the single most important piece of infrastructure in this repo — more important than any algorithmic piece.

### Batching

When sampling N completions for one question, pass them as a single batch to the model if VRAM allows. Qwen-0.5B/1.5B can easily batch N=16; Qwen-3B may need N=4 or 8 per batch. Detect OOM and halve batch size automatically.

### Seeds

All sampling uses seeds. For Best-of-N at N samples, use seeds `[base_seed, base_seed+1, ..., base_seed+N-1]` so that the first k samples of an N=64 run are identical to the k probe samples of adaptive. This lets us reuse probe samples rather than re-generate.

### Answer extraction

GSM8K: regex for the final number in the last line, strip commas/units. Fall back to the last number anywhere in the output.
MATH-500: look for `\boxed{...}` first, then fall back to last `$...$` math expression.

Normalize answers before comparison: strip whitespace, convert fractions to decimals when unambiguous, lowercase.

### Prompts

Use the instruct-tuned chat template for Qwen2.5-Instruct. The prompt should ask for step-by-step reasoning followed by a final answer in a specific format. Keep the prompt identical across all conditions — changing prompts between methods is a confound we want to avoid.

Example template for GSM8K:
```
Solve the following math problem step by step. Put your final numerical answer on the last line after "####".

Problem: {question}
```

## Experimental plan

Run in this order. Each builds on the previous.

### Experiment 1: baseline grid

Run greedy and fixed-N BoN (N ∈ {1, 4, 8, 16, 32, 64}) for Qwen-0.5B, Qwen-1.5B, Qwen-3B on GSM8K (200 problems).

Purpose: establish the accuracy-vs-compute frontier per model. This is where we can already see whether small + big-N beats large + greedy (a first hint at the crossover point).

Output: a plot of accuracy vs. FLOPs per question, one curve per model, markers for each N.

### Experiment 2: uncertainty probe validation

For each question in the Exp 1 runs, compute the three uncertainty signals from the first 4 samples. Compute Spearman correlation between each signal and the "true difficulty" of that question (1 - probe accuracy).

Purpose: verify the uncertainty signal actually tracks difficulty. If correlation is low, the whole method falls apart and we need to revisit signal choice.

Output: correlation table + scatter plots.

### Experiment 3: adaptive method

Run our adaptive method with the heuristic (τ_low, τ_high) for Qwen-0.5B and Qwen-1.5B on GSM8K (200 problems). Tune thresholds on a 50-problem held-out split first, then evaluate.

Purpose: the main result. Compare adaptive to fixed-N at matched average compute.

Output: a table with method, model, accuracy, average FLOPs, stdev FLOPs; plus an overlaid Pareto curve.

### Experiment 4: crossover analysis

Plot all (model, method) pairs on a single accuracy vs. FLOPs plot. Identify any crossover points — configurations where small + adaptive beats large + greedy at matched compute.

Purpose: the headline chart of the paper. If any crossover exists, we have the central claim.

Output: the headline figure.

### Experiment 5 (stretch): MATH-500 and robustness

Re-run Experiments 3–4 on MATH-500. Harder problems should amplify the benefit of adaptive allocation, because the "hard question" tail is longer.

## Running experiments

All experiments should be single-file scripts that:

1. Load config from `configs/experiments.yaml`
2. Use the cache — never regenerate samples that already exist
3. Write raw outputs to `results/raw/{experiment}_{model}_{method}.jsonl` (one line per question with all fields)
4. Print a summary table at the end
5. Be resumable — rerunning should skip already-done questions

Do NOT write analysis code into the experiment scripts. Experiments produce raw data; notebooks produce figures. Keep them separated.

## Compute budget estimates

For full Qwen-0.5B on GSM8K 200 problems:
- Greedy: ~5 min on T4
- Fixed BoN N=64: ~15 min (batched)
- Adaptive method: ~8 min average

Total wall-clock for the full experimental plan on GSM8K: 3–5 hours on T4 if caching is correct. Without caching, 15+ hours. **Get the cache right first.**

## Things to NOT do

- Do not fine-tune any model
- Do not implement a process reward model (PRM) — we're relying on voting-based selection, not verifier-guided search. The PRM route exists in the literature (Liu et al. 2025) but requires resources we don't have.
- Do not use closed APIs (OpenAI, Anthropic, Gemini). Everything runs locally on T4.
- Do not try to use vLLM or other inference servers — they add infrastructure complexity. Plain HuggingFace `transformers` with batched generation is sufficient and debuggable.
- Do not optimize for real wall-clock FLOPs at the lowest level. Use the (tokens × params × 2) proxy consistently and don't worry about matmul efficiency.
- Do not add multiple dataset formats to the core pipeline until GSM8K works end-to-end.

## Success criteria for the prototype

The prototype is "done" when:

1. All three Qwen models load, generate, and cache correctly on T4
2. Greedy and fixed-N BoN baselines reproduce known GSM8K accuracies within 2pp (Qwen2.5-1.5B-Instruct should get ~55% greedy, ~70% at SC-32)
3. The adaptive allocator runs end-to-end and produces results logged in the same format as baselines
4. The crossover analysis plot can be generated from logged results with one command

Only after (1)–(4) work on GSM8K should we expand to MATH-500 or further ablations.

## Key references

- Snell et al. 2024, "Scaling LLM Test-Time Compute Optimally Can Be More Effective Than Scaling Model Parameters" (arXiv:2408.03314) — the paper that established compute-optimal test-time scaling at large scale. We are the small-scale analog with adaptive allocation.
- Wang et al. 2022, "Self-Consistency Improves Chain of Thought Reasoning in Language Models" (arXiv:2203.11171) — the self-consistency baseline.
- Liu et al. 2025, "Can 1B LLM Surpass 405B LLM? Rethinking Compute-Optimal Test-Time Scaling" (arXiv:2502.06703) — closest prior work; differs from us by using PRM-guided search and by not proposing an adaptive allocator.
- Cobbe et al. 2021, "Training Verifiers to Solve Math Word Problems" (arXiv:2110.14168) — GSM8K dataset.

## Style and tone for generated code

- Python 3.10+
- Type hints everywhere
- No frameworks beyond transformers, datasets, numpy, scikit-learn, matplotlib, pyyaml, tqdm
- Each module has a docstring explaining its role
- No clever one-liners — clarity over compression
- All randomness seeded
- Prefer pure functions where possible; the allocator policy should be a pure function of (probe samples, hyperparameters) → N