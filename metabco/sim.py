"""The simulator: turn scheduler over a graph of agents in a shared world.

This is dynamic FBA with the linear program pulled out and replaced by a plug.
Put an `FBABrain` in the socket and you get textbook dFBA. Put an LLM in the same
socket and every other line of physics is unchanged, so any difference in outcome
is attributable to the brain and nothing else.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .env import World
from .trace import Tracer
from .types import Brain, FluxProposal, Observation, SpeciesSpec
from .validate import ConservationValidator, ValidatorConfig


@dataclass
class SimConfig:
    n_turns: int = 48
    dt_h: float = 0.25  # decision interval: one brain call per agent per turn
    n_substeps: int = 10  # ODE substeps per decision, fluxes held constant
    history_len: int = 4  # turns of self-memory an agent carries in context
    stop_on_extinction: bool = False
    schedule: str = "simultaneous"  # simultaneous | round_robin
    seed: int = 0


@dataclass
class Placement:
    """Which organism lives in which patch, and which brain drives it."""

    species_id: str
    patch_id: str
    brain: Brain


@dataclass
class SimResult:
    run_id: str
    trace_dir: str
    snapshots: list[dict] = field(default_factory=list)
    final_biomass: dict[str, float] = field(default_factory=dict)
    validation: dict = field(default_factory=dict)
    wall_s: float = 0.0
    n_turns: int = 0

    # Series must be dense. A snapshot omits metabolites that have hit zero, so
    # appending only what is present yields ragged, time-misaligned arrays in
    # which a metabolite that ran out looks like one that stayed at its last
    # value forever. Every comparison downstream depends on getting this right.

    def biomass_series(self) -> dict[str, list[float]]:
        species = {sp for s in self.snapshots for p in s["patches"].values()
                   for sp in p["biomass"]}
        out: dict[str, list[float]] = {sp: [] for sp in species}
        for snap in self.snapshots:
            totals: dict[str, float] = {}
            for p in snap["patches"].values():
                for sp, x in p["biomass"].items():
                    totals[sp] = totals.get(sp, 0.0) + x
            for sp in species:
                out[sp].append(totals.get(sp, 0.0))
        return out

    def metabolite_series(self, patch: str | None = None) -> dict[str, list[float]]:
        mets = {m for s in self.snapshots
                for pid, p in s["patches"].items()
                if not patch or pid == patch
                for m in p["conc"]}
        out: dict[str, list[float]] = {m: [] for m in mets}
        for snap in self.snapshots:
            pool: dict[str, float] = {}
            for pid, p in snap["patches"].items():
                if patch and pid != patch:
                    continue
                for met, c in p["conc"].items():
                    pool[met] = pool.get(met, 0.0) + c
            for met in mets:
                out[met].append(pool.get(met, 0.0))
        return out

    def times(self) -> list[float]:
        return [s["time_h"] for s in self.snapshots]


class Simulator:
    """Runs a community. Owns the clock, the trace and the validator."""

    def __init__(self, world: World, placements: list[Placement],
                 validator: ConservationValidator, config: SimConfig | None = None,
                 specs: dict[str, SpeciesSpec] | None = None,
                 runs_dir: str = "runs", echo: bool = False):
        self.world = world
        self.placements = placements
        self.validator = validator
        self.cfg = config or SimConfig()
        self.specs = specs or {}
        self.runs_dir = runs_dir
        self.echo = echo
        self.history: dict[str, list[dict]] = {p.species_id: [] for p in placements}
        self.flux_full: dict[str, list[dict]] = {p.species_id: [] for p in placements}

    def run(self, run_id: str | None = None, events: dict[int, list[str]] | None = None
            ) -> SimResult:
        cfg_dict = {
            "sim": vars(self.cfg),
            "medium": self.world.medium.name,
            "patches": list(self.world.patches),
            "agents": [
                {"species": p.species_id, "patch": p.patch_id,
                 "brain": getattr(p.brain, "kind", type(p.brain).__name__),
                 "model": getattr(p.brain, "model_name", None)}
                for p in self.placements
            ],
            "validator": vars(self.validator.cfg),
        }
        tracer = Tracer(self.runs_dir, run_id=run_id, config=cfg_dict, echo=self.echo)
        t0 = time.time()
        snapshots = [self.world.snapshot()]
        tracer.emit("state", **snapshots[0])
        tracer.emit("run_start", n_agents=len(self.placements))

        for turn in range(self.cfg.n_turns):
            turn_events = (events or {}).get(turn, [])
            if turn_events:
                tracer.emit("perturbation", turn=turn, events=turn_events)
            tracer.emit("turn_start", turn=turn, time_h=round(self.world.time_h, 4))

            observations = self._observe(turn_events)
            proposals = self._propose(observations, tracer)
            validated = self._validate(proposals, observations, tracer)
            report = self.world.apply(validated, self.cfg.dt_h, self.cfg.n_substeps)

            if report["rationed"]:
                tracer.emit("rationing", turn=turn, n=len(report["rationed"]),
                            detail=report["rationed"][:8])
                tracer.count("rationing_events", len(report["rationed"]))
            if report["extinct"]:
                tracer.emit("extinction", turn=turn, who=report["extinct"])

            snap = self.world.snapshot()
            snapshots.append(snap)
            tracer.emit("state", **snap)
            self._remember(observations, validated)

            if self.cfg.stop_on_extinction and not any(
                x > 1e-9 for x in self.world.total_biomass().values()
            ):
                tracer.emit("halt", reason="total extinction", turn=turn)
                break

        result = SimResult(
            run_id=tracer.run_id,
            trace_dir=str(tracer.dir),
            snapshots=snapshots,
            final_biomass=self.world.total_biomass(),
            validation={sp: st.summary() for sp, st in self.validator.stats.items()},
            wall_s=round(time.time() - t0, 3),
            n_turns=len(snapshots) - 1,
        )
        tracer.close(result={
            "final_biomass": result.final_biomass,
            "validation": result.validation,
            "n_turns": result.n_turns,
            "wall_s": result.wall_s,
        })
        return result

    # ----------------------------------------------------------------- phases

    def _observe(self, turn_events: list[str]) -> dict[tuple[str, str], Observation]:
        obs = {}
        for p in self.placements:
            obs[(p.patch_id, p.species_id)] = self.world.observe(
                p.species_id, p.patch_id, self.cfg.dt_h,
                history=self.history[p.species_id][-self.cfg.history_len:],
                events=turn_events,
                substep_h=self.cfg.dt_h / max(1, self.cfg.n_substeps),
            )
        return obs

    def _propose(self, observations: dict[tuple[str, str], Observation],
                 tracer: Tracer) -> dict[tuple[str, str], FluxProposal]:
        props = {}
        for p in self.placements:
            key = (p.patch_id, p.species_id)
            o = observations[key]
            if o.biomass_gdw_per_l <= 0:
                continue
            with tracer.timed("brain_call", species=p.species_id, turn=o.turn):
                try:
                    prop = p.brain.step(o)
                except Exception as exc:  # a dead brain must not kill the run
                    tracer.emit("brain_error", species=p.species_id, turn=o.turn,
                                error=f"{type(exc).__name__}: {exc}")
                    tracer.count("brain_errors")
                    prop = FluxProposal(species_id=p.species_id, turn=o.turn,
                                        fluxes={}, growth_rate=0.0,
                                        rationale=f"error: {exc}")
            props[key] = prop
            tracer.emit("proposal", species=p.species_id, turn=o.turn,
                        growth=round(prop.growth_rate, 6),
                        fluxes={k: round(v, 4) for k, v in prop.fluxes.items()},
                        rationale=prop.rationale[:600], confidence=prop.confidence,
                        meta=prop.meta)
            if p.patch_id == p.patch_id and getattr(p.brain, "kind", "") == "llm":
                usage = prop.meta.get("usage") or {}
                if usage:
                    tracer.emit("llm_call", species=p.species_id, turn=o.turn, **usage)
                    for k, v in usage.items():
                        if isinstance(v, (int, float)):
                            tracer.count(f"usage.{k}", v)
        return props

    def _validate(self, proposals: dict[tuple[str, str], FluxProposal],
                  observations: dict[tuple[str, str], Observation],
                  tracer: Tracer) -> dict[tuple[str, str], FluxProposal]:
        out = {}
        for key, prop in proposals.items():
            res = self.validator.validate(prop, observations[key])
            tracer.emit("validation", species=prop.species_id, turn=prop.turn,
                        ok=res.ok, violations=res.violations, warnings=res.warnings,
                        repairs=res.repairs)
            if not res.ok:
                tracer.count("violations", len(res.violations))
            out[key] = FluxProposal(
                species_id=prop.species_id, turn=prop.turn, fluxes=res.fluxes,
                growth_rate=res.growth_rate, rationale=prop.rationale,
                confidence=prop.confidence, meta={**prop.meta, "validated": res.ok},
            )
        return out

    def _remember(self, observations: dict[tuple[str, str], Observation],
                  validated: dict[tuple[str, str], FluxProposal]) -> None:
        """Give each agent a compressed memory of what it just did.

        Kept small on purpose: an agent that can re-read its entire past is not
        modelling a cell, and context length is the budget we care about most.
        """
        for key, prop in validated.items():
            obs = observations[key]
            top = sorted(prop.fluxes.items(), key=lambda kv: -abs(kv[1]))[:6]
            self.history[prop.species_id].append({
                "turn": prop.turn,
                "growth": round(prop.growth_rate, 4),
                "biomass": round(obs.biomass_gdw_per_l, 6),
                "fluxes": {k: round(v, 3) for k, v in top},
            })
            # The FULL applied vector, kept separately and never shown to an
            # agent -- `history` above stays the compressed six-flux memory.
            # Needed because the top-6 cut is not neutral for cross-feeding
            # analysis: bulk and waste (co2, h2o, h2, na1) crowd out the
            # currencies, and A. caccae's lactate UPTAKE vanished from its
            # record entirely. Deriving a cross-feeding network from the
            # truncated vector reported 1 edge where the real number is higher.
            self.flux_full[prop.species_id].append({
                "turn": prop.turn,
                "fluxes": {k: round(v, 4) for k, v in prop.fluxes.items()
                           if abs(v) > 1e-9},
            })


def make_validator(registry, specs: dict[str, SpeciesSpec], mode: str = "repair",
                   biomass_composition: dict[str, dict[str, float]] | None = None,
                   **cfg_kwargs) -> ConservationValidator:
    return ConservationValidator(registry, ValidatorConfig(mode=mode, **cfg_kwargs),
                                 specs, biomass_composition)
