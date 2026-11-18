"""OpenAI-compatible backend.

Deliberately covers vLLM, Ollama, Together, Groq and OpenRouter as well as
OpenAI itself, because they all speak the same wire format. That is what lets a
fine-tuned open checkpoint be dropped into an experiment with only an env-var
change, which is the whole point of the tier ladder.

Uses plain HTTP rather than the vendor SDK so the only dependency is httpx,
which is already present wherever cobra is.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .base import BaseClient

# Where each provider's OpenAI-compatible endpoint lives, and which env var
# holds its key.
ENDPOINTS = {
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "together": ("https://api.together.xyz/v1", "TOGETHER_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    # Hugging Face's router is OpenAI-compatible and fans out to Together,
    # Fireworks, Nebius and friends behind one key, so an open checkpoint needs
    # no new backend -- only an endpoint. Model ids are full repo paths, e.g.
    # hf:meta-llama/Llama-3.3-70B-Instruct. Appending ":together" or similar to
    # the model id pins one provider instead of letting the router choose.
    "hf": ("https://router.huggingface.co/v1", "HF_TOKEN"),
    "huggingface": ("https://router.huggingface.co/v1", "HF_TOKEN"),
    "local": ("http://localhost:8000/v1", "METABCO_LOCAL_KEY"),
    "vllm": ("http://localhost:8000/v1", "METABCO_LOCAL_KEY"),
}


class OpenAICompatibleClient(BaseClient):
    def __init__(self, model: str, provider: str = "openai", base_url: str | None = None,
                 api_key: str | None = None, **kw):
        super().__init__(model, **kw)
        self.provider = provider
        default_url, key_var = ENDPOINTS.get(provider, ENDPOINTS["openai"])
        self.base_url = (base_url or os.environ.get("METABCO_BASE_URL") or
                         default_url).rstrip("/")
        # Local servers usually accept any key; remote ones do not.
        self.api_key = api_key or os.environ.get(key_var) or (
            "local" if provider in ("local", "vllm") else None)
        if not self.api_key:
            raise RuntimeError(
                f"no API key for provider {provider!r}: set ${key_var}")

    def _call(self, system: str, user: str, max_tokens: int, temperature: float,
              stop: list[str] | None) -> tuple[str, dict]:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if stop:
            payload["stop"] = stop
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}",
                     # urllib's default UA ("Python-urllib/3.x") is blocked at
                     # the edge by some providers -- Hugging Face's router
                     # returns a Cloudflare 403 HTML page for it while the
                     # identical request with any ordinary UA returns 200.
                     # Verified by curl A/B against the same endpoint and key.
                     "User-Agent": "metabco/0.1 (+https://github.com/metabco)"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read()[:500].decode(errors='replace')}") from e
        text = body["choices"][0]["message"]["content"] or ""
        usage = body.get("usage", {}) or {}
        return text, {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }
