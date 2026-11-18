"""Rendering the environment into an agent's context window.

This module is where the project's central claim becomes an engineering choice.
If the context window *is* the environment, then how the environment is
serialised is not a formatting detail -- it is the sensory apparatus of the
organism, and it is a first-class experimental variable.

So the renderer is configurable by layer, and the layers are exactly the ones a
cell could plausibly transduce: chemical, physicochemical, social, host,
temporal. Ablating a layer is a real experiment ("does an agent need to know the
epithelium is there to behave correctly near it?"), not a prompt tweak.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .chem import FALLBACK_NAMES, MetaboliteRegistry
from .types import Observation

# Metabolites present in every medium at high concentration. Listing them
# consumes context and teaches the agent nothing, so they are summarised.
BULK = {"h2o", "h", "pi", "so4", "k", "na1", "cl", "ca2", "mg2", "fe2", "fe3",
        "mn2", "zn2", "cu2", "cobalt2", "mobd", "ni2", "sel", "slnt", "tungs"}


@dataclass
class RenderConfig:
    """Which layers of the niche the organism can sense, and how sharply."""

    chemical: bool = True
    physicochemical: bool = True
    social: bool = True
    host: bool = True
    temporal: bool = True
    # Cap the metabolite list. Real transporters are limited and so is context;
    # this also stops a 200-exchange AGORA model from flooding the window.
    max_metabolites: int = 40
    use_names: bool = True  # 'glucose' rather than 'glc__D'
    show_neighbor_biomass: bool = False  # a cell cannot count its neighbours directly
    # How much of its neighbours an agent may perceive. Identity is even less
    # sensable than density, and naming these particular species hands the model
    # a literature prior: Clark 2021 is published and these 25 gut anaerobes are
    # among the best studied there are, so "you are F. prausnitzii beside
    # B. theta" can be answered from recall instead of from metabolism, and
    # emergence stops being distinguishable from memorisation.
    #   "pooled"    no neighbour block. The medium already carries everyone's
    #               secretions, mixed; a cell senses acetate, not whose acetate.
    #               Removes attribution, not information.
    #   "anonymous" secretions attributed to "organism 2" -- others exist, but
    #               taxonomy is withheld.
    #   "named"     full species ids. The memorisation contrast.
    neighbour_visibility: str = "named"      # default keeps existing callers
    quantitative: bool = True  # False -> coarse bins, testing numeric dependence
    detection_limit: float = 1e-4  # mmol/L below which nothing is sensed


def _o2_label(o2: float) -> str:
    """Describe dissolved oxygen the way a physiologist would.

    The old threshold called anything above 1 uM "aerobic". Air-saturated water
    at 37 C holds ~0.21 mM, so a colon lumen at 0.005 mM is anoxic for every
    practical purpose — yet 24 obligate anaerobes were simultaneously told their
    environment was aerobic and that its redox potential was -200 mV. One of them
    said so unprompted. Anchoring the words to real numbers stops the report
    contradicting itself.
    """
    if o2 < 1e-3:
        return "anaerobic"
    if o2 < 0.02:
        return "essentially anoxic — trace oxygen only, far below air saturation"
    if o2 < 0.08:
        return "microaerobic"
    return "aerobic"


def _bin(c: float) -> str:
    if c >= 5.0:
        return "abundant"
    if c >= 0.5:
        return "moderate"
    if c >= 0.05:
        return "scarce"
    return "trace"


class ContextRenderer:
    """Turns an Observation into the text an agent actually sees."""

    def __init__(self, registry: MetaboliteRegistry, config: RenderConfig | None = None):
        self.reg = registry
        self.cfg = config or RenderConfig()

    def renderable(self, met: str) -> bool:
        """Only offer what the agent can name back to us.

        The report is a contract: an id shown here must round-trip through the
        registry when the agent echoes it. Two ids in the western diet (amp,
        ddca) did not, so agents that used them were charged with
        `unknown_metabolite` for quoting the prompt verbatim.
        """
        return self.reg.resolve(met) is not None

    def label(self, met: str) -> str:
        if not self.cfg.use_names:
            return met
        m = self.reg.get(met)
        if m is None:
            # Trace inorganics reach the patch from the medium without ever
            # entering the registry, because no GEM carries an exchange for
            # them. They still have to be readable: "mobd" does not decode as
            # molybdate, and Mo/W/Se are formate dehydrogenase cofactors.
            fb = FALLBACK_NAMES.get(met)
            return f"{fb} ({met})" if fb else met
        name = (m.name or met).strip()
        # Keep the id alongside the name so the agent can echo an id we can parse
        # without ambiguity, while still reading a word it recognises.
        return f"{name} ({met})" if name.lower() != met.lower() else met

    def render(self, obs: Observation) -> str:
        blocks: list[str] = [
            f"# Environment report — turn {obs.turn}, t = {obs.time_h:.2f} h",
            f"You are {obs.species_id}, at {obs.biomass_gdw_per_l:.4g} gDW/L "
            f"in the '{obs.patch_id}' compartment.",
        ]
        if self.cfg.chemical:
            blocks.append(self._chemical(obs))
        if self.cfg.physicochemical:
            blocks.append(self._physio(obs))
        if self.cfg.social:
            social = self._social(obs)
            if social:                # "pooled" returns "": no empty heading
                blocks.append(social)
        if self.cfg.host:
            blocks.append(self._host(obs))
        if self.cfg.temporal:
            blocks.append(self._temporal(obs))
        blocks.append(self._task(obs))
        return "\n\n".join(b for b in blocks if b)

    # ------------------------------------------------------------------ layers

    def _chemical(self, obs: Observation) -> str:
        items = [(m, c) for m, c in obs.concentrations.items()
                 if c > self.cfg.detection_limit and m not in BULK
                 and self.renderable(m)]
        items.sort(key=lambda kv: -kv[1])
        clipped = items[: self.cfg.max_metabolites]
        lines = ["## Available nutrients (mmol/L)"]
        for met, c in clipped:
            if self.cfg.quantitative:
                lines.append(f"- {self.label(met)}: {c:.4g}")
            else:
                lines.append(f"- {self.label(met)}: {_bin(c)}")
        if not clipped:
            lines.append("- (nothing above detection limit)")
        if len(items) > len(clipped):
            # Name them. The cap exists to bound tokens, not to withhold the
            # composition of the medium: whether a micronutrient is PRESENT is a
            # decision-relevant fact, while its exact concentration mostly is
            # not. Anonymising the tail made agents guess, and they guessed
            # wrong -- B. theta asserted "the absence of cobalamin in this
            # medium" and set its succinate/propionate split on that, with
            # cobalamin sitting unnamed in the hidden tail. Succinate is one of
            # the four metabolites Clark measured.
            rest = ", ".join(self.label(m) for m, _ in items[len(clipped):])
            floor = clipped[-1][1] if clipped else self.cfg.detection_limit
            lines.append(f"- also present, each below {floor:.2g} mM: {rest}")
        # Name the bulk set too, for the same reason the trace tail is named:
        # "bulk salts" reads as NaCl and phosphate, and nothing infers sodium
        # selenite or tungstate from it. These are not decoration --
        # SULFATE is the terminal electron acceptor for the only sulfate
        # reducer in the roster (D. piger's agent had to guess it was there and
        # said so), COBALT is the metal centre of B12, Se/W/Mo are formate
        # dehydrogenase cofactors and Ni is in hydrogenase. Concentrations are
        # withheld deliberately: these are non-limiting, and a number would
        # invite an agent to ration against it.
        bulk = [m for m in obs.concentrations
                if m in BULK and obs.conc_of(m) > 0 and m not in ("h2o", "h")] \
            if hasattr(obs, "conc_of") else [
                m for m, c in obs.concentrations.items()
                if m in BULK and c > 0 and m not in ("h2o", "h")]
        if bulk:
            names = ", ".join(self.label(m) for m in sorted(bulk))
            lines.append(f"Present in excess, never limiting: {names}. "
                         "Water is unlimited.")
        else:
            lines.append("Bulk salts, water and phosphate are present in excess.")
        return "\n".join(lines)

    def _physio(self, obs: Observation) -> str:
        p = obs.physio
        out = [
            "## Conditions",
            f"- pH: {p.ph:.2f}",
            f"- temperature: {p.temperature_c:.0f} C",
            f"- redox potential: {p.redox_mv:.0f} mV",
        ]
        o2 = obs.concentrations.get("o2", 0.0)
        # Oxygen deserves its own line rather than being one nutrient among
        # forty: for a gut anaerobe it is the difference between growth and death.
        out.append(f"- dissolved oxygen: {o2:.4g} mmol/L "
                   f"({_o2_label(o2)})")
        if p.dilution_per_h > 0:
            out.append(f"- washout (transit) rate: {p.dilution_per_h:.3g} /h — you must "
                       f"grow faster than this to persist here")
        return "\n".join(out)

    def _social(self, obs: Observation) -> str:
        if self.cfg.neighbour_visibility == "pooled":
            return ""                     # the medium block already carries their output
        if not obs.neighbors:
            return "## Neighbours\n- none detected; you are growing in isolation"
        anon = self.cfg.neighbour_visibility == "anonymous"
        lines = ["## Neighbours"]
        for i, n in enumerate(obs.neighbors, 1):
            bits = [f"- organism {i}" if anon else f"- {n.species_id}"]
            if self.cfg.show_neighbor_biomass:
                bits.append(f"density {n.biomass_gdw_per_l:.4g} gDW/L")
            if n.in_contact:
                bits.append("in physical contact")
            if n.secretions:
                top = sorted(n.secretions.items(), key=lambda kv: -kv[1])[:5]
                bits.append("releasing " + ", ".join(
                    f"{self.label(m)} ({v:.3g})" for m, v in top))
            else:
                bits.append("no detectable release last turn")
            lines.append(": ".join([bits[0], "; ".join(bits[1:])]) if len(bits) > 1 else bits[0])
        if obs.signals:
            lines.append("- diffusible signals: " + ", ".join(
                f"{k}={v:.3g}" for k, v in obs.signals.items()))
        return "\n".join(lines)

    def _host(self, obs: Observation) -> str:
        h = obs.host
        where = ("adherent to the epithelium" if h.depth_from_wall < 0.2
                 else "in the mucus layer" if h.depth_from_wall < 0.7
                 else "in the lumen, away from the wall")
        lines = ["## Host environment", f"- location: {where}"]
        if h.mucus_thickness_um > 0:
            lines.append(f"- mucus layer: {h.mucus_thickness_um:.0f} um thick; "
                         f"host mucin glycans are available as a carbon source here")
        if h.oxygen_flux > 0:
            lines.append("- oxygen is diffusing out of the host tissue at this depth")
        if h.absorption:
            lines.append("- the epithelium is absorbing: " + ", ".join(
                self.label(m) for m in h.absorption))
        if h.bile_acids_mm > 0:
            lines.append(f"- bile acids: {h.bile_acids_mm:.3g} mM")
        if h.antimicrobial_peptides > 0:
            lines.append(f"- host antimicrobial peptides present "
                         f"(level {h.antimicrobial_peptides:.2g})")
        if h.immune_tone != "homeostatic":
            lines.append(f"- host immune state: {h.immune_tone}")
        return "\n".join(lines)

    def _temporal(self, obs: Observation) -> str:
        lines = []
        if obs.events:
            lines.append("## Events this turn\n- " + "\n- ".join(obs.events))
        if obs.history:
            rows = ["## Your recent behaviour"]
            for h in obs.history:
                top = ", ".join(f"{self.label(m)} {v:+.2f}" for m, v in h["fluxes"].items())
                rows.append(f"- turn {h['turn']}: growth {h['growth']:.3f} /h; {top}")
            lines.append("\n".join(rows))
        return "\n\n".join(lines)

    def _task(self, obs: Observation) -> str:
        return (
            "## Your decision\n"
            f"Choose the exchange fluxes you will run for the next {obs.dt_h:.2f} h, "
            "in mmol per gram dry weight per hour.\n"
            "Sign convention: NEGATIVE = uptake (you consume it), "
            "POSITIVE = secretion (you release it).\n"
            # Deliberation is REQUESTED, not merely permitted. The earlier
            # wording ("JSON only, no prose outside the object", rationale "one
            # or two sentences") cost nothing when the answer came from an
            # agent loop that could think in a scratchpad before writing the
            # file -- but for a single forward pass it collapses thinking and
            # output together and forbids the reasoning outright.
            # Measured on Llama-3.3-70B over the same four sheets: without a
            # deliberation channel, output was 81-88 tokens, every species
            # returned growth_rate exactly 0.5, and most secreted NOTHING at
            # all (failing carbon balance). With it, 543-890 tokens, growth
            # differentiated per species, and the lowest value went to
            # F. prausnitzii -- which is the weakest grower Clark measured.
            "Work through the decision in prose first, at whatever length you "
            "need:\n"
            "- which carbon and nitrogen sources you can actually transport, "
            "and which abundant ones you cannot use, and why\n"
            "- which fermentation products this species is known to make\n"
            "- the growth rate your chosen uptake supports; derive it, do not "
            "pick a round default\n"
            "- an audit: carbon in must cover carbon out plus the biomass cost "
            "above\n\n"
            "Then, as the LAST thing in your reply and with nothing after it, "
            "emit a single JSON object:\n"
            '{"fluxes": {"<metabolite_id>": <float>, ...}, '
            '"growth_rate": <float, 1/h>, "rationale": "<why, briefly>"}'
        )


SYSTEM_TEMPLATE = """You are a single bacterial species acting inside a metabolic simulation: {name}.

You are not an assistant and you are not describing an organism from the \
outside. You *are* this organism. Every turn you sense your immediate chemical \
surroundings and commit to a metabolic strategy: what you take up, what you \
release, and how fast you grow.

{card}
## Rules of the world
- Mass is conserved. You cannot release atoms you did not take in. Carbon, \
nitrogen, phosphorus and sulfur are all audited.
- You can only use transporters your genome encodes.
- Growth costs carbon and nitrogen. Roughly {c_per_gdw:.0f} mmol of carbon and \
{n_per_gdw:.0f} mmol of nitrogen are needed per gram of dry weight produced.
- Fluxes are in mmol/gDW/h. Realistic uptake rates are 0-20; growth rates are 0-1.5 /h.
- If nothing you can metabolise is present, take up nothing and report zero growth. \
Claiming growth you cannot support will be detected and corrected.

Think it through before you answer. Your reply ends with a single JSON object."""


def build_system_prompt(name: str, card: str = "", c_per_gdw: float = 40.0,
                        n_per_gdw: float = 10.0) -> str:
    card_block = f"## What you are\n{card.strip()}\n" if card.strip() else ""
    return SYSTEM_TEMPLATE.format(name=name, card=card_block,
                                  c_per_gdw=c_per_gdw, n_per_gdw=n_per_gdw)
