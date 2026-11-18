"""Joint community flux balance analysis: two organisms sharing one medium.

The benchmark's original cross-feeding family asks a *capability* question --
could this organism consume acetate if acetate were freely supplied -- answered by
flux variability analysis on a single model. That turns out to be a different
question from the ecological one, and for the butyrate producers the two disagree
in sign: at its own optimum Roseburia intestinalis *secretes* acetate, while its
maximum possible acetate uptake is 7.1 mmol/gDW/h.

This module answers the other one. Both organisms are placed in a single model
sharing an extracellular pool, and the community is solved once. Cross-feeding is
then read off the solution directly: organism A secretes metabolite m into the
pool and organism B draws it out, in the same steady state. Nothing is supplied
that a member did not release.

Coexistence is imposed the way SteadyCom does it, by requiring every member to
grow at the same specific rate. Without that constraint, maximising summed
biomass hands the entire medium to whichever organism has the better yield and
the other one is assigned zero -- an optimum that is mathematically correct and
ecologically meaningless.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import cobra

from .chem import MetaboliteRegistry
from .oracle import GEM

log = logging.getLogger(__name__)

POOL_SUFFIX = "_pool"


@dataclass
class CommunitySolution:
    growth_rate: float  # shared specific growth rate (1/h)
    biomass: dict[str, float]  # species -> biomass flux
    # species -> {metabolite: flux}, negative uptake / positive secretion,
    # in each organism's own units of mmol/gDW/h.
    exchanges: dict[str, dict[str, float]] = field(default_factory=dict)
    # What the community as a whole drew from or released to the medium.
    medium_exchange: dict[str, float] = field(default_factory=dict)
    status: str = "optimal"

    def cross_feeds(self, threshold: float = 0.1) -> list[tuple[str, str, str, float]]:
        """(donor, recipient, metabolite, amount) actually traded in this solution.

        The amount reported is the smaller of what the donor released and what
        the recipient took, which is the part of the flux that genuinely changed
        hands rather than passing through the medium.
        """
        out = []
        for donor, dflux in self.exchanges.items():
            for met, v in dflux.items():
                if v <= threshold:
                    continue
                for recipient, rflux in self.exchanges.items():
                    if recipient == donor:
                        continue
                    taken = -rflux.get(met, 0.0)
                    if taken > threshold:
                        out.append((donor, recipient, met, round(min(v, taken), 4)))
        return sorted(out, key=lambda t: -t[3])


class Community:
    """Two or more GEMs sharing one extracellular compartment."""

    def __init__(self, gems: list[GEM], registry: MetaboliteRegistry,
                 abundances: dict[str, float] | None = None):
        self.gems = gems
        self.reg = registry
        n = len(gems)
        self.abundances = abundances or {g.species_id: 1.0 / n for g in gems}
        self.biomass_ids: dict[str, str] = {}
        self.exchange_ids: dict[str, dict[str, str]] = {}
        self.pool_exchange: dict[str, str] = {}
        self.model = self._build()

    # ------------------------------------------------------------------ build

    def _build(self) -> cobra.Model:
        com = cobra.Model("community")
        pool_mets: dict[str, cobra.Metabolite] = {}

        for gem in self.gems:
            sid = gem.species_id
            sub = gem.model.copy()
            # Namespace everything so two organisms can hold the same metabolite
            # id without silently sharing an internal pool.
            for met in sub.metabolites:
                met.id = f"{sid}__{met.id}"
            for rxn in sub.reactions:
                rxn.id = f"{sid}__{rxn.id}"
            sub.repair()

            # Every flux in the joint model is expressed per gram of *total*
            # community biomass, which is what makes the shared pool balance
            # without a separate abundance term on each transfer. An organism
            # present at fraction f therefore carries at most f times its own
            # per-gram capacity, so its bounds scale with abundance.
            f = self.abundances[sid]
            for rxn in sub.reactions:
                lb, ub = rxn.bounds
                rxn.bounds = (lb * f, ub * f)

            self.biomass_ids[sid] = f"{sid}__{gem.biomass_reaction}"
            self.exchange_ids[sid] = {}
            com.add_reactions(sub.reactions)

            for met_id, ex_id in gem.exchanges.items():
                local_ex = com.reactions.get_by_id(f"{sid}__{ex_id}")
                if met_id not in pool_mets:
                    pool_mets[met_id] = cobra.Metabolite(
                        f"{met_id}{POOL_SUFFIX}", name=met_id, compartment="pool")
                pool = pool_mets[met_id]
                # The organism's boundary reaction becomes a transporter between
                # its own extracellular space and the shared pool. Uptake by the
                # organism (negative flux) now draws the metabolite out of the
                # pool rather than conjuring it from nowhere.
                local_ex.add_metabolites({pool: 1.0})
                self.exchange_ids[sid][met_id] = local_ex.id

        for met_id, pool in pool_mets.items():
            ex = cobra.Reaction(f"EX_{met_id}{POOL_SUFFIX}")
            ex.lower_bound, ex.upper_bound = 0.0, 1000.0
            com.add_reactions([ex])
            ex.add_metabolites({pool: -1.0})
            self.pool_exchange[met_id] = ex.id

        self._impose_equal_growth(com)
        com.objective = {com.reactions.get_by_id(r): 1.0
                         for r in self.biomass_ids.values()}
        return com

    def _impose_equal_growth(self, com: cobra.Model) -> None:
        """Every member grows at the same specific rate (SteadyCom's condition).

        biomass_i / abundance_i is the specific growth rate of member i; setting
        those equal across members is what makes the solution a coexisting steady
        state rather than a takeover by the most efficient organism.
        """
        ids = list(self.biomass_ids)
        for a, b in zip(ids, ids[1:]):
            ra = com.reactions.get_by_id(self.biomass_ids[a])
            rb = com.reactions.get_by_id(self.biomass_ids[b])
            cons = com.problem.Constraint(0, lb=0, ub=0, name=f"coexist_{a}_{b}")
            com.add_cons_vars([cons])
            com.solver.update()
            cons.set_linear_coefficients({
                ra.forward_variable: 1.0 / self.abundances[a],
                ra.reverse_variable: -1.0 / self.abundances[a],
                rb.forward_variable: -1.0 / self.abundances[b],
                rb.reverse_variable: 1.0 / self.abundances[b],
            })

    # ------------------------------------------------------------------ solve

    def set_medium(self, uptake_bounds: dict[str, float]) -> None:
        for met, ex_id in self.pool_exchange.items():
            self.model.reactions.get_by_id(ex_id).lower_bound = 0.0
        for met, v in uptake_bounds.items():
            ex_id = self.pool_exchange.get(met)
            if ex_id:
                self.model.reactions.get_by_id(ex_id).lower_bound = -abs(v)

    def add_carbon_caps(self, limit: float = 60.0) -> None:
        """Per-organism uptake ceiling, matching the single-organism simulator.

        Applied per member rather than to the community as a whole: the limit is
        membrane and proteome capacity per gram of cell, which does not become
        more generous because a second organism is present.
        """
        for gem in self.gems:
            sid = gem.species_id
            coeffs = {}
            for met, ex_id in self.exchange_ids[sid].items():
                n_c = self.reg.carbon_of(met)
                if n_c <= 0:
                    continue
                rxn = self.model.reactions.get_by_id(ex_id)
                coeffs[rxn.reverse_variable] = n_c
            if not coeffs:
                continue
            # Scaled by abundance for the same reason the bounds are: the limit
            # is capacity per gram of *that organism*, and the solver is working
            # in per-gram-of-community units. Omitting the factor lets each
            # member draw 1/f times its real capacity -- with two members at
            # equal abundance, exactly double.
            cons = self.model.problem.Constraint(
                0, lb=0, ub=limit * self.abundances[sid], name=f"carbon_cap_{sid}")
            self.model.add_cons_vars([cons])
            self.model.solver.update()
            cons.set_linear_coefficients(coeffs)

    def solve(self, medium: dict[str, float], carbon_cap: float | None = 60.0
              ) -> CommunitySolution:
        with self.model:
            self.set_medium(medium)
            if carbon_cap:
                self.add_carbon_caps(carbon_cap)
            sol = self.model.optimize()
            if sol.status != "optimal":
                return CommunitySolution(0.0, {}, status=sol.status)

            biomass = {sid: float(sol.fluxes.get(rid, 0.0))
                       for sid, rid in self.biomass_ids.items()}
            mu = max((b / self.abundances[s] for s, b in biomass.items()), default=0.0)
            exchanges: dict[str, dict[str, float]] = {}
            for sid, mets in self.exchange_ids.items():
                # Reported per gram of that organism, so the numbers are directly
                # comparable with a single-organism solve.
                scale = self.abundances[sid] or 1.0
                exchanges[sid] = {
                    met: round(float(sol.fluxes.get(ex, 0.0)) / scale, 5)
                    for met, ex in mets.items()
                    if abs(float(sol.fluxes.get(ex, 0.0))) > 1e-9
                }
            medium_ex = {met: round(float(sol.fluxes.get(ex, 0.0)), 5)
                         for met, ex in self.pool_exchange.items()
                         if abs(float(sol.fluxes.get(ex, 0.0))) > 1e-9}
        return CommunitySolution(growth_rate=round(mu, 6), biomass=biomass,
                                 exchanges=exchanges, medium_exchange=medium_ex)


def pair_community(a: GEM, b: GEM, registry: MetaboliteRegistry) -> Community:
    return Community([a, b], registry, {a.species_id: 0.5, b.species_id: 0.5})
