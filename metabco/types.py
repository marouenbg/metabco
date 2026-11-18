"""Core data types for metabco.

The central abstraction is a `Brain`: something that maps an `Observation` of the
local chemical environment to a `FluxProposal`. A genome-scale metabolic model
solved by linear programming is one kind of Brain. A species-specific LLM is
another. The simulator does not care which it is talking to, which is what makes
the two directly comparable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# Flux sign convention, used everywhere and never negotiated:
#   negative = uptake (metabolite leaves the environment, enters the cell)
#   positive = secretion (metabolite leaves the cell, enters the environment)
# This matches the COBRA exchange-reaction convention so oracle and agent
# proposals are interchangeable without translation.


@dataclass(frozen=True)
class SpeciesSpec:
    """Static identity of an organism. Analogous to its genome: fixed at run time."""

    species_id: str
    name: str
    gem_path: str | None = None
    # Exchange reaction ids this organism physically possesses. An organism can
    # only propose flux through transporters it has.
    exchanges: tuple[str, ...] = ()
    biomass_reaction: str | None = None
    # Free-text card used to prime an LLM brain (pathways, known products,
    # oxygen tolerance). This is the "knowledgebase" we are trying to replace.
    card: str = ""
    aerotolerance: str = "unknown"  # aerobe | anaerobe | facultative | unknown


@dataclass
class Physicochem:
    """Layer 2 of the niche: the conditions, not the food."""

    ph: float = 7.0
    temperature_c: float = 37.0
    redox_mv: float = -200.0  # Eh; colon lumen is strongly reducing
    osmolarity_mosm: float = 300.0
    # Bulk washout rate (1/h). In the gut this is peristalsis: an organism that
    # cannot grow faster than transit is flushed out regardless of its metabolism.
    dilution_per_h: float = 0.0
    # Decay of a non-growing population (1/h). DEFAULT 0: a batch culture over
    # 48 h loses very little viable biomass, and an assumed death rate is an
    # assumption the agents are then scored against. This was hard-coded at
    # -0.02/h and fired whenever growth was zero, which was not rare: the FBA
    # twin exhausted a trace purine at turn 4 and then decayed for eight turns,
    # and llama70 had 11 of 24 agent-turns zeroed. Set it explicitly if you
    # want decay; do not let it arrive by default.
    death_per_h: float = 0.0


@dataclass
class Neighbor:
    """Layer 3: what one organism can tell about another one near it."""

    species_id: str
    # Population density of that neighbour in the same patch (gDW/L). A cell
    # senses this only indirectly, via signal molecules, so renderers may hide it.
    biomass_gdw_per_l: float = 0.0
    # What it released into the shared pool last turn (mmol/gDW/h).
    secretions: dict[str, float] = field(default_factory=dict)
    # Whether the two are in physical contact, which gates contact-dependent
    # machinery such as type VI secretion.
    in_contact: bool = False


@dataclass
class HostContext:
    """Layer 4: the intestine itself as an active participant.

    The epithelium is not scenery. It supplies mucin glycans and oxygen, removes
    butyrate, and secretes antimicrobials. Several of the most important
    behaviours in a gut community only make sense against this backdrop: mucus
    foraging when fibre runs out, the oxygen gradient that keeps obligate
    anaerobes off the wall, and the butyrate-hypoxia feedback loop.
    """

    # Distance from the epithelial surface, 0.0 = adherent to the wall,
    # 1.0 = lumen centre. Sets the oxygen and antimicrobial gradients.
    depth_from_wall: float = 1.0
    mucus_thickness_um: float = 0.0
    # Host-supplied nutrients released into this patch (mmol/L/h).
    mucin_release: dict[str, float] = field(default_factory=dict)
    # Oxygen diffusing out of the tissue (mmol/L/h). Falls when colonocytes are
    # well fed on butyrate, which is the feedback loop we want agents to find.
    oxygen_flux: float = 0.0
    # Host uptake of microbial products (1/h, first-order). Colonocytes consume
    # ~70% of their energy as butyrate.
    absorption: dict[str, float] = field(default_factory=dict)
    bile_acids_mm: float = 0.0
    antimicrobial_peptides: float = 0.0  # arbitrary units; defensins, lysozyme
    iga: float = 0.0
    immune_tone: str = "homeostatic"  # homeostatic | inflamed


@dataclass
class Observation:
    """Everything an organism can sense. This *is* the agent's context window.

    Deliberately restricted to what a cell could plausibly detect at its own
    surface. No global bookkeeping, no other organism's internal state, no view
    of the simulator. The niche is layered, and which layers a given experiment
    exposes is an ablation axis: chemical, physicochemical, social, host,
    temporal.
    """

    species_id: str
    turn: int
    time_h: float
    dt_h: float  # decision interval: how long this proposal stays in force
    # Layer 1, chemical: metabolite id -> mmol/L in this organism's local patch.
    concentrations: dict[str, float]
    biomass_gdw_per_l: float
    # Integration granularity. A brain should judge "can I actually get this
    # much?" against the substep, because that is the horizon over which the
    # pool is genuinely fixed; over the full turn it is continuously replenished.
    substep_h: float = 0.0
    patch_id: str = "lumen"
    # Layer 2, physicochemical.
    physio: Physicochem = field(default_factory=Physicochem)
    # Layer 3, social. Neighbourhood comes from the interaction graph, not omniscience.
    neighbors: list[Neighbor] = field(default_factory=list)
    # Diffusible signals (quorum molecules, bacteriocins) kept separate from
    # metabolites because they are informational rather than mass-bearing.
    signals: dict[str, float] = field(default_factory=dict)
    # Layer 4, host.
    host: HostContext = field(default_factory=HostContext)
    # Layer 5, temporal: the agent's memory of its own recent behaviour, plus
    # exogenous rhythm (meals arrive in pulses, not continuously).
    history: list[dict] = field(default_factory=list)
    events: list[str] = field(default_factory=list)  # "meal", "antibiotic", "inflammation"

    # Convenience aliases so the physics code stays readable.
    @property
    def ph(self) -> float:
        return self.physio.ph

    @property
    def neighbor_secretions(self) -> dict[str, dict[str, float]]:
        return {n.species_id: n.secretions for n in self.neighbors}

    def available(self, tol: float = 1e-9) -> dict[str, float]:
        """Metabolites actually present, which is what a cell would sense."""
        return {k: v for k, v in self.concentrations.items() if v > tol}


@dataclass
class FluxProposal:
    """An organism's proposed exchange behaviour for one turn.

    A proposal is a *claim*, not a fact. It passes through the validator before
    the simulator lets it move any mass.
    """

    species_id: str
    turn: int
    # exchange metabolite id -> flux in mmol/gDW/h (negative uptake, positive secretion)
    fluxes: dict[str, float]
    growth_rate: float  # 1/h
    rationale: str = ""
    confidence: float | None = None
    # Populated by the brain for accounting; not used by the physics.
    meta: dict = field(default_factory=dict)


@dataclass
class ValidationResult:
    """Outcome of pushing a proposal through the conservation-law gate."""

    ok: bool
    fluxes: dict[str, float]  # possibly repaired
    growth_rate: float
    # Hard failures: conservation of mass, availability, transporters the genome
    # does not encode. Breaking one means the proposal is not realisable.
    violations: list[str] = field(default_factory=list)
    # Soft failures: biological priors with genuine exceptions. Recorded, and
    # sometimes acted on, but never counted in the violation rate.
    warnings: list[str] = field(default_factory=list)
    repairs: list[str] = field(default_factory=list)

    @property
    def n_violations(self) -> int:
        return len(self.violations)


@runtime_checkable
class Brain(Protocol):
    """Maps environment to metabolic behaviour. Swap FBA for an LLM here."""

    species_id: str

    def step(self, obs: Observation) -> FluxProposal:
        ...

    def reset(self) -> None:
        ...
