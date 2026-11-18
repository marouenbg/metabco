"""In-process client for a locally fine-tuned LoRA adapter.

The other backends POST to an HTTP endpoint. This one holds the weights: an 8B
base plus a LoRA, loaded once and reused for every call, so the Phase 1 sweep can
run a fine-tuned arm on a GPU node without standing up a server.

Spec form:  peft:/abs/path/to/adapter_dir[|base_model_id]
The base defaults to what the adapter's own adapter_config.json records, which is
the only place that cannot drift out of step with the weights.

Loading is deferred to the first call, so building a client costs nothing until
an arm actually runs -- dm38_mono.py constructs one per arm per turn.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .base import BaseClient

_CACHE: dict[str, tuple] = {}          # adapter path -> (tokenizer, model)


def _load(adapter: str, base: str | None):
    """Load base+adapter once per process. Repeated arms reuse the same weights."""
    if adapter in _CACHE:
        return _CACHE[adapter]
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from peft import PeftModel

    cfg = Path(adapter) / "adapter_config.json"
    if base is None and cfg.exists():
        base = json.loads(cfg.read_text()).get("base_model_name_or_path")
    if not base:
        raise ValueError(f"no base model recorded in {cfg}; pass it as peft:{adapter}|<base>")

    tok = AutoTokenizer.from_pretrained(base)
    model = AutoModelForCausalLM.from_pretrained(
        base, dtype=torch.bfloat16,
        device_map="cuda" if torch.cuda.is_available() else "cpu")
    model = PeftModel.from_pretrained(model, adapter)
    model.eval()
    _CACHE[adapter] = (tok, model)
    return tok, model


class PeftLocalClient(BaseClient):
    """Generate from base+LoRA in this process, no network."""

    provider = "peft"

    def __init__(self, model: str, cache=None, **kw):
        adapter, _, base = model.partition("|")
        self.adapter = os.path.abspath(adapter)
        self.base = base or None
        super().__init__(model=model, cache=cache, **kw)

    def _call(self, system: str, user: str, max_tokens: int, temperature: float,
              stop: list[str] | None) -> tuple[str, dict]:
        import torch
        tok, model = _load(self.adapter, self.base)
        msgs = ([{"role": "system", "content": system}] if system else []) \
            + [{"role": "user", "content": user}]
        text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        # add_special_tokens=False: apply_chat_template already emitted the BOS, and
        # a second one puts the model off-manifold -- it degenerates and looks like
        # a model failure. Cost a retracted finding once already.
        ids = tok(text, return_tensors="pt", add_special_tokens=False).to(model.device)
        with torch.no_grad():
            out = model.generate(
                **ids, max_new_tokens=max_tokens,
                do_sample=temperature > 0, temperature=temperature or None,
                top_p=0.95 if temperature > 0 else None,
                pad_token_id=tok.eos_token_id)
        n_in = ids["input_ids"].shape[1]
        reply = tok.decode(out[0][n_in:], skip_special_tokens=True)
        return reply, {"input_tokens": int(n_in),
                       "output_tokens": int(out.shape[1] - n_in),
                       "total_tokens": int(out.shape[1])}
