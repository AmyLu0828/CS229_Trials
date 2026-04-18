"""Unified HuggingFace model wrapper for Qwen2.5-Instruct models.

Handles loading in bfloat16, batched generation with automatic OOM recovery,
and routing all outputs through the disk cache.

torch and transformers are imported lazily inside ModelWrapper.__init__ so that
the rest of the codebase (and unit tests) can import Generation without a GPU env.
"""

import gc
import logging
from dataclasses import dataclass
from typing import Optional

from src import cache as cache_mod

logger = logging.getLogger(__name__)

PARAM_COUNTS: dict[str, int] = {
    "Qwen/Qwen2.5-0.5B-Instruct": 494_032_768,
    "Qwen/Qwen2.5-1.5B-Instruct": 1_543_714_816,
    "Qwen/Qwen2.5-3B-Instruct":   3_085_396_992,
}


@dataclass
class Generation:
    output: str
    n_tokens: int
    token_log_probs: Optional[list[float]]


class ModelWrapper:
    """Wraps a single Qwen2.5-Instruct model with cached generation."""

    def __init__(
        self,
        hf_path: str,
        max_new_tokens: int = 512,
        device: Optional[str] = None,
    ) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self._torch = torch
        self.hf_path = hf_path
        self.model_name = hf_path.split("/")[-1]
        self.max_new_tokens = max_new_tokens
        self.param_count = PARAM_COUNTS.get(hf_path, 0)
        if device:
            self.device = device
        elif torch.cuda.is_available():
            self.device = "cuda"
        elif torch.backends.mps.is_available():
            self.device = "mps"
        else:
            self.device = "cpu"

        logger.info(f"Loading {hf_path} on {self.device} ...")
        self.tokenizer = AutoTokenizer.from_pretrained(hf_path, trust_remote_code=True)

        # device_map only works for CUDA multi-GPU; for MPS/CPU load then move
        if self.device == "cuda":
            self.model = AutoModelForCausalLM.from_pretrained(
                hf_path,
                torch_dtype=torch.bfloat16,
                device_map="auto",
                trust_remote_code=True,
            )
        else:
            self.model = AutoModelForCausalLM.from_pretrained(
                hf_path,
                torch_dtype=torch.bfloat16,
                trust_remote_code=True,
            ).to(self.device)
        self.model.eval()
        logger.info(f"Loaded {self.model_name}.")

    def _apply_chat_template(self, user_prompt: str) -> str:
        messages = [{"role": "user", "content": user_prompt}]
        return self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )

    def generate_one(
        self,
        prompt: str,
        temperature: float,
        seed: int,
        return_logprobs: bool = False,
    ) -> Generation:
        """Generate a single completion. Checks cache first."""
        torch = self._torch
        cached = cache_mod.get(self.model_name, prompt, temperature, seed, self.max_new_tokens)
        if cached is not None:
            return Generation(
                output=cached["output"],
                n_tokens=cached["n_tokens"],
                token_log_probs=cached["token_log_probs"],
            )

        formatted = self._apply_chat_template(prompt)
        inputs = self.tokenizer(formatted, return_tensors="pt").to(self.device)
        input_len = inputs["input_ids"].shape[1]

        gen_kwargs: dict = dict(
            max_new_tokens=self.max_new_tokens,
            do_sample=(temperature > 0),
            pad_token_id=self.tokenizer.eos_token_id,
        )
        if temperature > 0:
            gen_kwargs["temperature"] = temperature
        if return_logprobs:
            gen_kwargs["output_scores"] = True
            gen_kwargs["return_dict_in_generate"] = True

        torch.manual_seed(seed)
        with torch.no_grad():
            out = self.model.generate(**inputs, **gen_kwargs)

        if return_logprobs:
            sequences = out.sequences
            scores = out.scores
        else:
            sequences = out
            scores = None

        generated_ids = sequences[0][input_len:]
        output_text = self.tokenizer.decode(generated_ids, skip_special_tokens=True)
        n_tokens = len(generated_ids)

        token_log_probs: Optional[list[float]] = None
        if scores is not None:
            token_log_probs = _extract_logprobs(torch, scores, generated_ids)

        cache_mod.put(
            self.model_name, prompt, temperature, seed, self.max_new_tokens,
            output_text, n_tokens, token_log_probs,
        )
        return Generation(output=output_text, n_tokens=n_tokens, token_log_probs=token_log_probs)

    def generate_batch(
        self,
        prompt: str,
        temperature: float,
        seeds: list[int],
        max_batch_size: int = 16,
        return_logprobs: bool = False,
    ) -> list[Generation]:
        """Generate multiple completions for the same prompt, one per seed.

        Checks cache per seed first, only runs the model for uncached seeds.
        Automatically halves batch size on OOM.
        """
        torch = self._torch
        results: dict[int, Generation] = {}
        uncached_seeds: list[int] = []

        for seed in seeds:
            cached = cache_mod.get(self.model_name, prompt, temperature, seed, self.max_new_tokens)
            if cached is not None:
                results[seed] = Generation(
                    output=cached["output"],
                    n_tokens=cached["n_tokens"],
                    token_log_probs=cached["token_log_probs"],
                )
            else:
                uncached_seeds.append(seed)

        if uncached_seeds:
            formatted = self._apply_chat_template(prompt)
            inputs = self.tokenizer(formatted, return_tensors="pt").to(self.device)
            input_len = inputs["input_ids"].shape[1]

            batch_size = min(max_batch_size, len(uncached_seeds))
            i = 0
            while i < len(uncached_seeds):
                chunk_seeds = uncached_seeds[i : i + batch_size]
                try:
                    chunk_results = self._generate_chunk(
                        inputs, input_len, chunk_seeds, temperature, return_logprobs
                    )
                    for seed, gen in zip(chunk_seeds, chunk_results):
                        results[seed] = gen
                        cache_mod.put(
                            self.model_name, prompt, temperature, seed, self.max_new_tokens,
                            gen.output, gen.n_tokens, gen.token_log_probs,
                        )
                    i += batch_size
                except torch.cuda.OutOfMemoryError:
                    if batch_size == 1:
                        raise
                    batch_size = max(1, batch_size // 2)
                    logger.warning(f"OOM — reducing batch size to {batch_size}")
                    gc.collect()
                    torch.cuda.empty_cache()

        return [results[s] for s in seeds]

    def _generate_chunk(
        self,
        inputs: dict,
        input_len: int,
        seeds: list[int],
        temperature: float,
        return_logprobs: bool,
    ) -> list[Generation]:
        """Run generation for a list of seeds, one completion per seed."""
        torch = self._torch

        gens = []
        for seed in seeds:
            torch.manual_seed(seed)
            with torch.no_grad():
                out = self.model.generate(
                    input_ids=inputs["input_ids"],
                    attention_mask=inputs["attention_mask"],
                    max_new_tokens=self.max_new_tokens,
                    do_sample=(temperature > 0),
                    temperature=temperature if temperature > 0 else None,
                    pad_token_id=self.tokenizer.eos_token_id,
                    output_scores=return_logprobs,
                    return_dict_in_generate=return_logprobs,
                )
            if return_logprobs:
                sequences, scores = out.sequences, out.scores
            else:
                sequences, scores = out, None
            generated_ids = sequences[0][input_len:]
            output_text = self.tokenizer.decode(generated_ids, skip_special_tokens=True)
            n_tokens = len(generated_ids)
            token_log_probs = _extract_logprobs(torch, scores, generated_ids) if scores else None
            gens.append(Generation(output_text, n_tokens, token_log_probs))
        return gens

    def flops_proxy(self, n_tokens: int) -> int:
        """Approximate FLOPs: tokens × params × 2."""
        return n_tokens * self.param_count * 2

    def unload(self) -> None:
        """Free GPU memory."""
        del self.model
        gc.collect()
        if self._torch.cuda.is_available():
            self._torch.cuda.empty_cache()


def _extract_logprobs(torch, scores: tuple, generated_ids) -> list[float]:
    log_probs = []
    for i, step_scores in enumerate(scores):
        lp = torch.log_softmax(step_scores[0], dim=-1)
        log_probs.append(lp[generated_ids[i].item()].item())
    return log_probs
