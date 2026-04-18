"""Experiment 4: crossover analysis — headline chart.

Plots all (model, method) pairs on a single accuracy vs. FLOPs figure.
Identifies crossover points where small + adaptive beats large + greedy.

Requires Exp 1 and Exp 3 outputs to exist.

Run:
    python experiments/04_crossover_analysis.py
"""

import json
import logging
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.evaluator import AggregateResult, QuestionResult, aggregate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
with open(ROOT / "configs" / "experiments.yaml") as f:
    CFG = yaml.safe_load(f)
with open(ROOT / "configs" / "models.yaml") as f:
    MODEL_CFG = yaml.safe_load(f)

RESULTS_DIR = ROOT / "results" / "raw"
FIGURES_DIR = ROOT / "results" / "figures"
FIGURES_DIR.mkdir(parents=True, exist_ok=True)

MODEL_COLORS = {
    "Qwen2.5-0.5B-Instruct": "#1f77b4",
    "Qwen2.5-1.5B-Instruct": "#ff7f0e",
    "Qwen2.5-3B-Instruct":   "#2ca02c",
}
MODEL_MARKERS = {
    "Qwen2.5-0.5B-Instruct": "o",
    "Qwen2.5-1.5B-Instruct": "s",
    "Qwen2.5-3B-Instruct":   "^",
}


def _load_results(path: Path) -> list[QuestionResult]:
    rows = []
    with open(path) as f:
        for line in f:
            obj = json.loads(line)
            rows.append(QuestionResult(**obj))
    return rows


def collect_all() -> dict[tuple[str, str], AggregateResult]:
    """Collect all results into {(model_name, method): AggregateResult}."""
    data: dict[tuple[str, str], AggregateResult] = {}

    # Exp1 baselines
    for path in RESULTS_DIR.glob("exp1_*.jsonl"):
        stem = path.stem[len("exp1_"):]
        # stem format: ModelName_method  (ModelName may contain underscores)
        # we know the model names, so match greedily
        for m in MODEL_CFG["models"]:
            short = m["hf_path"].split("/")[-1]
            if stem.startswith(short + "_"):
                method = stem[len(short) + 1:]
                results = _load_results(path)
                if results:
                    data[(short, method)] = aggregate(results)
                break

    # Exp3 adaptive
    for path in RESULTS_DIR.glob("exp3_adaptive_*.jsonl"):
        short = path.stem[len("exp3_adaptive_"):]
        results = _load_results(path)
        if results:
            data[(short, "adaptive")] = aggregate(results)

    return data


def main() -> None:
    data = collect_all()
    if not data:
        logger.error("No results found. Run experiments 1 and 3 first.")
        sys.exit(1)

    fig, ax = plt.subplots(figsize=(9, 6))

    # Group by model
    model_names = [m["hf_path"].split("/")[-1] for m in MODEL_CFG["models"]]

    for model_name in model_names:
        color = MODEL_COLORS.get(model_name, "black")
        marker = MODEL_MARKERS.get(model_name, "o")

        # Baselines: collect SC points sorted by FLOPs
        baseline_points = []
        for method in ["greedy", "sc_1", "sc_4", "sc_8", "sc_16", "sc_32", "sc_64"]:
            key = (model_name, method)
            if key in data:
                agg = data[key]
                baseline_points.append((agg.avg_flops, agg.accuracy, method))

        baseline_points.sort(key=lambda x: x[0])
        if baseline_points:
            xs = [p[0] for p in baseline_points]
            ys = [p[1] for p in baseline_points]
            ax.plot(xs, ys, marker=marker, color=color, linestyle="--",
                    label=f"{model_name} SC", alpha=0.7)
            for x, y, m_label in baseline_points:
                ax.annotate(m_label, (x, y), textcoords="offset points",
                            xytext=(3, 3), fontsize=6, color=color)

        # Adaptive point
        key = (model_name, "adaptive")
        if key in data:
            agg = data[key]
            ax.scatter([agg.avg_flops], [agg.accuracy],
                       marker="*", s=250, color=color, zorder=6,
                       label=f"{model_name} adaptive")
            ax.annotate("adaptive", (agg.avg_flops, agg.accuracy),
                        textcoords="offset points", xytext=(4, -10),
                        fontsize=7, color=color, fontweight="bold")

    ax.set_xlabel("Avg FLOPs per question (proxy: tokens × params × 2)", fontsize=11)
    ax.set_ylabel("Accuracy", fontsize=11)
    ax.set_title("Accuracy vs. Compute — All Models and Methods\n(★ = adaptive; dashed = fixed SC)", fontsize=12)
    ax.legend(fontsize=8, loc="lower right")
    ax.set_xscale("log")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    out_path = FIGURES_DIR / "exp4_crossover_headline.png"
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    logger.info(f"Saved headline figure to {out_path}")

    # Print crossover analysis
    print("\n=== Experiment 4: Crossover Analysis ===")
    greedy_3b = data.get(("Qwen2.5-3B-Instruct", "greedy"))
    if greedy_3b:
        print(f"\nReference: Qwen2.5-3B greedy  acc={greedy_3b.accuracy:.3f}  "
              f"flops={greedy_3b.avg_flops:.3e}")
        for model_name in ["Qwen2.5-0.5B-Instruct", "Qwen2.5-1.5B-Instruct"]:
            agg = data.get((model_name, "adaptive"))
            if agg:
                beats = agg.accuracy >= greedy_3b.accuracy
                cheaper = agg.avg_flops <= greedy_3b.avg_flops
                status = "CROSSOVER ✓" if (beats and cheaper) else (
                    "same accuracy, more compute" if beats else "no crossover"
                )
                print(f"  {model_name} adaptive: acc={agg.accuracy:.3f}  "
                      f"flops={agg.avg_flops:.3e}  → {status}")
    else:
        print("3B greedy result not found. Run experiment 1 for Qwen2.5-3B first.")

    # Full table
    print("\n=== All results ===")
    rows = sorted(data.items(), key=lambda x: (x[0][0], x[0][1]))
    for (model, method), agg in rows:
        print(f"  {model:<32} {method:<12} acc={agg.accuracy:.3f}  "
              f"avgFLOPs={agg.avg_flops:.3e}  avgN={agg.avg_n_samples:.1f}")


if __name__ == "__main__":
    main()
