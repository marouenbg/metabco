"""The FBA oracle: ground truth, and the thing we are trying to replace.

Wraps a genome-scale model so that it satisfies the same `Brain` interface an
LLM agent does. Given an environment it returns exchange fluxes and a growth
rate, computed by linear programming over the stoichiometric matrix. Inside the
simulator it is interchangeable with an LLM, which is the whole point: the only
thing that changes between a control run and a treatment run is what computed
the fluxes.
"""

from __future__ import annotations

import logging
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import cobra
from cobra.flux_analysis import pfba

from .chem import (
    BIOMASS_ELEMENTS_MMOL_PER_GDW,
    Metabolite,
    MetaboliteRegistry,
    apply_common_synonyms,
    parse_formula,
)
from .types import FluxProposal, Observation, SpeciesSpec

log = logging.getLogger(__name__)
logging.getLogger("cobra").setLevel(logging.ERROR)
# An infeasible LP is a normal, expected outcome here: it is what "this organism
# cannot survive on what is left in the medium" looks like. We handle it by
# returning zero growth, so cobra's per-solve warning is pure noise at 40 turns
# times N species.
warnings.filterwarnings("ignore", message="Solver status is", category=UserWarning)

# Exchange ids are spelled differently by every reconstruction pipeline:
# BiGG and CarveMe write EX_glc__D_e, AGORA/AGORA2 write EX_glc_D(e), older
# COBRA exports use EX_glc__D[e]. Missing one of these does not raise an error,
# it silently produces metabolite ids that never match anything, so every
# cross-feeding link quietly disappears.
EX_RE = re.compile(r"^EX_(.+?)(_e0?|_e|\[e\]|\(e\)|_LPAREN_e_RPAREN_)$")


# AGORA writes stereochemistry with one underscore (glc_D, lac_L, ala_L) where
# BiGG uses two (glc__D). Without normalising, 'glucose' resolves in a BiGG model
# and fails in an AGORA one, so the same agent silently loses its carbon source
# depending on which reconstruction it was handed.
_STEREO_RE = re.compile(r"(?<![_])_([DLRS])$")


def normalize_met_id(met_id: str) -> str:
    """Map an AGORA-style metabolite id onto the BiGG namespace."""
    return _STEREO_RE.sub(r"__\1", met_id)


def ex_to_met(ex_id: str) -> str:
    """'EX_glc__D_e' -> 'glc__D'. Handles BiGG, AGORA and CarveMe spellings."""
    m = EX_RE.match(ex_id)
    base = m.group(1) if m else (ex_id[3:] if ex_id.startswith("EX_") else ex_id)
    return normalize_met_id(base)


@dataclass
class Kinetics:
    """Uptake kinetics converting a concentration into an FBA bound.

    Flux balance analysis has no notion of concentration; something has to make
    the bridge. Michaelis-Menten with a shared default is the standard choice in
    dynamic FBA and is deliberately simple, because per-transporter constants are
    not known for most of these organisms.

    `max_carbon_uptake` is the constraint that per-metabolite kinetics alone
    cannot express, and leaving it out is not a small approximation. A rich gut
    medium offers ~170 transportable compounds; if each may be taken up at
    10 mmol/gDW/h independently, an organism can draw nearly 4000 mmol of carbon
    per gram per hour and flux balance analysis will happily report a growth rate
    of 8/h -- a five-minute doubling, faster than any bacterium that has ever
    lived. Cells are bounded by membrane area and proteome allocation, not by
    each transporter separately. The default, 60 mmol C/gDW/h, is roughly what
    E. coli consumes at its maximum aerobic growth rate.
    """

    vmax: float = 10.0  # mmol/gDW/h
    km: float = 0.01  # mmol/L
    overrides: dict[str, tuple[float, float]] = field(default_factory=dict)
    max_carbon_uptake: float | None = 60.0  # mmol C/gDW/h; None disables

    def bound(self, met: str, conc: float) -> float:
        vmax, km = self.overrides.get(met, (self.vmax, self.km))
        if conc <= 0:
            return 0.0
        return vmax * conc / (km + conc)


class GEM:
    """A loaded genome-scale model plus the metadata metabco needs around it."""

    def __init__(self, path: str | Path, species_id: str, name: str | None = None):
        self.path = str(path)
        self.species_id = species_id
        self.model = _read_model(path)
        self.name = name or self.model.name or self.model.id or species_id
        self.biomass_reaction = _find_biomass(self.model)
        self.exchanges = {ex_to_met(r.id): r.id for r in self.model.exchanges}
        self._default_bounds = {r.id: r.bounds for r in self.model.exchanges}

    # ------------------------------------------------------------------ basics

    def registry(self) -> MetaboliteRegistry:
        reg = MetaboliteRegistry()
        for met_id, ex_id in self.exchanges.items():
            rxn = self.model.reactions.get_by_id(ex_id)
            mets = list(rxn.metabolites)
            if not mets:
                continue
            m = mets[0]
            reg.add(
                Metabolite(
                    met_id=met_id,
                    name=(m.name or met_id).replace(" [Extracellular]", "").strip(),
                    formula=m.formula,
                    charge=int(m.charge) if m.charge is not None else None,
                )
            )
        apply_common_synonyms(reg)
        return reg

    def biomass_composition(self) -> dict[str, float]:
        """Elements consumed per gram of dry weight, read off the biomass reaction.

        Hardcoding a canonical cell composition looks reasonable and is wrong in
        practice: reduced models such as e_coli_core use a stripped biomass
        reaction that needs no sulfur at all, so a fixed constant accuses the
        very oracle we are calibrating against of violating conservation. Reading
        the stoichiometry means the audit is always in the model's own units.
        """
        if not self.biomass_reaction:
            return dict(BIOMASS_ELEMENTS_MMOL_PER_GDW)
        rxn = self.model.reactions.get_by_id(self.biomass_reaction)
        net: dict[str, float] = {}
        for met, coeff in rxn.metabolites.items():
            for element, n in parse_formula(met.formula).items():
                # Negative coefficient = consumed, so a positive net requirement.
                net[element] = net.get(element, 0.0) - coeff * n
        return {e: v for e, v in net.items() if v > 1e-6}

    def reset_bounds(self) -> None:
        for rid, b in self._default_bounds.items():
            self.model.reactions.get_by_id(rid).bounds = b

    def close_uptake(self) -> None:
        """Shut every inbound exchange. The starting point for a defined medium."""
        for r in self.model.exchanges:
            r.lower_bound = 0.0

    def set_medium(self, uptake_bounds: dict[str, float]) -> None:
        """uptake_bounds maps metabolite id -> max uptake rate (positive number)."""
        self.close_uptake()
        for met, v in uptake_bounds.items():
            ex = self.exchanges.get(met)
            if ex is not None:
                self.model.reactions.get_by_id(ex).lower_bound = -abs(v)

    def add_carbon_cap(self, limit: float, registry: MetaboliteRegistry | None = None
                       ) -> None:
        """Cap total carbon drawn in across all exchanges simultaneously.

        Must be called inside a `with model:` block so the constraint is removed
        on exit. Uses each exchange's reverse variable, which carries the uptake
        magnitude under COBRA's sign convention.
        """
        reg = registry or self.registry()
        coeffs = {}
        for met, ex_id in self.exchanges.items():
            n_c = reg.carbon_of(met)
            if n_c <= 0:
                continue
            rxn = self.model.reactions.get_by_id(ex_id)
            coeffs[rxn.reverse_variable] = n_c
        if not coeffs:
            return
        cons = self.model.problem.Constraint(0, lb=0, ub=limit, name="carbon_uptake_cap")
        self.model.add_cons_vars([cons])
        self.model.solver.update()
        cons.set_linear_coefficients(coeffs)

    def growth(self) -> float:
        v = self.model.slim_optimize()
        return 0.0 if v is None or v != v else float(v)

    def solve(self, parsimonious: bool = True) -> tuple[float, dict[str, float]]:
        """Return (growth rate, exchange fluxes keyed by metabolite id)."""
        try:
            if parsimonious:
                sol = pfba(self.model)
            else:
                sol = self.model.optimize()
            if sol.status != "optimal":
                return 0.0, {}
        except Exception:
            try:
                sol = self.model.optimize()
                if sol.status != "optimal":
                    return 0.0, {}
            except Exception:
                return 0.0, {}
        mu = float(sol.fluxes.get(self.biomass_reaction, 0.0)) if self.biomass_reaction else 0.0
        fluxes = {}
        for met, ex in self.exchanges.items():
            v = float(sol.fluxes.get(ex, 0.0))
            if abs(v) > 1e-9:
                fluxes[met] = v
        return mu, fluxes

    def __repr__(self) -> str:
        return (f"<GEM {self.species_id} rxns={len(self.model.reactions)} "
                f"mets={len(self.model.metabolites)} ex={len(self.exchanges)}>")


class FBABrain:
    """Ground-truth Brain. Solves an LP where an LLM would reason.

    Determinism note: pFBA is used so repeated solves on identical input give
    identical fluxes, which matters when this is the reference every agent is
    scored against.
    """

    kind = "fba"

    def __init__(self, gem: GEM, kinetics: Kinetics | None = None,
                 spec: SpeciesSpec | None = None, parsimonious: bool = True,
                 max_dvdt: float | None = None, dvdt_relax_conc: float = 1e-3):
        """`max_dvdt` turns this from naive sequential FBA into Mahadevan's SOA.

        Mahadevan, Edwards & Doyle (2002), Biophys J 83:1331-1340, defined dFBA
        and showed that reproducing diauxie needs more than re-optimising each
        interval: enzyme levels cannot change instantaneously, so they bound the
        RATE OF CHANGE of each flux, |v_t - v_{t-1}| <= max_dvdt * dt. That bound
        is what produces the lag, and it is present in both their formulations --
        imposed at every instant in DOA, relaxed near-zero concentrations in SOA.

        Without it (max_dvdt=None) the brain is memoryless: it re-optimises from
        scratch every turn, consumes every available substrate at once, and shows
        no lag. That is the naive dFBA their paper exists to improve on, and it is
        the wrong thing to hold up as "what a solver can do".

        `dvdt_relax_conc` is the SOA relaxation: a substrate whose concentration
        has fallen below this is exempt, because a flux forced to decline slowly
        cannot follow a pool that has already emptied, and holding the constraint
        there makes the LP infeasible rather than slow.
        """
        self.gem = gem
        self.species_id = gem.species_id
        self.kinetics = kinetics or Kinetics()
        self.spec = spec
        self.parsimonious = parsimonious
        self.max_dvdt = max_dvdt
        self.dvdt_relax_conc = dvdt_relax_conc
        self.n_calls = 0
        self.n_dvdt_relaxed = 0
        self._reg = gem.registry()
        self._prev: dict[str, float] = {}      # last turn's exchange fluxes

    def reset(self) -> None:
        self.gem.reset_bounds()
        self.n_calls = 0
        self._prev = {}

    def _apply_dvdt(self, obs: Observation) -> None:
        """Tighten each exchange bound to within max_dvdt*dt of last turn's flux.

        Applied to EXCHANGES only, which is where the substrate switch happens and
        where the enzyme-synthesis argument bites. Relaxed for any metabolite whose
        pool has essentially emptied (see dvdt_relax_conc).
        """
        dt = obs.dt_h or 1.0
        delta = self.max_dvdt * dt
        for met, prev in self._prev.items():
            rid = self.gem.exchanges.get(met) if hasattr(self.gem.exchanges, "get") else None
            if rid is None:
                continue
            if obs.concentrations.get(met, 0.0) <= self.dvdt_relax_conc:
                continue                      # pool gone; the flux must be free to drop
            try:
                rxn = self.gem.model.reactions.get_by_id(rid)
            except KeyError:
                continue
            lo = max(rxn.lower_bound, prev - delta)
            hi = min(rxn.upper_bound, prev + delta)
            if lo <= hi:
                rxn.lower_bound, rxn.upper_bound = lo, hi

    def bounds_for(self, obs: Observation) -> dict[str, float]:
        """Translate what is in the environment into uptake capacity."""
        out: dict[str, float] = {}
        for met, conc in obs.concentrations.items():
            if met not in self.gem.exchanges:
                continue
            v = self.kinetics.bound(met, conc)
            if v <= 0:
                continue
            # An organism cannot consume more than exists. Judged over the
            # integration substep, not the whole decision interval: over a turn
            # the pool is refilled by gas transfer, host secretion and diffusion,
            # and capping against the standing pool would starve an aerobe that
            # is in fact fully aerated.
            horizon = obs.substep_h or obs.dt_h
            if obs.biomass_gdw_per_l > 0 and horizon > 0:
                v = min(v, conc / (obs.biomass_gdw_per_l * horizon))
            out[met] = v
        return out

    def step(self, obs: Observation) -> FluxProposal:
        self.n_calls += 1
        with self.gem.model:  # restores bounds and constraints on exit
            self.gem.set_medium(self.bounds_for(obs))
            if self.kinetics.max_carbon_uptake:
                self.gem.add_carbon_cap(self.kinetics.max_carbon_uptake, self._reg)
            if self.max_dvdt is not None and self._prev:
                self._apply_dvdt(obs)
                try:
                    mu, fluxes = self.gem.solve(self.parsimonious)
                except Exception:
                    # An infeasible LP RAISES here rather than returning zeros,
                    # so checking the return value alone missed it entirely and
                    # the exception escaped step(), leaving the agent with no
                    # proposal for the rest of the run.
                    mu, fluxes = 0.0, {}
                # NO re-solve here. A failed solve under the rate bound is the
                # LAG: the cell uses neither substrate this interval, `fluxes` is
                # empty, `_prev` becomes {} below, and the constraint is skipped
                # next turn so it re-plans freely and resumes at the lower rate.
                # Relaxing and re-solving immediately -- which is what Mahadevan
                # does within a timestep -- removes the do-nothing interval and
                # with it the plateau. Measured both ways.
            else:
                mu, fluxes = self.gem.solve(self.parsimonious)
        # Stored UNCONDITIONALLY, including after a failed solve -- and that is
        # what produces the lag. When the rate bound makes the LP infeasible at
        # substrate exhaustion, `fluxes` is empty, so `_prev` becomes {} and the
        # constraint is skipped next turn (`if self._prev`), letting the cell
        # re-plan freely. The result is exactly one interval in which it uses
        # neither substrate, then resumes at the new, lower rate.
        #
        # Guarding this to keep only successful solves -- which looks like the
        # obviously correct thing -- deadlocks the culture: the stale `_prev`
        # keeps pinning fluxes and it never restarts. Tried, measured, reverted.
        if self.max_dvdt is not None:
            self._prev = dict(fluxes)
        return FluxProposal(
            species_id=self.species_id,
            turn=obs.turn,
            fluxes=fluxes,
            growth_rate=mu,
            rationale="FBA / pFBA optimum",
            confidence=1.0,
            meta={"brain": "fba", "n_exchange": len(fluxes)},
        )


def _read_model(path: str | Path) -> cobra.Model:
    p = str(path)
    if p.endswith((".json", ".json.gz")):
        return cobra.io.load_json_model(p)
    if p.endswith((".mat",)):
        return cobra.io.load_matlab_model(p)
    return cobra.io.read_sbml_model(p)


BIOMASS_HINTS = ("biomass", "growth", "bio1", "core_biomass")


def _find_biomass(model: cobra.Model) -> str | None:
    """Locate the biomass reaction, which models name inconsistently."""
    try:
        expr = model.objective.expression
        for r in model.reactions:
            if r.id in str(expr) and r.objective_coefficient != 0:
                return r.id
    except Exception:
        pass
    for r in model.reactions:
        if any(h in r.id.lower() for h in BIOMASS_HINTS):
            return r.id
    for r in model.reactions:
        if any(h in (r.name or "").lower() for h in BIOMASS_HINTS):
            return r.id
    return None
