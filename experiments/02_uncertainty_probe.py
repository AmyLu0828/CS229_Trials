"""Experiment 2: uncertainty probe validation.

For each question in the Exp 1 runs, compute all uncertainty signals from the
first k=4 samples. Correlate each signal with per-question probe accuracy
(a proxy for true difficulty).

Outputs:
  - results/raw/exp2_uncertainty_{model}.jsonl  (per-question signals + accuracy)
  - results/figures/exp2_correlation_table.txt
  - results/figures/exp2_scatter_{model}_{signal}.png

Run:
    python experiments/02_uncertainty_probe.py
"""

import json
import logging
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import yaml
from scipy.stats import spearmanr
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.datasets import extract_answer, format_prompt, load_gsm8k
from src.models import ModelWrapper
from src.sampling import best_of_n
from src.uncertainty import answer_entropy, compute_uncertainty, token_entropy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
with open(ROOT / "configs" / "experiments.yaml") as f:
    CFG = yaml.safe_load(f)
with open(ROOT / "configs" / "models.yaml") as f:
    MODEL_CFG = yaml.safe_load(f)

EXP = CFG["experiment_2_uncertainty_probe"]
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
    return RESULTS_DIR / f"exp2_uncertainty_{model_name}.jsonl"


def _load_done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    done = set()
    with open(path) as f:
        for line in f:
            obj = json.loads(line)
            done.add(obj["problem_id"])
    return done


def run_model(model_name: str, problems: list, dataset: str) -> None:
    mcfg = _model_cfg(model_name)
    model = ModelWrapper(hf_path=mcfg["hf_path"], max_new_tokens=GLOBAL["max_new_tokens"])
    max_batch = mcfg["max_batch_size"]
    k = EXP["probe_k"]
    temperature = GLOBAL["temperature"]
    base_seed = GLOBAL["base_seed"]

    out_path = _result_path(model.model_name)
    done = _load_done(out_path)
    todo = [p for p in problems if p.id not in done]
    logger.info(f"{model.model_name}: {len(done)} cached, {len(todo)} to run")

    with open(out_path, "a") as fout:
        for problem in tqdm(todo, desc=f"{model.model_name} probe"):
            prompt = format_prompt(problem.question, dataset)
            sampled = best_of_n(
                model, prompt, n=k, temperature=temperature,
                base_seed=base_seed, max_batch_size=max_batch,
            )
            answers = [extract_answer(g.output, dataset) for g in sampled.generations]

            # per-question probe accuracy = fraction of k samples with correct answer
            probe_acc = sum(1 for a in answers if a == problem.answer) / k
            difficulty = 1.0 - probe_acc

            ae = answer_entropy(answers)
            te = token_entropy(sampled, sample_idx=0)

            row = {
                "problem_id": problem.id,
                "ground_truth": problem.answer,
                "probe_answers": answers,
                "probe_accuracy": probe_acc,
                "difficulty": difficulty,
                "answer_entropy": ae,
                "token_entropy": te,
            }
            fout.write(json.dumps(row) + "\n")

    model.unload()


def analyze(model_name: str) -> dict:
    path = _result_path(model_name)
    rows = []
    with open(path) as f:
        for line in f:
            rows.append(json.loads(line))

    difficulties = np.array([r["difficulty"] for r in rows])
    signals = {
        "answer_entropy": np.array([r["answer_entropy"] for r in rows]),
        "token_entropy": np.array([r["token_entropy"] for r in rows]),
    }

    corrs = {}
    for sig_name, sig_vals in signals.items():
        rho, pval = spearmanr(difficulties, sig_vals)
        corrs[sig_name] = {"spearman_rho": float(rho), "p_value": float(pval)}
        # scatter plot
        fig, ax = plt.subplots(figsize=(5, 4))
        ax.scatter(sig_vals, difficulties, alpha=0.4, s=20)
        ax.set_xlabel(sig_name)
        ax.set_ylabel("difficulty (1 - probe_acc)")
        ax.set_title(f"{model_name}\nSpearman ρ = {rho:.3f}  p = {pval:.3g}")
        fig.tight_layout()
        fig.savefig(FIGURES_DIR / f"exp2_scatter_{model_name}_{sig_name}.png", dpi=120)
        plt.close(fig)

    return corrs


def main() -> None:
    dataset = EXP["dataset"]
    n_problems = EXP["n_problems"]
    base_seed = GLOBAL["base_seed"]
    problems = load_gsm8k(n=n_problems, seed=base_seed)

    for model_name in EXP["models"]:
        run_model(model_name, problems, dataset)

    print("\n=== Experiment 2: Correlation Table ===")
    header = f"{'Model':<30} {'Signal':<20} {'Spearman ρ':>12} {'p-value':>12}"
    print(header)
    print("-" * len(header))

    corr_lines = []
    for model_name in EXP["models"]:
        mcfg = _model_cfg(model_name)
        corrs = analyze(mcfg["hf_path"].split("/")[-1])
        for sig, stats in corrs.items():
            line = f"{mcfg['hf_path'].split('/')[-1]:<30} {sig:<20} {stats['spearman_rho']:>12.4f} {stats['p_value']:>12.4g}"
            print(line)
            corr_lines.append(line)

    table_path = FIGURES_DIR / "exp2_correlation_table.txt"
    with open(table_path, "w") as f:
        f.write(header + "\n" + "-" * len(header) + "\n")
        for line in corr_lines:
            f.write(line + "\n")
    logger.info(f"Saved correlation table to {table_path}")


if __name__ == "__main__":
    main()
