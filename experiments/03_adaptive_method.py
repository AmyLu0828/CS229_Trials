"""Experiment 3: adaptive compute allocation — our method.

1. Tune thresholds (tau_low, tau_high) on a 50-problem held-out split.
2. Evaluate on the remaining 200 problems.
3. Compare adaptive to fixed-N SC at matched average compute.

Outputs:
  - results/raw/exp3_adaptive_{model}.jsonl
  - results/figures/exp3_pareto.png
  - Summary table printed to stdout

Run:
    python experiments/03_adaptive_method.py
"""

import json
import logging
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import yaml
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.allocator import adaptive_n, tune_thresholds
from src.datasets import extract_answer, format_prompt, load_gsm8k
from src.evaluator import (
    AggregateResult,
    QuestionResult,
    aggregate,
    compute_flops_proxy,
    print_aggregate_table,
)
from src.models import ModelWrapper
from src.sampling import probe_then_topup
from src.selection import majority_vote

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
with open(ROOT / "configs" / "experiments.yaml") as f:
    CFG = yaml.safe_load(f)
with open(ROOT / "configs" / "models.yaml") as f:
    MODEL_CFG = yaml.safe_load(f)

EXP = CFG["experiment_3_adaptive"]
GLOBAL = CFG["global"]
RESULTS_DIR = ROOT / "results" / "raw"
FIGURES_DIR = ROOT / "results" / "figures"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
FIGURES_DIR.mkdir(parents=True, exist_ok=True)


def _model_cfg(name: str) -> dict:
    for m in MODEL_CFG["models"]:
        if m["name"] == name:
            return m
    raise KeyError(name)


def _result_path(model_name: str) -> Path:
    return RESULTS_DIR / f"exp3_adaptive_{model_name}.jsonl"


def _load_done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    done = set()
    with open(path) as f:
        for line in f:
            done.add(json.loads(line)["problem_id"])
    return done


def _load_results(path: Path) -> list[QuestionResult]:
    results = []
    if not path.exists():
        return results
    with open(path) as f:
        for line in f:
            obj = json.loads(line)
            results.append(QuestionResult(**obj))
    return results


def run_adaptive(
    model: ModelWrapper,
    problems: list,
    tau_low: float,
    tau_high: float,
    k: int,
    n_max: int,
    temperature: float,
    base_seed: int,
    max_batch_size: int,
    dataset: str,
    out_path: Path,
) -> None:
    done = _load_done(out_path)
    todo = [p for p in problems if p.id not in done]
    logger.info(f"Adaptive ({model.model_name}): {len(done)} cached, {len(todo)} to run")

    with open(out_path, "a") as fout:
        for problem in tqdm(todo, desc=f"adaptive {model.model_name}"):
            prompt = format_prompt(problem.question, dataset)

            # Step 1: probe
            probe_sampled = probe_then_topup(
                model, prompt, k=k, n_total=k,
                temperature=temperature, base_seed=base_seed,
                max_batch_size=max_batch_size,
            )
            probe_answers = [extract_answer(g.output, dataset) for g in probe_sampled.generations]

            # Step 2: choose N
            n_chosen, uncertainty = adaptive_n(
                probe_sampled, probe_answers, k, n_max, tau_low, tau_high
            )

            # Step 3: top up (reuses probe samples from cache)
            full_sampled = probe_then_topup(
                model, prompt, k=k, n_total=n_chosen,
                temperature=temperature, base_seed=base_seed,
                max_batch_size=max_batch_size,
            )
            full_answers = [extract_answer(g.output, dataset) for g in full_sampled.generations]

            # Step 4: aggregate
            predicted = majority_vote(full_answers)
            is_correct = (predicted == problem.answer) if predicted else False
            flops = compute_flops_proxy(full_sampled.total_tokens, model.param_count)

            row = QuestionResult(
                problem_id=problem.id,
                question=problem.question,
                ground_truth=problem.answer,
                predicted_answer=predicted,
                is_correct=is_correct,
                n_samples=n_chosen,
                total_tokens=full_sampled.total_tokens,
                flops_proxy=flops,
                uncertainty=uncertainty,
                method="adaptive",
                model_name=model.model_name,
            )
            fout.write(json.dumps(row.__dict__) + "\n")


def _load_exp1_results(model_name: str) -> dict[str, list[QuestionResult]]:
    """Load all Exp1 results for a model as {method: [results]}."""
    results: dict[str, list[QuestionResult]] = {}
    exp1_dir = RESULTS_DIR
    for path in exp1_dir.glob(f"exp1_{model_name}_*.jsonl"):
        method = path.stem.replace(f"exp1_{model_name}_", "")
        r = []
        with open(path) as f:
            for line in f:
                obj = json.loads(line)
                r.append(QuestionResult(**obj))
        results[method] = r
    return results


def plot_pareto(
    adaptive_agg: AggregateResult,
    baseline_aggs: dict[str, AggregateResult],
    model_name: str,
) -> None:
    fig, ax = plt.subplots(figsize=(7, 5))
    # baselines
    xs = [a.avg_flops for a in baseline_aggs.values()]
    ys = [a.accuracy for a in baseline_aggs.values()]
    labels = list(baseline_aggs.keys())
    ax.plot(xs, ys, "o--", color="steelblue", label="Fixed SC")
    for x, y, lbl in zip(xs, ys, labels):
        ax.annotate(lbl, (x, y), textcoords="offset points", xytext=(4, 4), fontsize=7)
    # adaptive
    ax.scatter([adaptive_agg.avg_flops], [adaptive_agg.accuracy],
               marker="*", s=200, color="crimson", zorder=5, label="Adaptive (ours)")
    ax.set_xlabel("Avg FLOPs per question (proxy)")
    ax.set_ylabel("Accuracy")
    ax.set_title(f"Accuracy vs FLOPs — {model_name}")
    ax.legend()
    fig.tight_layout()
    path = FIGURES_DIR / f"exp3_pareto_{model_name}.png"
    fig.savefig(path, dpi=120)
    plt.close(fig)
    logger.info(f"Saved {path}")


def main() -> None:
    dataset = EXP["dataset"]
    n_problems = EXP["n_problems"]
    n_tune = EXP["n_tune"]
    k = EXP["probe_k"]
    n_max = EXP["n_max"]
    temperature = GLOBAL["temperature"]
    base_seed = GLOBAL["base_seed"]
    tau_low_cands = EXP["n_candidates_tau"]["low"]
    tau_high_cands = EXP["n_candidates_tau"]["high"]

    all_problems = load_gsm8k(n=n_problems, seed=base_seed)
    # held-out tuning split: last n_tune problems
    tune_problems = all_problems[-n_tune:]
    eval_problems = all_problems[:-n_tune]

    all_aggregates: list[AggregateResult] = []

    for model_name in EXP["models"]:
        mcfg = _model_cfg(model_name)
        model = ModelWrapper(hf_path=mcfg["hf_path"], max_new_tokens=GLOBAL["max_new_tokens"])
        max_batch = mcfg["max_batch_size"]

        # ── Threshold tuning ──────────────────────────────────────────────────
        logger.info(f"Tuning thresholds on {len(tune_problems)} problems ...")
        tune_uncertainties = []
        tune_correctness = []
        for problem in tqdm(tune_problems, desc="tuning probe"):
            prompt = format_prompt(problem.question, dataset)
            probe_sampled = probe_then_topup(
                model, prompt, k=k, n_total=k,
                temperature=temperature, base_seed=base_seed,
                max_batch_size=max_batch,
            )
            probe_answers = [extract_answer(g.output, dataset) for g in probe_sampled.generations]
            from src.uncertainty import answer_entropy as ae_fn
            u = ae_fn(probe_answers)
            probe_acc = sum(1 for a in probe_answers if a == problem.answer) / k
            tune_uncertainties.append(u)
            tune_correctness.append(probe_acc > 0)

        tau_low, tau_high = tune_thresholds(
            tune_uncertainties, tune_correctness, k, n_max,
            tau_low_cands, tau_high_cands,
        )
        logger.info(f"Best thresholds: tau_low={tau_low}, tau_high={tau_high}")

        # ── Evaluation ────────────────────────────────────────────────────────
        out_path = _result_path(model.model_name)
        run_adaptive(
            model, eval_problems, tau_low, tau_high, k, n_max,
            temperature, base_seed, max_batch, dataset, out_path,
        )

        adaptive_results = _load_results(out_path)
        adaptive_agg = aggregate(adaptive_results)
        all_aggregates.append(adaptive_agg)

        # Load baselines for comparison plot
        baseline_aggs: dict[str, AggregateResult] = {}
        for path in RESULTS_DIR.glob(f"exp1_{model.model_name}_*.jsonl"):
            method = path.stem.replace(f"exp1_{model.model_name}_", "")
            r = []
            with open(path) as f:
                for line in f:
                    obj = json.loads(line)
                    r.append(QuestionResult(**obj))
            if r:
                baseline_aggs[method] = aggregate(r)
                all_aggregates.append(baseline_aggs[method])

        if baseline_aggs:
            plot_pareto(adaptive_agg, baseline_aggs, model.model_name)

        model.unload()

    print("\n=== Experiment 3 Summary ===")
    print_aggregate_table(all_aggregates)


if __name__ == "__main__":
    main()
