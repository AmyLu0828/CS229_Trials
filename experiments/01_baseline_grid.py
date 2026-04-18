"""Experiment 1: baseline grid — greedy + fixed-N BoN across all three Qwen models.

Outputs one JSONL per (model, method) to results/raw/.
Prints a summary table at the end.
Fully resumable: already-cached questions are skipped automatically.

Run:
    python experiments/01_baseline_grid.py
"""

import json
import logging
import os
import sys
from pathlib import Path

import yaml
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import cache as cache_mod
from src.datasets import extract_answer, format_prompt, load_gsm8k
from src.evaluator import (
    AggregateResult,
    QuestionResult,
    aggregate,
    compute_flops_proxy,
    print_aggregate_table,
)
from src.models import ModelWrapper
from src.sampling import best_of_n, greedy
from src.selection import select_answer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent
with open(ROOT / "configs" / "experiments.yaml") as f:
    CFG = yaml.safe_load(f)
with open(ROOT / "configs" / "models.yaml") as f:
    MODEL_CFG = yaml.safe_load(f)

EXP = CFG["experiment_1_baseline_grid"]
GLOBAL = CFG["global"]
RESULTS_DIR = ROOT / "results" / "raw"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# On Colab set HF_HOME before running; here we respect the env var if set.
if os.environ.get("COLAB_BACKEND_VERSION"):
    cache_mod.set_cache_dir("/content/results/cache")


def _model_cfg(name: str) -> dict:
    for m in MODEL_CFG["models"]:
        if m["name"] == name:
            return m
    raise KeyError(name)


# ── Per-question runner ───────────────────────────────────────────────────────

def run_greedy_question(
    model: ModelWrapper,
    problem,
    dataset: str,
    base_seed: int,
) -> QuestionResult:
    prompt = format_prompt(problem.question, dataset)
    sampled = greedy(model, prompt, seed=base_seed)
    answers = [extract_answer(g.output, dataset) for g in sampled.generations]
    predicted = select_answer(sampled, answers, method="first")
    is_correct = (predicted == problem.answer) if predicted else False
    flops = compute_flops_proxy(sampled.total_tokens, model.param_count)
    return QuestionResult(
        problem_id=problem.id,
        question=problem.question,
        ground_truth=problem.answer,
        predicted_answer=predicted,
        is_correct=is_correct,
        n_samples=1,
        total_tokens=sampled.total_tokens,
        flops_proxy=flops,
        method="greedy",
        model_name=model.model_name,
    )


def run_bon_question(
    model: ModelWrapper,
    problem,
    n: int,
    dataset: str,
    temperature: float,
    base_seed: int,
    max_batch_size: int,
) -> QuestionResult:
    prompt = format_prompt(problem.question, dataset)
    sampled = best_of_n(model, prompt, n=n, temperature=temperature,
                        base_seed=base_seed, max_batch_size=max_batch_size)
    answers = [extract_answer(g.output, dataset) for g in sampled.generations]
    predicted = select_answer(sampled, answers, method="majority_vote")
    is_correct = (predicted == problem.answer) if predicted else False
    flops = compute_flops_proxy(sampled.total_tokens, model.param_count)
    return QuestionResult(
        problem_id=problem.id,
        question=problem.question,
        ground_truth=problem.answer,
        predicted_answer=predicted,
        is_correct=is_correct,
        n_samples=n,
        total_tokens=sampled.total_tokens,
        flops_proxy=flops,
        method=f"sc_{n}",
        model_name=model.model_name,
    )


# ── Output helpers ────────────────────────────────────────────────────────────

def _result_path(model_name: str, method: str) -> Path:
    return RESULTS_DIR / f"exp1_{model_name}_{method}.jsonl"


def _load_done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    done = set()
    with open(path) as f:
        for line in f:
            obj = json.loads(line)
            done.add(obj["problem_id"])
    return done


def _append_result(path: Path, result: QuestionResult) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(result.__dict__) + "\n")


def _load_all_results(path: Path) -> list[QuestionResult]:
    results = []
    if not path.exists():
        return results
    with open(path) as f:
        for line in f:
            obj = json.loads(line)
            results.append(QuestionResult(**obj))
    return results


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-problems", type=int, default=None)
    parser.add_argument("--models", nargs="+", default=None)
    parser.add_argument("--bon-ns", nargs="+", type=int, default=None)
    args = parser.parse_args()

    dataset = EXP["dataset"]
    n_problems = args.n_problems or EXP["n_problems"]
    bon_ns = args.bon_ns or EXP["bon_n_values"]
    model_names = args.models or EXP["models"]
    temperature = GLOBAL["temperature"]
    base_seed = GLOBAL["base_seed"]
    max_new_tokens = GLOBAL["max_new_tokens"]

    problems = load_gsm8k(n=n_problems, seed=base_seed)
    logger.info(f"Loaded {len(problems)} problems from {dataset}")

    all_aggregates: list[AggregateResult] = []

    for model_name in EXP["models"]:
        mcfg = _model_cfg(model_name)
        model = ModelWrapper(
            hf_path=mcfg["hf_path"],
            max_new_tokens=max_new_tokens,
        )
        max_batch = mcfg["max_batch_size"]

        # ── Greedy ──
        method = "greedy"
        out_path = _result_path(model.model_name, method)
        done = _load_done(out_path)
        todo = [p for p in problems if p.id not in done]
        logger.info(f"{model.model_name} greedy: {len(done)} cached, {len(todo)} to run")

        for problem in tqdm(todo, desc=f"{model.model_name} greedy"):
            result = run_greedy_question(model, problem, dataset, base_seed)
            _append_result(out_path, result)

        agg = aggregate(_load_all_results(out_path))
        all_aggregates.append(agg)

        # ── Fixed BoN / SC ──
        for n in bon_ns:
            method = f"sc_{n}"
            out_path = _result_path(model.model_name, method)
            done = _load_done(out_path)
            todo = [p for p in problems if p.id not in done]
            logger.info(f"{model.model_name} sc_{n}: {len(done)} cached, {len(todo)} to run")

            for problem in tqdm(todo, desc=f"{model.model_name} sc_{n}"):
                result = run_bon_question(
                    model, problem, n, dataset, temperature, base_seed, max_batch
                )
                _append_result(out_path, result)

            agg = aggregate(_load_all_results(out_path))
            all_aggregates.append(agg)

        model.unload()
        logger.info(f"Unloaded {model.model_name}")

    print("\n=== Experiment 1 Summary ===")
    print_aggregate_table(all_aggregates)


if __name__ == "__main__":
    main()
