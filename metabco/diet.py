"""Published diet definitions, loaded from QIIME2 artifacts.

Constructing a defined medium by hand looks reasonable and produces artefacts:
gut anaerobes need dipeptides rather than free amino acids, meso-diaminopimelate
for their cell wall, NMN, haem, and a dozen other things a person writing a list
from memory will omit. Every omission turns into a false "cannot grow" that gets
scored as biology.

So the media come from the published VMH/MICOM diet files instead. They are
citable, reproducible, and known to support growth of the AGORA reconstructions.
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .oracle import ex_to_met

DIET_DIR = "data/media"
DIETS = {
    "western": "western_diet_gut_agora.qza",
    "high_fiber": "vmh_high_fiber_agora.qza",
    "eu_average": "vmh_eu_average_agora.qza",
}


@dataclass
class Diet:
    name: str
    bounds: dict[str, float]  # metabolite id (BiGG namespace) -> max uptake

    def without(self, mets: set[str] | list[str]) -> "Diet":
        drop = set(mets)
        return Diet(f"{self.name}-minus{len(drop)}",
                    {m: v for m, v in self.bounds.items() if m not in drop})

    def plus(self, mets: dict[str, float]) -> "Diet":
        return Diet(self.name, {**self.bounds, **mets})

    def scaled(self, factor: float) -> "Diet":
        return Diet(f"{self.name}x{factor}", {m: v * factor for m, v in self.bounds.items()})

    def __len__(self) -> int:
        return len(self.bounds)


def load_diet(name: str = "western", diet_dir: str = DIET_DIR) -> Diet:
    path = Path(diet_dir) / DIETS.get(name, name)
    with zipfile.ZipFile(path) as z:
        member = next(n for n in z.namelist() if n.endswith("medium.csv"))
        rows = list(csv.DictReader(io.StringIO(z.read(member).decode())))
    bounds: dict[str, float] = {}
    for r in rows:
        # 'global_id' is the exchange id in the reconstruction's own namespace.
        met = ex_to_met(r.get("global_id") or r["reaction"])
        try:
            flux = abs(float(r["flux"]))
        except (TypeError, ValueError):
            continue
        bounds[met] = max(bounds.get(met, 0.0), flux)
    return Diet(name, bounds)


def restrict_to_model(diet: Diet, exchanges: dict[str, str]) -> dict[str, float]:
    """Keep only what this organism can actually take up."""
    return {m: v for m, v in diet.bounds.items() if m in exchanges}
