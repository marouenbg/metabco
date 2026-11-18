"""Distilling genome-scale models into training data.

This is the T3/T4 rung: stop retrieving the reconstruction at inference time and
put it in the weights instead. The pipeline follows the SmileyLlama shape --
supervised fine-tuning, then preference optimisation, then reinforcement learning
-- because that shape fits our setting unusually well. SmileyLlama could score a
generated molecule automatically with a property oracle; we can score a generated
flux distribution automatically with a linear program and a conservation check.
That means every stage can be built without a single human annotation.

Three sources of training signal, in increasing order of value:

  trajectory   (rendered environment -> FBA-optimal flux proposal). The simulator
               generates these in bulk, which is the point: running dFBA over
               many species, media and perturbations *is* the data pipeline.
  capability   (question -> LP-verified answer), the benchmark families
  preference   (chosen, rejected) pairs where "rejected" is a plausible error
               that the validator confirms is genuinely worse

The preference stage deserves care. Corrupting a good answer into obvious noise
teaches a trivial discriminator. The corruptions here are the specific mistakes
an ungrounded model actually makes -- claiming a transporter the genome lacks,
inflating growth past the carbon budget, emitting the wrong fermentation product
-- and each candidate is passed through the validator so a pair is only kept when
the rejection is demonstrably invalid.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

from .chem import MetaboliteRegistry
from .oracle import FBABrain, GEM
from .render import ContextRenderer, RenderConfig, build_system_prompt
from .types import FluxProposal, Observation, SpeciesSpec
from .validate import ConservationValidator, ValidatorConfig

# Products a fermenting organism might plausibly be accused of making. Used to
# build capability-hallucination corruptions that look like real model errors.
PLAUSIBLE_PRODUCTS = ["ac", "ppa", "but", "lac__L", "lac__D", "for", "etoh",
                      "succ", "h2", "co2", "meoh", "isocapr"]


@dataclass
class Example:
    """One chat-format training example."""

    system: str
    user: str
    assistant: str
    meta: dict = field(default_factory=dict)

    def chat(self) -> dict:
        return {"messages": [
            {"role": "system", "content": self.system},
            {"role": "user", "content": self.user},
            {"role": "assistant", "content": self.assistant},
        ], "meta": self.meta}


@dataclass
class PreferencePair:
    system: str
    prompt: str
    chosen: str
    rejected: str
    meta: dict = field(default_factory=dict)

    def record(self) -> dict:
        return {"system": self.system, "prompt": self.prompt,
                "chosen": self.chosen, "rejected": self.rejected, "meta": self.meta}


# Log-spaced signed magnitude bands for exchange fluxes, in mmol/gDW/h.
#
# Emitting `-9.985` as a training target is a mistake, and there is direct
# evidence for it: in the SmileyLlama ablations, constraints of the form
# "exactly k hydrogen-bond donors" were satisfied 14-32% of the time while the
# same constraints stated as ranges were satisfied over 90% of the time. A
# multi-digit decimal is several tokens of spurious precision that the model
# cannot reliably produce and that the underlying biology does not support
# anyway -- flux predictions are not accurate to three decimal places.
#
# Two properties are deliberate. The bands are logarithmic, because metabolic
# fluxes span orders of magnitude and a linear scale would put almost everything
# in one bin. And they *tile the entire line*, including open-ended top bands and
# an explicit "none" band: if a band were missing, silence about a metabolite
# would itself carry information and bias the model at inference.
# "micro" exists because vitamins and cofactors sit in the published diets at
# 1e-4..1e-2 mmol/gDW/h, three decades under the carbon scale the other bands
# were cut for. Without it every vitamin banded as "none" and the model could
# never learn that an organism takes up cobalamin, only that it dies without
# it. The floor is 1e-5: two decades above solver tolerance (~1e-7), under the
# smallest real biomass-driven vitamin fluxes (~2e-5 at mu 0.2).
FLUX_BANDS: list[tuple[float, float, str]] = [
    (0.0, 1e-5, "none"),
    (1e-5, 0.01, "micro"),
    (0.01, 0.1, "trace"),
    (0.1, 1.0, "low"),
    (1.0, 5.0, "moderate"),
    (5.0, 15.0, "high"),
    (15.0, float("inf"), "very_high"),
]
GROWTH_BANDS: list[tuple[float, float, str]] = [
    (0.0, 0.001, "no_growth"),
    (0.001, 0.05, "very_slow"),
    (0.05, 0.2, "slow"),
    (0.2, 0.5, "moderate"),
    (0.5, 1.0, "fast"),
    (1.0, float("inf"), "very_fast"),
]


def _band(value: float, bands: list[tuple[float, float, str]]) -> str:
    v = abs(value)
    for lo, hi, name in bands:
        if lo <= v < hi:
            return name
    return bands[-1][2]


def _band_midpoint(name: str, bands: list[tuple[float, float, str]]) -> float:
    """Geometric midpoint, so a decoded band lands in the middle of its decade."""
    for lo, hi, label in bands:
        if label != name:
            continue
        if hi == float("inf"):
            return lo * 2.0
        if lo <= 0:
            return hi / 2.0
        return (lo * hi) ** 0.5
    return 0.0


def flux_band(value: float) -> str:
    """'consume_high', 'secrete_low', 'none' — direction and magnitude together."""
    band = _band(value, FLUX_BANDS)
    if band == "none":
        return "none"
    return ("secrete_" if value > 0 else "consume_") + band


def decode_flux_band(label: str) -> float:
    label = (label or "").strip().lower()
    if label in ("none", "", "no_exchange"):
        return 0.0
    sign = 1.0
    for prefix, s in (("secrete_", 1.0), ("consume_", -1.0),
                      ("uptake_", -1.0), ("release_", 1.0)):
        if label.startswith(prefix):
            sign, label = s, label[len(prefix):]
            break
    return sign * _band_midpoint(label, FLUX_BANDS)


def growth_band(mu: float) -> str:
    return _band(mu, GROWTH_BANDS)


def decode_growth_band(label: str) -> float:
    label = (label or "").strip().lower()
    if label in ("no_growth", "none", ""):
        return 0.0  # "no growth" must decode to exactly zero, not half a band
    return _band_midpoint(label, GROWTH_BANDS)


# Never asked for as a prediction target. `biomass` is a pseudo-exchange AGORA
# carries for bookkeeping and is not a metabolite anyone trades; water and
# protons move with almost every reaction, so predicting them is free marks that
# crowd out the products the community actually exchanges.
EXCLUDE_FROM_TARGET = {"biomass", "h2o", "h"}
# Bulk salts too. The rendered prompt declares them "present in excess, never
# limiting" and the validator CREDITS them automatically when a proposal needs
# them ("credited 0.131 mmol/gDW/h of so4"), so an agent never has to ask. FBA
# nonetheless assigns each a tiny flux, and 12 of 49 entries in a sample target
# were bulk ions rounding to -0.0 -- teaching the model to emit meaningless
# zeros every turn, and crowding the products that actually matter.
EXCLUDE_FROM_TARGET |= {"na1", "cl", "k", "ca2", "mg2", "fe2", "fe3", "zn2",
                        "cu2", "mn2", "cobalt2", "ni2", "mobd", "slnt",
                        "tungs", "sel", "so4", "pi"}
# Floor shared by BOTH target formats. The banded path already drops anything
# banding to "none"; without the same floor the numeric path kept those and
# rounded them to -0.0, so a numeric-vs-banded comparison would have measured
# a content difference rather than a representation difference.
TARGET_FLOOR = 1e-5


def proposal_json(prop: FluxProposal, rationale: str | None = None,
                  bucketed: bool = True) -> str:
    """Serialise a proposal as an agent is asked to emit it.

    `bucketed` is the default because precise decimals are not learnable (see
    FLUX_BANDS). The numeric form is kept so the two can be compared directly:
    which representation a fine-tuned model predicts better is an experiment, not
    an assumption.
    """
    fluxes = {k: v for k, v in sorted(prop.fluxes.items())
              if k not in EXCLUDE_FROM_TARGET and abs(v) >= TARGET_FLOOR}
    if bucketed:
        body = {
            "exchanges": {k: flux_band(v) for k, v in fluxes.items()
                          if flux_band(v) != "none"},
            "growth": growth_band(prop.growth_rate),
        }
    else:
        body = {
            # 5 dp, not 3: the micro band runs down to 1e-5 and 3 dp would
            # render every vitamin-scale flux as 0.0.
            "fluxes": {k: round(v, 5) for k, v in fluxes.items()},
            "growth_rate": round(prop.growth_rate, 4),
        }
    body["rationale"] = rationale if rationale is not None else prop.rationale
    return json.dumps(body)


# ------------------------------------------------------------ environment sampler

def sample_environments(spec: SpeciesSpec, diet_bounds: dict[str, float], n: int,
                        seed: int = 0, carbon_panel: list[str] | None = None,
                        neighbors_from: list[str] | None = None) -> list[Observation]:
    """Draw diverse chemical situations for an organism to respond to.

    Coverage is the whole point. A model trained only on trajectories from a
    single medium learns that medium, not metabolism, so this varies the carbon
    sources present, their abundance over three orders of magnitude, oxygen, pH,
    spatial position and which neighbours are around. Concentrations are drawn
    log-uniformly because that is how nutrients actually vary, and a linear draw
    would put almost every sample in the saturated regime where every organism
    looks the same.
    """
    from .types import HostContext, Neighbor, Physicochem

    def depth_hint(r):
        """Oxygen only reaches organisms sitting near the epithelium."""
        return r.random()

    rng = random.Random(seed)
    panel = [c for c in (carbon_panel or list(diet_bounds))
             if not spec.exchanges or c in spec.exchanges]
    out: list[Observation] = []
    for i in range(n):
        conc = {m: v * rng.uniform(0.5, 4.0) for m, v in diet_bounds.items()}
        # One to four carbon sources at real concentrations, on top of the diet's
        # trace background. Multiple sources create the preference and diauxie
        # decisions that single-substrate data never exercises.
        for met in rng.sample(panel, min(len(panel), rng.randint(1, 4))):
            conc[met] = 10 ** rng.uniform(-2, 1.3)
        conc.update({"h2o": 1000.0, "h": 1000.0, "pi": 20.0, "nh4": 20.0,
                     "so4": 10.0, "co2": 1.0})
        # Oxygen exposure has to respect what the organism actually is. AGORA
        # reconstructions give most gut anaerobes an oxygen exchange for
        # detoxification, so an unconditional aerobic draw makes flux balance
        # analysis hand back a happily respiring Bacteroides -- and training on
        # that teaches the model the opposite of the organism's defining trait.
        # Obligate anaerobes see only the trace oxygen that leaks from the
        # epithelium; facultative organisms see the full range.
        if spec.aerotolerance == "anaerobe":
            aerobic = rng.random() < 0.15 and depth_hint(rng) < 0.5
            conc["o2"] = 10 ** rng.uniform(-5, -3.5) if aerobic else 0.0
        else:
            aerobic = rng.random() < 0.4
            conc["o2"] = 10 ** rng.uniform(-3, -0.5) if aerobic else 0.0
        depth = rng.choice([0.0, 0.4, 1.0])
        neighbors = []
        for other in (neighbors_from or []):
            if other == spec.species_id or rng.random() > 0.5:
                continue
            neighbors.append(Neighbor(
                species_id=other,
                biomass_gdw_per_l=10 ** rng.uniform(-3, -0.5),
                secretions={m: round(rng.uniform(0.2, 4.0), 2)
                            for m in rng.sample(PLAUSIBLE_PRODUCTS, rng.randint(1, 3))},
                in_contact=rng.random() < 0.4,
            ))
        out.append(Observation(
            species_id=spec.species_id, turn=i, time_h=i * 0.5, dt_h=0.5,
            substep_h=0.05,
            concentrations={m: c for m, c in conc.items() if c > 0},
            biomass_gdw_per_l=10 ** rng.uniform(-3, -0.5),
            patch_id={0.0: "mucosa", 0.4: "mucus", 1.0: "lumen"}[depth],
            physio=Physicochem(ph=round(rng.uniform(5.5, 7.4), 2),
                               dilution_per_h=rng.choice([0.0, 0.05, 0.15, 0.3])),
            neighbors=neighbors,
            host=HostContext(
                depth_from_wall=depth,
                mucus_thickness_um=0.0 if depth > 0.7 else rng.uniform(20, 120),
                mucin_release=({} if depth > 0.7 else
                               {"acgam": 0.3, "gal": 0.2, "fuc__L": 0.1}),
                oxygen_flux=0.0 if depth > 0.7 else rng.uniform(0.0, 0.06),
                absorption={"but": 0.8} if depth < 0.2 else {},
            ),
        ))
    return out


# --------------------------------------------------------------- SFT: trajectory

def trajectory_examples(gem: GEM, spec: SpeciesSpec, reg: MetaboliteRegistry,
                        observations: list[Observation],
                        render_config: RenderConfig | None = None,
                        brain: FBABrain | None = None,
                        bucketed: bool = True) -> list[Example]:
    """Teach the mapping from environment to flux distribution.

    These are the examples that actually train the agent's job. Everything else
    trains it to answer questions about biology; only these train it to behave
    like a cell in a specific chemical situation.
    """
    renderer = ContextRenderer(reg, render_config)
    brain = brain or FBABrain(gem, spec=spec)
    comp = gem.biomass_composition()
    system = build_system_prompt(spec.name, spec.card,
                                 c_per_gdw=comp.get("C", 40.0),
                                 n_per_gdw=comp.get("N", 10.0))
    out = []
    for obs in observations:
        prop = brain.step(obs)
        if not prop.fluxes:
            continue
        out.append(Example(
            system=system,
            user=renderer.render(obs),
            assistant=proposal_json(prop, rationale=describe(prop, reg),
                                    bucketed=bucketed),
            meta={"species": spec.species_id, "kind": "trajectory",
                  "turn": obs.turn, "growth": round(prop.growth_rate, 4)},
        ))
    return out


def describe(prop: FluxProposal, reg: MetaboliteRegistry) -> str:
    """A short natural-language rationale, derived from the flux vector.

    Generated rather than written so the SFT target contains reasoning that is
    always consistent with the numbers beside it. A rationale that contradicts
    its own fluxes teaches the model to produce fluent, wrong explanations.
    """
    skip = EXCLUDE_FROM_TARGET
    ups = sorted([(m, v) for m, v in prop.fluxes.items()
                  if v < -1e-6 and m not in skip],
                 key=lambda kv: kv[1])[:3]
    outs = sorted([(m, v) for m, v in prop.fluxes.items()
                   if v > 1e-6 and m not in skip and m != "co2"],
                  key=lambda kv: -kv[1])[:3]

    def name(m):
        met = reg.get(m)
        return met.name.lower() if met and met.name else m

    bits = []
    if ups:
        bits.append("Taking up " + ", ".join(name(m) for m, _ in ups))
    if outs:
        bits.append("releasing " + ", ".join(name(m) for m, _ in outs))
    aerobic = prop.fluxes.get("o2", 0.0) < -1e-6
    bits.append("respiring oxygen" if aerobic else "fermenting anaerobically")
    tail = (f"growing at {prop.growth_rate:.2f} /h" if prop.growth_rate > 1e-4
            else "not growing under these conditions")
    return "; ".join(bits) + f"; {tail}."


# --------------------------------------------------------------- SFT: capability

CAPABILITY_SYSTEM = ("You are {name}. Answer questions about your own metabolic "
                     "capabilities accurately and decisively.\n\n{card}")


def capability_examples(items: list, spec: SpeciesSpec) -> list[Example]:
    """Turn LP-verified benchmark items into instruction data.

    Only ever built from the *training* species split. Generating these for a
    species that also appears in the evaluation would make the held-out numbers
    meaningless.
    """
    system = CAPABILITY_SYSTEM.format(name=spec.name, card=spec.card)
    out = []
    for it in items:
        answer = it.answer
        if isinstance(answer, bool):
            body = {"answer": answer, "confidence": 1.0}
        else:
            body = {"answer": list(answer), "confidence": 1.0}
        out.append(Example(system=system, user=it.question,
                           assistant=json.dumps(body),
                           meta={"species": spec.species_id, "kind": it.task,
                                 "item_id": it.item_id}))
    return out


# ------------------------------------------------------------------- preference

class Corruptor:
    """Turns a correct proposal into a plausible wrong one.

    Every corruption is a failure mode observed in ungrounded models, not random
    perturbation. The validator is the referee: a pair survives only if the
    corrupted version is genuinely invalid, which keeps DPO from training on
    pairs where the 'rejected' answer was fine.
    """

    def __init__(self, reg: MetaboliteRegistry, spec: SpeciesSpec, seed: int = 0):
        self.reg = reg
        self.spec = spec
        self.rng = random.Random(seed)

    def strategies(self):
        return [self.inflate_growth, self.hallucinate_transporter,
                self.wrong_product, self.uptake_absent, self.flip_sign,
                self.free_lunch]

    def inflate_growth(self, prop: FluxProposal, obs: Observation) -> tuple[FluxProposal, str]:
        f = FluxProposal(prop.species_id, prop.turn, dict(prop.fluxes),
                         max(prop.growth_rate, 0.05) * self.rng.uniform(3, 8))
        return f, "growth inflated beyond the carbon taken up"

    def hallucinate_transporter(self, prop, obs):
        absent = [m for m in PLAUSIBLE_PRODUCTS
                  if self.spec.exchanges and m not in self.spec.exchanges]
        if not absent:
            return None, ""
        met = self.rng.choice(absent)
        f = dict(prop.fluxes)
        f[met] = abs(self.rng.uniform(1, 5))
        return FluxProposal(prop.species_id, prop.turn, f, prop.growth_rate), \
            f"secretes {met}, for which this genome encodes no transporter"

    def wrong_product(self, prop, obs):
        outs = [m for m, v in prop.fluxes.items() if v > 1e-6 and m != "co2"]
        if not outs:
            return None, ""
        f = dict(prop.fluxes)
        met = self.rng.choice(outs)
        # Move the carbon onto a different product without changing uptake, so
        # the elemental books no longer balance.
        f[met] = f[met] * self.rng.uniform(2.5, 6)
        return FluxProposal(prop.species_id, prop.turn, f, prop.growth_rate), \
            f"over-secretes {met} relative to the carbon consumed"

    def uptake_absent(self, prop, obs):
        present = set(obs.concentrations)
        candidates = [m for m in (self.spec.exchanges or ()) if m not in present]
        if not candidates:
            return None, ""
        met = self.rng.choice(candidates[:200])
        f = dict(prop.fluxes)
        f[met] = -abs(self.rng.uniform(1, 6))
        return FluxProposal(prop.species_id, prop.turn, f, prop.growth_rate), \
            f"consumes {met}, which is not present in the environment"

    def flip_sign(self, prop, obs):
        ups = [m for m, v in prop.fluxes.items() if v < -1e-6]
        if not ups:
            return None, ""
        f = dict(prop.fluxes)
        met = self.rng.choice(ups)
        f[met] = -f[met]
        return FluxProposal(prop.species_id, prop.turn, f, prop.growth_rate), \
            f"secretes {met} instead of consuming it"

    def free_lunch(self, prop, obs):
        f = {m: v for m, v in prop.fluxes.items() if v > 0}
        if not f:
            return None, ""
        return FluxProposal(prop.species_id, prop.turn, f,
                            max(prop.growth_rate, 0.1)), \
            "produces and grows while taking up nothing at all"


def preference_pairs(gem: GEM, spec: SpeciesSpec, reg: MetaboliteRegistry,
                     observations: list[Observation],
                     render_config: RenderConfig | None = None,
                     seed: int = 0, per_obs: int = 2,
                     include_suboptimal: bool = True,
                     bucketed: bool = True) -> list[PreferencePair]:
    """Build (chosen, rejected) pairs with an automatic referee.

    Two flavours, and both matter. *Validity* pairs teach the model not to break
    physics. *Optimality* pairs teach it to behave like a cell under selection:
    the rejected side is fully valid but grows more slowly, which is a preference
    no conservation check could ever express.
    """
    renderer = ContextRenderer(reg, render_config)
    brain = FBABrain(gem, spec=spec)
    comp = gem.biomass_composition()
    system = build_system_prompt(spec.name, spec.card,
                                 c_per_gdw=comp.get("C", 40.0),
                                 n_per_gdw=comp.get("N", 10.0))
    validator = ConservationValidator(reg, ValidatorConfig(mode="log_only"),
                                      {spec.species_id: spec},
                                      {spec.species_id: comp})
    corruptor = Corruptor(reg, spec, seed)
    rng = random.Random(seed)
    pairs: list[PreferencePair] = []

    for obs in observations:
        good = brain.step(obs)
        if not good.fluxes or good.growth_rate <= 1e-4:
            continue
        prompt = renderer.render(obs)
        chosen = proposal_json(good, rationale=describe(good, reg), bucketed=bucketed)

        strategies = corruptor.strategies()[:]
        rng.shuffle(strategies)
        made = 0
        for strat in strategies:
            if made >= per_obs:
                break
            bad, why = strat(good, obs)
            if bad is None:
                continue
            # The referee: keep the pair only if the corruption really is invalid.
            if validator.validate(bad, obs).ok:
                continue
            pairs.append(PreferencePair(
                system=system, prompt=prompt, chosen=chosen,
                rejected=proposal_json(bad, rationale=describe(bad, reg),
                                       bucketed=bucketed),
                meta={"species": spec.species_id, "kind": "validity",
                      "corruption": strat.__name__, "why": why},
            ))
            made += 1

        if include_suboptimal:
            sub = suboptimal_proposal(gem, spec, obs, brain, fraction=0.5)
            if sub is not None and sub.growth_rate < good.growth_rate * 0.9:
                pairs.append(PreferencePair(
                    system=system, prompt=prompt, chosen=chosen,
                    rejected=proposal_json(sub, rationale=describe(sub, reg),
                                           bucketed=bucketed),
                    meta={"species": spec.species_id, "kind": "optimality",
                          "chosen_growth": round(good.growth_rate, 4),
                          "rejected_growth": round(sub.growth_rate, 4)},
                ))
    return pairs


def suboptimal_proposal(gem: GEM, spec: SpeciesSpec, obs: Observation,
                        brain: FBABrain, fraction: float = 0.5) -> FluxProposal | None:
    """A valid but slower flux distribution: the 'lazy cell' negative.

    Obtained by capping growth at a fraction of the optimum and re-solving
    parsimoniously, so the result is a genuine feasible steady state rather than
    a scaled-down copy of the optimum.
    """
    bounds = brain.bounds_for(obs)
    with gem.model:
        gem.set_medium(bounds)
        # The cap must match the brain's, or this solves a different problem:
        # an uncapped half-optimum can easily exceed the capped optimum, the
        # "slower" negative comes out faster than the positive, and every
        # optimality pair is silently discarded.
        if brain.kinetics.max_carbon_uptake:
            gem.add_carbon_cap(brain.kinetics.max_carbon_uptake, brain._reg)
        mu = gem.growth()
        if mu is None or mu <= 1e-4:
            return None
        bm = gem.model.reactions.get_by_id(gem.biomass_reaction)
        bm.bounds = (fraction * mu, fraction * mu)
        mu2, fluxes = gem.solve(parsimonious=True)
    if not fluxes:
        return None
    return FluxProposal(spec.species_id, obs.turn, fluxes, fraction * mu,
                        "a slower, less efficient use of the same nutrients")


# ---------------------------------------------------------------------- writing

def write_sft(examples: list[Example], path: str | Path) -> Path:
    """Chat-format JSONL, the shape axolotl / TRL / LLaMA-Factory all accept."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as fh:
        for ex in examples:
            fh.write(json.dumps(ex.chat()) + "\n")
    return p


def write_dpo(pairs: list[PreferencePair], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as fh:
        for pair in pairs:
            fh.write(json.dumps(pair.record()) + "\n")
    return p


def split_by_species(examples: list, train_species: set[str]) -> tuple[list, list]:
    """Split held-out by SPECIES, never by item.

    The claim we ultimately want to test is that a fine-tuned model generalises to
    organisms with no reconstruction at all. A random item split would let the
    model memorise each organism from its training questions and would report a
    number that means nothing for that claim.
    """
    train = [e for e in examples if e.meta.get("species") in train_species]
    test = [e for e in examples if e.meta.get("species") not in train_species]
    return train, test
