"""The environment: a chemical world with spatial structure and a host in it.

This module owns the physics. Organisms propose; the environment disposes. Every
molecule that appears in a cell came out of the pool, and every molecule a cell
releases goes back into it. Nothing here trusts an organism's arithmetic.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from .types import FluxProposal, HostContext, Neighbor, Observation, Physicochem

# Metabolites treated as inexhaustible: water, protons, and bulk minerals. Real
# media contain them in vast excess and tracking them adds noise without insight.
UNLIMITED = {"h2o", "h", "pi", "so4", "k", "na1", "cl", "ca2", "mg2", "fe2", "fe3",
             "mn2", "zn2", "cu2", "cobalt2", "mobd", "ni2", "sel", "slnt", "tungs", "cbl1"}


@dataclass
class Medium:
    """A defined growth medium: what is in it and how fast it is replenished."""

    name: str
    concentrations: dict[str, float]  # mmol/L at t=0
    feed: dict[str, float] = field(default_factory=dict)  # mmol/L in inflow
    unlimited: set[str] = field(default_factory=lambda: set(UNLIMITED))
    description: str = ""

    @classmethod
    def load(cls, path: str | Path) -> "Medium":
        d = json.loads(Path(path).read_text())
        return cls(
            name=d["name"],
            concentrations=d["concentrations"],
            feed=d.get("feed", {}),
            unlimited=set(d.get("unlimited", UNLIMITED)),
            description=d.get("description", ""),
        )

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(
                {
                    "name": self.name,
                    "description": self.description,
                    "concentrations": self.concentrations,
                    "feed": self.feed,
                    "unlimited": sorted(self.unlimited),
                },
                indent=1,
            )
        )


@dataclass
class Patch:
    """One well-mixed spatial compartment.

    A gut is not a flask. Splitting the world into patches along the
    lumen-to-epithelium axis is the cheapest way to get the structure that
    actually decides who lives where: the oxygen gradient off the wall, mucin
    available only near it, and the antimicrobial layer on top of it.
    """

    patch_id: str
    concentrations: dict[str, float] = field(default_factory=dict)
    biomass: dict[str, float] = field(default_factory=dict)  # species_id -> gDW/L
    physio: Physicochem = field(default_factory=Physicochem)
    host: HostContext = field(default_factory=HostContext)
    volume_l: float = 1.0
    # Diffusive exchange with neighbouring patches (1/h), by patch id.
    couplings: dict[str, float] = field(default_factory=dict)
    signals: dict[str, float] = field(default_factory=dict)
    # Gas transfer with a reservoir: metabolite -> (rate 1/h, setpoint mmol/L).
    # A shaken flask holds dissolved O2 near saturation; a constant O2 *influx*
    # instead makes oxygen pile up without bound whenever biomass is low.
    gas_exchange: dict[str, tuple[float, float]] = field(default_factory=dict)

    def conc(self, met: str) -> float:
        return self.concentrations.get(met, 0.0)


@dataclass
class World:
    """The full environment: patches, the medium feeding them, and the clock."""

    patches: dict[str, Patch]
    medium: Medium
    time_h: float = 0.0
    turn: int = 0
    # Interaction graph: species_id -> species it can sense. Empty means all
    # co-located organisms sense each other.
    interaction_graph: dict[str, set[str]] = field(default_factory=dict)
    # Log of what each species secreted last turn, for neighbour observation.
    last_secretions: dict[str, dict[str, float]] = field(default_factory=dict)
    _sec_accum: dict[str, dict[str, float]] = field(default_factory=dict)
    last_growth: dict[str, float] = field(default_factory=dict)
    rationing_events: list[dict] = field(default_factory=list)
    ph_dynamic: bool = True

    # ---------------------------------------------------------------- building

    @classmethod
    def single_patch(
        cls,
        medium: Medium,
        species_biomass: dict[str, float],
        dilution_per_h: float = 0.0,
        ph: float = 7.0,
    ) -> "World":
        """A flask. The right control for everything else."""
        patch = Patch(
            patch_id="flask",
            concentrations=dict(medium.concentrations),
            biomass=dict(species_biomass),
            physio=Physicochem(ph=ph, dilution_per_h=dilution_per_h),
        )
        return cls(patches={"flask": patch}, medium=medium)

    @classmethod
    def gut_column(
        cls,
        medium: Medium,
        species_biomass: dict[str, dict[str, float]],
        dilution_per_h: float = 0.25,
        oxygen_flux: float = 0.05,
    ) -> "World":
        """Three patches from epithelium to lumen.

        Oxygen enters at the wall and is consumed on the way out; mucin is only
        released at the wall; antimicrobial peptides are steepest there. This is
        the minimal geometry in which mucosal and luminal niches are different
        places to live.
        """
        layout = [
            ("mucosa", 0.0, 100.0, oxygen_flux, 1.0, 0.02),
            ("mucus", 0.4, 50.0, oxygen_flux * 0.3, 0.3, 0.1),
            ("lumen", 1.0, 0.0, 0.0, 0.0, dilution_per_h),
        ]
        patches: dict[str, Patch] = {}
        for pid, depth, mucus, o2f, amp, dil in layout:
            patches[pid] = Patch(
                patch_id=pid,
                concentrations=dict(medium.concentrations) if pid == "lumen" else {},
                biomass=dict(species_biomass.get(pid, {})),
                physio=Physicochem(ph=6.5 if pid == "lumen" else 7.0, dilution_per_h=dil),
                host=HostContext(
                    depth_from_wall=depth,
                    mucus_thickness_um=mucus,
                    # Mucin O-glycans: the host's own polysaccharide, and the
                    # fallback carbon source when dietary fibre runs out.
                    mucin_release={"acgam": 0.30, "acnam": 0.10, "gal": 0.20, "fuc__L": 0.10}
                    if pid in ("mucosa", "mucus")
                    else {},
                    oxygen_flux=o2f,
                    # Colonocytes burn butyrate; this is what keeps the mucosa hypoxic.
                    absorption={"but": 0.8, "ppa": 0.3, "ac": 0.2} if pid == "mucosa" else {},
                    antimicrobial_peptides=amp,
                ),
            )
        patches["mucosa"].couplings = {"mucus": 0.5}
        patches["mucus"].couplings = {"mucosa": 0.5, "lumen": 0.5}
        patches["lumen"].couplings = {"mucus": 0.5}
        return cls(patches=patches, medium=medium)

    # ------------------------------------------------------------- observation

    def observe(self, species_id: str, patch_id: str, dt_h: float,
                history: list[dict] | None = None,
                events: list[str] | None = None,
                substep_h: float = 0.0) -> Observation:
        patch = self.patches[patch_id]
        visible = self.interaction_graph.get(species_id)
        # Neighbours are drawn from this patch *and* the patches it exchanges
        # with. Restricting to the same patch looks conservative and is in fact
        # wrong for the geometry we use: in a three-layer gut column with one
        # organism per layer, nobody would ever have a neighbour and the entire
        # social layer would be silently dead. Diffusion already couples these
        # compartments chemically, so their occupants are within sensing range;
        # only physical contact stays same-patch.
        nearby: list[tuple[str, float, bool]] = []
        for other, x in patch.biomass.items():
            if other != species_id:
                nearby.append((other, x, True))
        for adj_id in patch.couplings:
            adj = self.patches.get(adj_id)
            if adj is None:
                continue
            for other, x in adj.biomass.items():
                if other != species_id and not any(n[0] == other for n in nearby):
                    nearby.append((other, x, False))

        neighbors = []
        for other, x, same_patch in nearby:
            if visible is not None and other not in visible:
                continue
            if x <= 0:
                continue
            neighbors.append(
                Neighbor(
                    species_id=other,
                    biomass_gdw_per_l=x,
                    secretions=dict(self.last_secretions.get(other, {})),
                    # Contact requires sharing a compartment; density is a crude
                    # but monotone stand-in for spatial packing within it.
                    in_contact=same_patch and x > 0.05,
                )
            )
        conc = {m: v for m, v in patch.concentrations.items() if v > 1e-12}
        for m in self.medium.unlimited:
            conc.setdefault(m, 1000.0)
        return Observation(
            species_id=species_id,
            turn=self.turn,
            time_h=self.time_h,
            dt_h=dt_h,
            substep_h=substep_h or dt_h,
            concentrations=conc,
            biomass_gdw_per_l=patch.biomass.get(species_id, 0.0),
            patch_id=patch_id,
            physio=patch.physio,
            neighbors=neighbors,
            signals=dict(patch.signals),
            host=patch.host,
            history=list(history or []),
            events=list(events or []),
        )

    # ---------------------------------------------------------------- dynamics

    def apply(self, proposals: dict[tuple[str, str], FluxProposal], dt_h: float,
              n_substeps: int = 10) -> dict:
        """Advance the world by dt_h given per-(patch, species) flux proposals.

        The decision interval and the integration step are deliberately
        different things. An organism commits to a flux distribution once per
        turn -- for an LLM agent that is one expensive inference call -- but the
        resulting differential equations are integrated finely, holding those
        fluxes constant. Without this, any metabolite whose standing pool is
        small relative to its turnover gets spuriously rationed: dissolved
        oxygen sits at ~0.21 mM and turns over in seconds, so a 15-minute
        Euler step strangles an aerobe that is in fact perfectly well aerated.

        Returns a report of what the physics had to override, which is a
        first-class result: an agent whose proposals are routinely rationed is an
        agent that does not understand its environment.
        """
        report = {"rationed": [], "starved": [], "extinct": []}
        n_sub = max(1, n_substeps)
        sub = dt_h / n_sub
        ration_worst: dict[str, dict] = {}
        starve_worst: dict[str, dict] = {}
        # What each species released, summed over substeps. Previously
        # last_secretions was overwritten every substep and only the last
        # survived -- and each substep's value is scaled by that substep's
        # rationing, so once a substrate depletes mid-turn the final substep is
        # rationed toward zero and the neighbour block reported ~0 for a species
        # that had in fact secreted heavily. The turn mean is what the
        # neighbours actually experienced.
        self._sec_accum = {}

        for _ in range(max(1, n_substeps)):
            for patch_id, patch in self.patches.items():
                here = {sp: p for (pid, sp), p in proposals.items() if pid == patch_id}
                if here:
                    self._apply_patch(patch, here, sub, ration_worst, starve_worst)
                self._apply_gas(patch, sub)
                self._apply_host(patch, sub)
                self._apply_dilution(patch, sub)
            self._apply_diffusion(sub)

        # Report the tightest constraint seen anywhere in the step, not one row
        # per substep, so the trace stays readable.
        report["rationed"] = sorted(ration_worst.values(), key=lambda r: r["factor"])
        report["starved"] = sorted(starve_worst.values(), key=lambda r: r["scale"])
        self.last_secretions = {sp: {m: v / n_sub for m, v in d.items() if v / n_sub > 1e-9}
                                for sp, d in self._sec_accum.items()}

        if self.ph_dynamic:
            self._update_ph()

        for patch in self.patches.values():
            for sp, x in list(patch.biomass.items()):
                if 0 < x < 1e-9:
                    patch.biomass[sp] = 0.0
                    report["extinct"].append(f"{sp}@{patch.patch_id}")

        self.time_h += dt_h
        self.turn += 1
        return report

    def _apply_patch(self, patch: Patch, props: dict[str, FluxProposal],
                     dt_h: float, ration_worst: dict, starve_worst: dict) -> None:
        # Step 1: how much of each metabolite is demanded, in mmol/L over dt.
        demand: dict[str, float] = {}
        for sp, prop in props.items():
            x = patch.biomass.get(sp, 0.0)
            if x <= 0:
                continue
            for met, v in prop.fluxes.items():
                if v < 0 and met not in self.medium.unlimited:
                    demand[met] = demand.get(met, 0.0) + (-v) * x * dt_h

        # Step 2: ration oversubscribed metabolites. Two organisms cannot both
        # eat the last glucose molecule; the simulator, not the agents, decides.
        ration: dict[str, float] = {}
        for met, want in demand.items():
            have = patch.conc(met)
            if want > have + 1e-12:
                ration[met] = max(0.0, have / want) if want > 0 else 0.0
                key = f"{patch.patch_id}/{met}"
                prev = ration_worst.get(key)
                if prev is None or ration[met] < prev["factor"]:
                    ration_worst[key] = {
                        "patch": patch.patch_id, "met": met, "want": round(want, 6),
                        "have": round(have, 6), "factor": round(ration[met], 4),
                        "turn": self.turn,
                    }

        # Step 3: each organism is throttled by its scarcest input. Growth is
        # scaled with it, standing in for the LP re-solve under tighter bounds.
        for sp, prop in props.items():
            x = patch.biomass.get(sp, 0.0)
            if x <= 0:
                continue
            scale = 1.0
            for met, v in prop.fluxes.items():
                if v < 0 and met in ration:
                    scale = min(scale, ration[met])
            if scale < 1.0:
                key = f"{patch.patch_id}/{sp}"
                prev = starve_worst.get(key)
                if prev is None or scale < prev["scale"]:
                    starve_worst[key] = {"species": sp, "patch": patch.patch_id,
                                         "scale": round(scale, 4), "turn": self.turn}

            secreted: dict[str, float] = {}
            for met, v in prop.fluxes.items():
                if met in self.medium.unlimited:
                    continue
                dc = v * scale * x * dt_h
                patch.concentrations[met] = max(0.0, patch.conc(met) + dc)
                if v > 0:
                    secreted[met] = v * scale
            acc = self._sec_accum.setdefault(sp, {})
            for met, v in secreted.items():
                acc[met] = acc.get(met, 0.0) + v

            mu = max(0.0, prop.growth_rate) * scale
            # A non-growing population decays only if the caller asks for it.
            # This used to be a hard-coded -0.02/h, which meant every turn an
            # agent failed to grow was also a turn it lost 8% of its biomass --
            # an assumption baked into the physics and then scored against.
            if mu <= 0:
                mu = -patch.physio.death_per_h
            patch.biomass[sp] = x * math.exp(mu * dt_h)
            self.last_growth[sp] = mu

    def _apply_gas(self, patch: Patch, dt_h: float) -> None:
        for met, (k, setpoint) in patch.gas_exchange.items():
            c = patch.conc(met)
            patch.concentrations[met] = setpoint + (c - setpoint) * math.exp(-k * dt_h)

    def _apply_host(self, patch: Patch, dt_h: float) -> None:
        h = patch.host
        for met, rate in h.mucin_release.items():
            patch.concentrations[met] = patch.conc(met) + rate * dt_h
        if h.oxygen_flux > 0:
            patch.concentrations["o2"] = patch.conc("o2") + h.oxygen_flux * dt_h
        for met, k in h.absorption.items():
            patch.concentrations[met] = patch.conc(met) * math.exp(-k * dt_h)

    def _apply_dilution(self, patch: Patch, dt_h: float) -> None:
        d = patch.physio.dilution_per_h
        if d <= 0:
            return
        f = math.exp(-d * dt_h)
        for met in set(patch.concentrations) | set(self.medium.feed):
            if met in self.medium.unlimited:
                continue
            feed = self.medium.feed.get(met, 0.0)
            patch.concentrations[met] = feed + (patch.conc(met) - feed) * f
        for sp in patch.biomass:
            patch.biomass[sp] *= f

    def _apply_diffusion(self, dt_h: float) -> None:
        if len(self.patches) < 2:
            return
        deltas: dict[str, dict[str, float]] = {p: {} for p in self.patches}
        for pid, patch in self.patches.items():
            for other, k in patch.couplings.items():
                if other not in self.patches:
                    continue
                op = self.patches[other]
                mets = set(patch.concentrations) | set(op.concentrations)
                for met in mets:
                    if met in self.medium.unlimited:
                        continue
                    flow = k * (patch.conc(met) - op.conc(met)) * dt_h * 0.5
                    deltas[pid][met] = deltas[pid].get(met, 0.0) - flow
                    deltas[other][met] = deltas[other].get(met, 0.0) + flow
        for pid, d in deltas.items():
            for met, dv in d.items():
                self.patches[pid].concentrations[met] = max(
                    0.0, self.patches[pid].conc(met) + dv
                )

    def _update_ph(self) -> None:
        """Acidification by fermentation acids.

        Crude but directionally right, and it matters: SCFA accumulation drops
        luminal pH, which in turn suppresses acid-sensitive taxa. Without this
        the environment cannot push back on its inhabitants.
        """
        for patch in self.patches.values():
            acids = sum(patch.conc(m) for m in ("ac", "ppa", "but", "lac__L", "lac__D", "for"))
            patch.physio.ph = max(4.5, min(7.5, 7.0 - 0.012 * acids))

    # ------------------------------------------------------------------ readout

    def snapshot(self) -> dict:
        return {
            "turn": self.turn,
            "time_h": round(self.time_h, 4),
            "patches": {
                pid: {
                    "biomass": {k: round(v, 8) for k, v in p.biomass.items()},
                    "conc": {k: round(v, 6) for k, v in p.concentrations.items() if v > 1e-9},
                    "ph": round(p.physio.ph, 3),
                }
                for pid, p in self.patches.items()
            },
        }

    def total_biomass(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in self.patches.values():
            for sp, x in p.biomass.items():
                out[sp] = out.get(sp, 0.0) + x * p.volume_l
        return out
