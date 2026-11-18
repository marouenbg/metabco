"""Anthropic backend over plain HTTP (no SDK dependency)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .base import BaseClient

DEFAULT_BASE = "https://api.anthropic.com"
API_VERSION = "2023-06-01"


class AnthropicClient(BaseClient):
    provider = "anthropic"

    def __init__(self, model: str = "claude-sonnet-5", api_key: str | None = None,
                 base_url: str | None = None, **kw):
        super().__init__(model, **kw)
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
        self.base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL")
                         or DEFAULT_BASE).rstrip("/")
        if not self.api_key:
            raise RuntimeError("no ANTHROPIC_API_KEY set")

    def _call(self, system: str, user: str, max_tokens: int, temperature: float,
              stop: list[str] | None) -> tuple[str, dict]:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if stop:
            payload["stop_sequences"] = stop
        req = urllib.request.Request(
            f"{self.base_url}/v1/messages",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "x-api-key": self.api_key,
                     "anthropic-version": API_VERSION,
                     # Identity-linked keys must name the workspace they act in
                     # or the API returns 400. Ordinary keys ignore the header,
                     # so sending it whenever it is configured is harmless.
                     **({"anthropic-workspace-id": self.workspace}
                        if self.workspace else {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read()[:500].decode(errors='replace')}") from e
        parts = [b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"]
        usage = body.get("usage", {}) or {}
        return "".join(parts), {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        }
