"""
Run a reasoning-model ReAct agent on tau-bench retail and save a result JSON
compatible with our analyzer.

Defaults target OpenAI o3-mini with reasoning_effort=high. Override with
--provider / --agent-model / --user-model / --reasoning-effort.

Usage (requires a relevant key in .env or the environment):

    # o3-mini (default) — cheapest heavy overthinker (~$0.30-0.50/task)
    python react_r1/run_r1_retail.py --num-tasks 2

    # o4-mini
    python react_r1/run_r1_retail.py --agent-model o4-mini --num-tasks 20

    # Anthropic Sonnet 4.5 with extended thinking
    python react_r1/run_r1_retail.py \
        --provider anthropic \
        --agent-model claude-sonnet-4-5 \
        --user-model gpt-4o-mini --user-provider openai \
        --num-tasks 5

    # Together R1 (if you have a valid TOGETHER_API_KEY)
    python react_r1/run_r1_retail.py \
        --provider together_ai --agent-model deepseek-ai/DeepSeek-R1 \
        --user-model meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo --num-tasks 2
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import random
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(HERE))
# The tau-bench-v1 editable install isn't auto-activated by this uv-managed venv's
# .pth mechanism, so add it to sys.path explicitly.
_TAU_V1 = REPO_ROOT / "external" / "tau-bench-v1"
if _TAU_V1.exists():
    sys.path.insert(0, str(_TAU_V1))


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (avoids adding python-dotenv as a dependency)."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


_load_dotenv(REPO_ROOT / ".env")

from r1_react_agent import ReasoningReActAgent  # noqa: E402
from atc_agent import ATCAgent  # noqa: E402
from satc_agent import SATCAgent  # noqa: E402
from tg_solver import solve_with_tg  # noqa: E402

from tau_bench.envs import get_env  # noqa: E402
from tau_bench.envs.user import UserStrategy  # noqa: E402
from tau_bench.types import EnvRunResult  # noqa: E402


# Defaults target OpenAI o3-mini (heaviest documented overthinker, cheap).
DEFAULT_AGENT_MODEL = "o3-mini"
DEFAULT_USER_MODEL = "gpt-4o-mini"
DEFAULT_PROVIDER = "openai"
DEFAULT_USER_PROVIDER = "openai"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--agent-model", default=DEFAULT_AGENT_MODEL)
    p.add_argument("--user-model", default=DEFAULT_USER_MODEL)
    p.add_argument("--provider", default=DEFAULT_PROVIDER,
                   help="LiteLLM provider for the agent "
                        "(e.g. openai, anthropic, together_ai, openrouter)")
    p.add_argument("--user-provider", default=None,
                   help="LiteLLM provider for the user simulator. "
                        "Defaults to --provider.")
    p.add_argument("--reasoning-effort", default="high",
                   choices=["low", "medium", "high"],
                   help="OpenAI o-series reasoning effort knob.")
    p.add_argument("--thinking-budget-tokens", type=int, default=8192,
                   help="Anthropic extended-thinking budget (ignored by other "
                        "providers).")
    p.add_argument("--env", default="retail", choices=["retail", "airline"])
    p.add_argument("--task-split", default="test",
                   choices=["train", "test", "dev"])
    p.add_argument("--temperature", type=float, default=0.0,
                   help="Ignored by o-series reasoning models.")
    p.add_argument("--num-tasks", type=int, default=2,
                   help="-1 means run all tasks")
    p.add_argument("--start-index", type=int, default=0)
    p.add_argument("--task-ids", type=int, nargs="+", default=None)
    p.add_argument("--num-trials", type=int, default=1)
    p.add_argument("--max-concurrency", type=int, default=1)
    p.add_argument("--max-num-steps", type=int, default=30)
    p.add_argument("--seed", type=int, default=10)
    p.add_argument("--log-dir", default=str(HERE / "logs"))
    p.add_argument("--run-name", default=None)
    # SATC + ATC + TG knobs
    p.add_argument("--method", default="fixed",
                   choices=["fixed", "satc", "atc"],
                   help="fixed: standard ReAct. satc: self-reported-confidence "
                        "ATC (primary). atc: K-probe ATC (ablation).")
    p.add_argument("--tg", action="store_true",
                   help="Enable Trajectory Guardrail wrapper.")
    p.add_argument("--satc-tau-hi", type=int, default=7)
    p.add_argument("--satc-tau-lo", type=int, default=3)
    p.add_argument("--satc-default-conf", type=int, default=5)
    p.add_argument("--atc-K", type=int, default=3)
    p.add_argument("--atc-tau-low", type=float, default=0.0)
    p.add_argument("--atc-tau-high", type=float, default=0.9)
    p.add_argument("--capture-logprobs", action="store_true",
                   help="Ask the provider for per-token chosen-token logprobs "
                        "and compute TECA-NLL stats per turn. Currently "
                        "supported on together_ai DeepSeek-R1.")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    user_provider = args.user_provider or args.provider
    provider_key = {
        "together_ai": "TOGETHER_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }
    for prov in {args.provider, user_provider}:
        expected = provider_key.get(prov, f"{prov.upper()}_API_KEY")
        if not os.environ.get(expected):
            print(f"ERROR: {expected} is not set (provider={prov}).",
                  file=sys.stderr)
            return 2

    random.seed(args.seed)
    time_str = datetime.now().strftime("%m%d%H%M%S")
    run_name = args.run_name or (
        f"react-{args.agent_model.split('/')[-1]}"
        f"-{args.env}-{args.task_split}-t{args.temperature}-{time_str}"
    )
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    ckpt_path = log_dir / f"{run_name}.json"

    print(f"Loading env={args.env} user_strategy=llm "
          f"user_model={args.user_model} user_provider={user_provider}")
    env = get_env(
        args.env,
        user_strategy=UserStrategy.LLM.value,
        user_model=args.user_model,
        user_provider=user_provider,
        task_split=args.task_split,
    )

    if args.method == "satc":
        agent = SATCAgent(
            tools_info=env.tools_info,
            wiki=env.wiki,
            model=args.agent_model,
            provider=args.provider,
            use_reasoning=True,
            temperature=args.temperature,
            reasoning_effort=args.reasoning_effort,
            thinking_budget_tokens=args.thinking_budget_tokens,
            tau_conf_hi=args.satc_tau_hi,
            tau_conf_lo=args.satc_tau_lo,
            default_conf=args.satc_default_conf,
        )
    elif args.method == "atc":
        agent = ATCAgent(
            tools_info=env.tools_info,
            wiki=env.wiki,
            model=args.agent_model,
            provider=args.provider,
            use_reasoning=True,
            temperature=args.temperature,
            reasoning_effort=args.reasoning_effort,
            thinking_budget_tokens=args.thinking_budget_tokens,
            K=args.atc_K,
            tau_low=args.atc_tau_low,
            tau_high=args.atc_tau_high,
        )
    else:
        agent = ReasoningReActAgent(
            tools_info=env.tools_info,
            wiki=env.wiki,
            model=args.agent_model,
            provider=args.provider,
            use_reasoning=True,
            temperature=args.temperature,
            reasoning_effort=args.reasoning_effort,
            thinking_budget_tokens=args.thinking_budget_tokens,
            capture_logprobs=args.capture_logprobs,
        )

    total_tasks = len(env.tasks)
    if args.task_ids:
        idxs = args.task_ids
    else:
        end = total_tasks if args.num_tasks == -1 else min(
            args.start_index + args.num_tasks, total_tasks
        )
        idxs = list(range(args.start_index, end))

    print(f"Running {len(idxs)} tasks. Checkpoint: {ckpt_path}")
    lock = multiprocessing.Lock()
    all_results = []

    def _run_one(idx: int, trial: int) -> EnvRunResult:
        iso_env = get_env(
            args.env,
            user_strategy=UserStrategy.LLM.value,
            user_model=args.user_model,
            user_provider=user_provider,
            task_split=args.task_split,
            task_index=idx,
        )
        t0 = time.time()
        try:
            if args.tg:
                sr = solve_with_tg(
                    env=iso_env,
                    agent=agent,
                    task_index=idx,
                    max_num_steps=args.max_num_steps,
                    tg_on=True,
                )
            else:
                sr = agent.solve(
                    env=iso_env,
                    task_index=idx,
                    max_num_steps=args.max_num_steps,
                )
            result = EnvRunResult(
                task_id=idx,
                reward=sr.reward,
                info=sr.info,
                traj=sr.messages,
                trial=trial,
            )
        except Exception as e:
            result = EnvRunResult(
                task_id=idx,
                reward=0.0,
                info={"error": str(e), "traceback": traceback.format_exc()},
                traj=[],
                trial=trial,
            )
        elapsed = time.time() - t0
        mark = "OK " if result.reward == 1 else "FAIL"
        print(f"[{mark}] task {idx} trial {trial}  {elapsed:6.1f}s  "
              f"reward={result.reward}")
        with lock:
            data = []
            if ckpt_path.exists():
                with ckpt_path.open() as f:
                    data = json.load(f)
            with ckpt_path.open("w") as f:
                json.dump(data + [result.model_dump()], f, indent=2)
        return result

    for trial in range(args.num_trials):
        work = [(i, trial) for i in idxs]
        with ThreadPoolExecutor(max_workers=args.max_concurrency) as ex:
            for r in ex.map(lambda w: _run_one(*w), work):
                all_results.append(r)

    with ckpt_path.open("w") as f:
        json.dump([r.model_dump() for r in all_results], f, indent=2)

    solved = sum(1 for r in all_results if r.reward == 1)
    print("=" * 60)
    print(f"Solved {solved}/{len(all_results)}  "
          f"accuracy={solved / max(1, len(all_results)):.3f}")
    print(f"Saved: {ckpt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
