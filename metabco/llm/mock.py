"""Deterministic stub backend: exercises the whole pipeline with no network.

It is not pretending to be smart. It parses the rendered environment back out of
the prompt, eats the most abundant carbon source it recognises, ferments it to
acetate, and reports a growth rate proportional to uptake. That is enough to
drive every code path -- rendering, JSON parsing, validation, repair, tracing --
in unit tests and in CI, and it doubles as a floor baseline: any real model that
cannot beat this is not contributing anything.
"""

from __future__ import annotations

import json
import re

from .base import BaseClient

SUGARS = ("glc__D", "fru", "malt", "lcts", "sucr", "gal", "arab__L", "xyl__D",
          "man", "rib__D", "acgam", "ac", "lac__L", "succ", "pyr", "glyc")

CONC_RE = re.compile(r"^\s*[-*]?\s*([A-Za-z0-9_\[\]]+)\s*[:=]\s*([0-9.eE+-]+)", re.M)


class MockClient(BaseClient):
    provider = "mock"

    def _call(self, system: str, user: str, max_tokens: int, temperature: float,
              stop: list[str] | None) -> tuple[str, dict]:
        conc: dict[str, float] = {}
        for met, val in CONC_RE.findall(user):
            try:
                conc[met] = float(val)
            except ValueError:
                continue

        source = next((s for s in SUGARS if conc.get(s, 0.0) > 1e-6), None)
        if source is None:
            payload = {"fluxes": {}, "growth_rate": 0.0,
                       "rationale": "no recognised carbon source present"}
        else:
            uptake = min(10.0, conc[source] * 5.0)
            aerobic = conc.get("o2", 0.0) > 1e-4
            # Six carbons in, two to acetate, the rest to CO2 and biomass.
            fluxes = {source: -round(uptake, 3)}
            if source != "ac":
                fluxes["ac"] = round(uptake * 0.8, 3)
            fluxes["co2"] = round(uptake * (2.4 if aerobic else 0.8), 3)
            payload = {
                "fluxes": fluxes,
                "growth_rate": round(uptake * (0.08 if aerobic else 0.02), 4),
                "rationale": f"consuming {source}; "
                             f"{'respiring' if aerobic else 'fermenting'}",
            }
        text = json.dumps(payload)
        return text, {"input_tokens": len(user) // 4, "output_tokens": len(text) // 4,
                      "total_tokens": (len(user) + len(text)) // 4}
