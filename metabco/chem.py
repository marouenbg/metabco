"""Chemistry: metabolite identity and elemental bookkeeping.

The simulator refuses to let an organism create carbon out of nothing. That rule
lives here. It is the difference between a physics engine and a story generator:
the LLM may say whatever it likes, but the atoms have to add up.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_FORMULA_RE = re.compile(r"([A-Z][a-z]?)(\d*\.?\d*)")

# Elemental composition of one gram of dry cell weight, from the E. coli biomass
# objective function (Feist et al. 2007). Used to check that the carbon an
# organism claims to consume can actually account for the biomass it claims to
# make. Species differ by a few percent; that is well inside our tolerance.
BIOMASS_ELEMENTS_MMOL_PER_GDW = {"C": 40.0, "N": 10.0, "P": 1.0, "S": 0.2, "O": 20.0, "H": 65.0}


def parse_formula(formula: str | None) -> dict[str, float]:
    """Turn 'C6H12O6' into {'C': 6, 'H': 12, 'O': 6}. Unknown formulas give {}."""
    if not formula:
        return {}
    # Cobra uses 'R' / 'X' placeholders for polymeric residues; those make
    # balance checks meaningless, so we mark the metabolite as unknown instead.
    if any(tok in formula for tok in ("R", "X")):
        return {}
    out: dict[str, float] = {}
    for element, count in _FORMULA_RE.findall(formula):
        if not element:
            continue
        out[element] = out.get(element, 0.0) + (float(count) if count else 1.0)
    return out


@dataclass
class Metabolite:
    met_id: str  # canonical id, BiGG-style base id e.g. 'glc__D'
    name: str
    formula: str | None = None
    charge: int | None = None
    synonyms: tuple[str, ...] = ()

    @property
    def elements(self) -> dict[str, float]:
        return parse_formula(self.formula)

    @property
    def carbon(self) -> float:
        return self.elements.get("C", 0.0)

    @property
    def known(self) -> bool:
        return bool(self.elements)


@dataclass
class MetaboliteRegistry:
    """Canonical metabolite namespace shared by oracle, agents and environment.

    Built from the GEMs themselves so ids always round-trip to real exchange
    reactions, with a synonym layer so an LLM can say 'butyrate' and be
    understood as 'but'.
    """

    mets: dict[str, Metabolite] = field(default_factory=dict)
    _syn: dict[str, str] = field(default_factory=dict)

    def add(self, met: Metabolite) -> None:
        existing = self.mets.get(met.met_id)
        if existing is None:
            self.mets[met.met_id] = met
        elif existing.formula is None and met.formula:
            self.mets[met.met_id] = met
        self._index(self.mets[met.met_id])

    def _index(self, met: Metabolite) -> None:
        keys = [met.met_id, met.name, *met.synonyms]
        for k in keys:
            if k:
                self._syn.setdefault(_norm(k), met.met_id)

    def resolve(self, token: str) -> str | None:
        """Map any name/synonym/id an agent emits onto a canonical id."""
        if token in self.mets:
            return token
        return self._syn.get(_norm(token))

    def get(self, token: str) -> Metabolite | None:
        mid = self.resolve(token)
        return self.mets.get(mid) if mid else None

    def carbon_of(self, token: str) -> float:
        met = self.get(token)
        return met.carbon if met else 0.0

    def elements_of(self, token: str) -> dict[str, float]:
        met = self.get(token)
        return met.elements if met else {}

    def __len__(self) -> int:
        return len(self.mets)

    def __contains__(self, token: str) -> bool:
        return self.resolve(token) is not None

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(
                [
                    {
                        "met_id": m.met_id,
                        "name": m.name,
                        "formula": m.formula,
                        "charge": m.charge,
                        "synonyms": list(m.synonyms),
                    }
                    for m in sorted(self.mets.values(), key=lambda x: x.met_id)
                ],
                indent=1,
            )
        )

    @classmethod
    def load(cls, path: str | Path) -> "MetaboliteRegistry":
        reg = cls()
        for row in json.loads(Path(path).read_text()):
            reg.add(
                Metabolite(
                    met_id=row["met_id"],
                    name=row["name"],
                    formula=row.get("formula"),
                    charge=row.get("charge"),
                    synonyms=tuple(row.get("synonyms", ())),
                )
            )
        return reg


def _norm(s: str) -> str:
    """Loose normalisation so 'D-Glucose', 'd glucose' and 'glc__D' converge."""
    s = s.strip().lower()
    s = re.sub(r"\s*\(.*?\)\s*$", "", s)  # trailing '(extracellular)' etc.
    s = re.sub(r"[\s\-_,'’]+", "", s)
    return s


# Curated synonyms for the metabolites that dominate gut fermentation. LLMs
# reliably use the common English names, and these must land on the right
# exchange reaction or every downstream number is wrong.
COMMON_SYNONYMS: dict[str, tuple[str, ...]] = {
    "glc__D": ("glucose", "d-glucose", "dextrose"),
    "fru": ("fructose", "d-fructose"),
    "gal": ("galactose",),
    "lcts": ("lactose",),
    "sucr": ("sucrose",),
    "malt": ("maltose",),
    "arab__L": ("arabinose", "l-arabinose"),
    "xyl__D": ("xylose", "d-xylose"),
    "rib__D": ("ribose",),
    "man": ("mannose",),
    "rmn": ("rhamnose",),
    "fuc__L": ("fucose", "l-fucose"),
    "acgam": ("n-acetylglucosamine", "glcnac", "nag"),
    "acnam": ("n-acetylneuraminate", "sialic acid", "neu5ac"),
    "inulin": ("inulin",),
    "strch1": ("starch",),
    "ac": ("acetate", "acetic acid"),
    "ppa": ("propionate", "propionic acid", "propanoate"),
    "but": ("butyrate", "butyric acid", "butanoate"),
    "for": ("formate", "formic acid"),
    "lac__D": ("d-lactate", "d-lactic acid"),
    "lac__L": ("l-lactate", "lactate", "lactic acid"),
    "succ": ("succinate", "succinic acid"),
    "etoh": ("ethanol",),
    "co2": ("carbon dioxide",),
    "h2": ("hydrogen", "h2 gas", "dihydrogen"),
    "ch4": ("methane",),
    "h2s": ("hydrogen sulfide",),
    "o2": ("oxygen", "dioxygen", "o2 gas"),
    "nh4": ("ammonium", "ammonia"),
    "pi": ("phosphate", "inorganic phosphate", "orthophosphate"),
    "so4": ("sulfate",),
    "no3": ("nitrate",),
    "no2": ("nitrite",),
    "h2o": ("water",),
    "h": ("proton", "h+"),
    "cit": ("citrate",),
    "fum": ("fumarate",),
    "mal__L": ("malate", "l-malate"),
    "akg": ("alpha-ketoglutarate", "2-oxoglutarate"),
    "pyr": ("pyruvate", "pyruvic acid"),
    "glyc": ("glycerol",),
    "urea": ("urea",),
    "chol": ("choline",),
    "tma": ("trimethylamine", "tma"),
    "btn": ("biotin", "vitamin b7"),
    "fol": ("folate", "vitamin b9"),
    "ribflv": ("riboflavin", "vitamin b2"),
    "thm": ("thiamine", "vitamin b1"),
    "pnto__R": ("pantothenate", "vitamin b5"),
    "nac": ("nicotinate", "niacin", "vitamin b3"),
    "pydxn": ("pyridoxine", "vitamin b6"),
    "cbl1": ("cobalamin", "vitamin b12"),
    "ala__L": ("alanine", "l-alanine"),
    "arg__L": ("arginine", "l-arginine"),
    "asn__L": ("asparagine",),
    "asp__L": ("aspartate", "aspartic acid"),
    "cys__L": ("cysteine",),
    "gln__L": ("glutamine",),
    "glu__L": ("glutamate", "glutamic acid"),
    "gly": ("glycine",),
    "his__L": ("histidine",),
    "ile__L": ("isoleucine",),
    "leu__L": ("leucine",),
    "lys__L": ("lysine",),
    "met__L": ("methionine",),
    "phe__L": ("phenylalanine",),
    "pro__L": ("proline",),
    "ser__L": ("serine",),
    "thr__L": ("threonine",),
    "trp__L": ("tryptophan",),
    "tyr__L": ("tyrosine",),
    "val__L": ("valine",),
}


# BiGG ids whose reconstructions carry no human-readable name. They are all
# trace inorganics, and they are exactly the ones an agent is least likely to
# decode from the id: "mobd" does not read as molybdate. Naming them is the
# point of listing them at all -- Se, W and Mo are formate dehydrogenase
# cofactors and Ni is in hydrogenase, so an agent reasoning about formate or H2
# needs to know they are present.
FALLBACK_NAMES = {
    "mobd": "Molybdate", "slnt": "Selenite", "tungs": "Tungstate",
    "sel": "Selenate", "ni2": "Ni2+", "cbl1": "Cobalamin (B12)",
}


def apply_fallback_names(reg: MetaboliteRegistry) -> None:
    """Give a display name to entries that arrived from a GEM without one."""
    for met_id, name in FALLBACK_NAMES.items():
        met = reg.mets.get(met_id)
        if met is None or (met.name or "").strip():
            continue
        reg.mets[met_id] = Metabolite(
            met_id=met.met_id, name=name, formula=met.formula,
            charge=met.charge, synonyms=met.synonyms)
        reg._index(reg.mets[met_id])


def apply_common_synonyms(reg: MetaboliteRegistry) -> None:
    for met_id, syns in COMMON_SYNONYMS.items():
        met = reg.mets.get(met_id)
        if met is None:
            continue
        reg.mets[met_id] = Metabolite(
            met_id=met.met_id,
            name=met.name,
            formula=met.formula,
            charge=met.charge,
            synonyms=tuple(dict.fromkeys((*met.synonyms, *syns))),
        )
        reg._index(reg.mets[met_id])
    apply_fallback_names(reg)

