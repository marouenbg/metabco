"""LLM backend abstraction with on-disk caching.

Backends are interchangeable on purpose. The scientific question is whether a
*small open model, specialised per species* can stand in for a curated
knowledgebase, so the harness must run identically against a frontier API, a
local vLLM server, and a LoRA-tuned 8B checkpoint.

Caching is not an optimisation here, it is a correctness property. A cached run
replays exactly, which means a result can be re-analysed months later without
paying for inference again or worrying that the endpoint has drifted underneath
the conclusion.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class LLMResponse:
    text: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    cached: bool = False
    raw: Any = None


class LLMClient(Protocol):
    model: str

    def complete(self, system: str, user: str, *, max_tokens: int = 1024,
                 temperature: float = 0.0, stop: list[str] | None = None) -> LLMResponse:
        ...


class DiskCache:
    """Content-addressed cache: same prompt and settings, same answer."""

    def __init__(self, root: str | Path = ".cache/llm", enabled: bool = True):
        self.root = Path(root)
        self.enabled = enabled
        if enabled:
            self.root.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    def key(self, **parts: Any) -> str:
        blob = json.dumps(parts, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()

    def get(self, key: str) -> dict | None:
        if not self.enabled:
            return None
        p = self.root / key[:2] / f"{key}.json"
        if p.exists():
            self.hits += 1
            return json.loads(p.read_text())
        self.misses += 1
        return None

    def put(self, key: str, value: dict) -> None:
        if not self.enabled:
            return
        p = self.root / key[:2] / f"{key}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value))

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {"hits": self.hits, "misses": self.misses,
                "hit_rate": round(self.hits / total, 3) if total else 0.0}


class BaseClient:
    """Shared retry, caching and accounting for concrete backends."""

    model = "unset"
    provider = "unset"

    def __init__(self, model: str, cache: DiskCache | None = None,
                 max_retries: int = 4, timeout: float = 300.0):
        # 300s, not 120s. The disposition treatment at richness 11 shows
        # each agent TEN peers' declared fluxes and rationales, which
        # doubles the prompt (4,239 -> 8,522 chars, ~2,130 tokens) and
        # roughly doubles the generation. At 120s that produced 11 read
        # timeouts in 27 attempts -- a 40% loss to a clock, not to any
        # behaviour of the model.
        self.model = model
        self.cache = cache if cache is not None else DiskCache()
        self.max_retries = max_retries
        self.timeout = timeout
        self.n_calls = 0
        self.total_usage: dict[str, float] = {}

    def _cache_key(self, system: str, user: str, max_tokens: int,
                   temperature: float) -> str:
        return self.cache.key(provider=self.provider, model=self.model, system=system,
                              user=user, max_tokens=max_tokens, temperature=temperature)

    def complete(self, system: str, user: str, *, max_tokens: int = 1024,
                 temperature: float = 0.0, stop: list[str] | None = None) -> LLMResponse:
        key = self._cache_key(system, user, max_tokens, temperature)
        hit = self.cache.get(key)
        if hit is not None:
            return LLMResponse(text=hit["text"], model=self.model,
                               usage=hit.get("usage", {}), cached=True)

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                t0 = time.time()
                text, usage = self._call(system, user, max_tokens, temperature, stop)
                latency = (time.time() - t0) * 1000
                self.n_calls += 1
                for k, v in usage.items():
                    if isinstance(v, (int, float)):
                        self.total_usage[k] = self.total_usage.get(k, 0.0) + v
                self.cache.put(key, {"text": text, "usage": usage, "model": self.model})
                return LLMResponse(text=text, model=self.model, usage=usage,
                                   latency_ms=round(latency, 2))
            except Exception as exc:  # noqa: BLE001 - backends raise many types
                last_err = exc
                if attempt == self.max_retries - 1:
                    break
                # Exponential backoff; rate limits are the common case.
                time.sleep(min(2 ** attempt, 20))
        raise RuntimeError(f"{self.provider}/{self.model} failed after "
                           f"{self.max_retries} attempts: {last_err}") from last_err

    def _call(self, system: str, user: str, max_tokens: int, temperature: float,
              stop: list[str] | None) -> tuple[str, dict]:
        raise NotImplementedError

    def stats(self) -> dict:
        return {"provider": self.provider, "model": self.model, "n_calls": self.n_calls,
                "usage": self.total_usage, "cache": self.cache.stats()}


def build_client(spec: str | None = None, cache_dir: str = ".cache/llm",
                 cache_enabled: bool = True) -> LLMClient:
    """Construct a client from a 'provider:model' string.

    Examples:
      mock                          deterministic stub, no network
      anthropic:claude-sonnet-5     Anthropic API
      openai:gpt-4o-mini            OpenAI API
      local:qwen2.5-7b-instruct     any OpenAI-compatible server (vLLM, Ollama, TGI)
      together:meta-llama/...       Together, OpenAI-compatible
      hf:meta-llama/Llama-3.3-70B-Instruct
                                    Hugging Face router (needs $HF_TOKEN)
    """
    spec = spec or os.environ.get("METABCO_LLM", "mock")
    provider, _, model = spec.partition(":")
    cache = DiskCache(cache_dir, enabled=cache_enabled)

    if provider == "mock":
        from .mock import MockClient
        return MockClient(model or "mock-v1", cache=DiskCache(cache_dir, enabled=False))
    if provider == "anthropic":
        from .anthropic_backend import AnthropicClient
        return AnthropicClient(model or "claude-sonnet-5", cache=cache)
    if provider == "peft":
        # A locally held LoRA: peft:/path/to/adapter[|base_id]. Weights in this
        # process, so the arm must run where a GPU is.
        from .peft_backend import PeftLocalClient
        return PeftLocalClient(model, cache=cache)
    if provider in ("openai", "local", "together", "groq", "openrouter", "vllm",
                    "hf", "huggingface"):
        from .openai_backend import OpenAICompatibleClient
        return OpenAICompatibleClient(model, provider=provider, cache=cache)
    raise ValueError(f"unknown LLM provider {provider!r} in spec {spec!r}")
