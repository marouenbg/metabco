"""Conservation-law gate between an agent's proposal and the world's physics.

A language model asked to behave like a bacterium will happily secrete more
carbon than it ate, grow without a nitrogen source, or respire oxygen it does not
have. None of that is a prompt-engineering problem; it is a physics problem, and
physics is not something you ask a model to please respect. So the simulator
audits every proposal against elemental balance, availability and yield limits
before a single molecule moves.

Two things fall out of this for free. Guarantees: no run can silently violate
mass conservation. And a metric: the violation rate is a direct, unfakeable
measure of how well an agent has internalised its own metabolism, available on
every turn without a ground-truth model to compare against.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .chem import BIOMASS_ELEMENTS_MMOL_PER_GDW, MetaboliteRegistry
from .types import FluxProposal, Observation, SpeciesSpec, ValidationResult

# Elements we audit. Carbon is the one that matters most; the others catch
# organisms that claim to build protein out of sugar alone.
AUDITED = ("C", "N", "P", "S")


@dataclass
class ValidatorConfig:
    """Two classes of check, deliberately kept apart.

    Hard checks are conservation laws and physical availability. They cannot be
    wrong, they apply to every organism equally, and breaking one means the
    proposal is not physically realisable. These produce `violations`.

    Soft checks are biological priors: an obligate anaerobe should not respire,
    fluxes should be within the usual range. These are useful and often right,
    but they are curated beliefs, not laws, and they have exceptions --
    Akkermansia is a nanaerobe that genuinely consumes trace oxygen, and AGORA
    polymer exchanges legitimately carry large fluxes. These produce `warnings`.

    Keeping them separate matters because `violation_rate` is the headline
    metric. If a soft prior can inflate it, the number stops meaning "this agent
    broke physics" and starts meaning "this agent disagreed with my priors" --
    and it would then flag the FBA oracle we calibrate against.
    """

    mode: str = "repair"  # repair | reject | log_only
    balance_tol: float = 0.05  # 5% slack absorbs unknown formulas and rounding
    min_formula_coverage: float = 0.8  # below this, balance checks are meaningless
    enforce_possession: bool = True  # can only use transporters the genome encodes
    # Whether a missing transporter is a law or a curation gap. It is a law only
    # if the reconstruction is complete, and these are not: AGORA2 gives all four
    # gut anaerobes in the Stage 2 pilot identical growth at 0 and 20 mM acetate,
    # when F. prausnitzii measurably goes from OD 0.03 alone to 1.53 beside
    # B. thetaiotaomicron. Counting an absent exchange as a hard violation
    # therefore scores the agent against the reconstruction's gaps as if they
    # were physics. Set "warn" to drop the flux from the applied vector -- the
    # physics still refuses to move atoms through a transporter that is not
    # modelled -- while recording it as a warning, so the proposals become a
    # measurement of where the agent asserts biology the model lacks.
    possession_severity: str = "violation"  # violation | warn
    # Whether an absent exchange also STOPS the flux. Separate from severity,
    # which only decides blame. Blocking is right whenever the reconstruction's
    # exchange list is the best evidence available -- but in Clark's DM38 it
    # demonstrably is not: AGORA predicts death for six of eleven species that
    # Clark measured growing there, and eight of the eleven models carry no
    # haemin exchange although haemin is in the medium and these organisms
    # require it. Blocking there would delete correct agent behaviour for the
    # reconstruction's reasons. Set False to let the flux through and record it,
    # turning the gap into a measurement -- how much biology the agent asserts
    # that the model lacks -- instead of a silent handicap. Mass balance is
    # unaffected either way; conservation is physics, annotation is not.
    possession_blocks: bool = True
    # Mass balance audits ATOMS and says nothing about DIRECTION. Secreting
    # glucose passes the carbon audit as long as the carbon came from somewhere
    # -- and Llama-3.1-8B duly made glucose appear in a sealed flask on 23
    # occasions across the monoculture sweep. No heterotrophic gut anaerobe runs
    # gluconeogenesis and exports free hexose; sugars are catabolic inputs, so
    # this is a law and it is always DETECTED.
    #
    # Whether it BLOCKS is a separate question, and the default is no. Three of
    # the four brains tested never attempted it, unguarded -- so the check
    # discriminates, and enforcing it would delete the signal it just produced.
    # Detection is free; enforcement removes a measurement. Same split as
    # possession_severity vs possession_blocks. Turn this on for a deployment
    # where a corrupted world state matters more than a clean evaluation.
    sugar_export_blocks: bool = False
    enforce_availability: bool = True
    # Soft priors below this line.
    max_abs_flux: float = 500.0  # clipped, but recorded as a warning
    max_growth: float = 3.0  # 1/h; ~14 min doubling, faster than any gut anaerobe
    check_aerotolerance: bool = True
    anaerobe_o2_tolerance: float = 1.0  # trace O2 use is normal for nanaerobes


@dataclass
class ValidationStats:
    """Rolling tally, so violation rate is queryable at any point in a run."""

    n_proposals: int = 0
    n_invalid: int = 0
    n_warned: int = 0
    n_repaired: int = 0
    by_code: dict[str, int] = field(default_factory=dict)
    by_warning: dict[str, int] = field(default_factory=dict)

    def record(self, res: ValidationResult) -> None:
        self.n_proposals += 1
        if res.violations:
            self.n_invalid += 1
        if res.warnings:
            self.n_warned += 1
        if res.repairs:
            self.n_repaired += 1
        for v in res.violations:
            code = v.split(":", 1)[0]
            self.by_code[code] = self.by_code.get(code, 0) + 1
        for w in res.warnings:
            code = w.split(":", 1)[0]
            self.by_warning[code] = self.by_warning.get(code, 0) + 1

    @property
    def violation_rate(self) -> float:
        """Fraction of proposals that were not physically realisable."""
        return self.n_invalid / self.n_proposals if self.n_proposals else 0.0

    @property
    def warning_rate(self) -> float:
        """Fraction that were realisable but biologically surprising."""
        return self.n_warned / self.n_proposals if self.n_proposals else 0.0

    def summary(self) -> dict:
        return {
            "n_proposals": self.n_proposals,
            "n_invalid": self.n_invalid,
            "n_repaired": self.n_repaired,
            "violation_rate": round(self.violation_rate, 4),
            "warning_rate": round(self.warning_rate, 4),
            "by_code": dict(sorted(self.by_code.items(), key=lambda kv: -kv[1])),
            "by_warning": dict(sorted(self.by_warning.items(), key=lambda kv: -kv[1])),
        }


class ConservationValidator:
    """Audits and, if asked, repairs a flux proposal.

    Repair is deliberately conservative and ordered: throw away what is not
    real, clip what is not available, then give up growth before giving up
    secretion. Growth is the quantity an over-claiming model inflates most, and
    it is the one the simulator can most safely reduce.
    """

    def __init__(self, registry: MetaboliteRegistry, config: ValidatorConfig | None = None,
                 specs: dict[str, SpeciesSpec] | None = None,
                 biomass_composition: dict[str, dict[str, float]] | None = None):
        self.reg = registry
        self.cfg = config or ValidatorConfig()
        self.specs = specs or {}
        # Per-species elemental cost of biomass, ideally read from that species'
        # own biomass reaction. Falls back to a generic cell composition for
        # organisms that have no model, which is the case we ultimately care about.
        self.biomass_composition = biomass_composition or {}
        self.stats: dict[str, ValidationStats] = {}

    def stats_for(self, species_id: str) -> ValidationStats:
        return self.stats.setdefault(species_id, ValidationStats())

    def _biomass_cost(self, species_id: str, element: str) -> float:
        comp = self.biomass_composition.get(species_id)
        if comp is None:
            return BIOMASS_ELEMENTS_MMOL_PER_GDW.get(element, 0.0)
        return comp.get(element, 0.0)

    # ------------------------------------------------------------------- entry

    def validate(self, prop: FluxProposal, obs: Observation) -> ValidationResult:
        violations: list[str] = []
        warnings: list[str] = []
        repairs: list[str] = []
        spec = self.specs.get(prop.species_id)

        fluxes = self._canonicalise(prop.fluxes, spec, violations, repairs, warnings)
        growth = self._sanitise_growth(prop.growth_rate, violations)
        if self.cfg.enforce_availability:
            fluxes = self._clip_availability(fluxes, obs, violations, repairs)
        if self.cfg.check_aerotolerance and spec is not None:
            self._check_aerotolerance(fluxes, spec, warnings)

        # Order matters here, and getting it wrong hides exactly the failure we
        # most need to catch. Soft clipping used to run first, so a model
        # claiming a growth rate of 400/h had it quietly reduced to the 3/h
        # ceiling and *then* audited -- at which point the carbon budget was
        # comfortably satisfied and the proposal passed clean. The soft ceiling
        # laundered the hard violation. Conservation is now judged on what the
        # agent actually claimed; cosmetic clipping happens afterwards.
        self._credit_free_minerals(fluxes, growth, prop.species_id, obs, spec, repairs)
        fluxes, growth = self._enforce_balance(fluxes, growth, prop.species_id,
                                               violations, repairs)
        fluxes = self._clip_magnitudes(fluxes, warnings, repairs)
        growth = self._soft_clip_growth(growth, warnings, repairs)

        ok = not violations
        if not ok and self.cfg.mode == "reject":
            # A rejected organism does nothing this turn. Honest, and it makes
            # the cost of an invalid proposal visible in the population curve.
            fluxes, growth = {}, 0.0
            repairs.append("reject: proposal discarded")
        elif self.cfg.mode == "log_only":
            fluxes, growth = dict(prop.fluxes), prop.growth_rate
            repairs = ["log_only: no repair applied"]

        res = ValidationResult(ok=ok, fluxes=fluxes, growth_rate=growth,
                               violations=violations, warnings=warnings, repairs=repairs)
        self.stats_for(prop.species_id).record(res)
        return res

    # -------------------------------------------------------------- individual

    def _canonicalise(self, fluxes: dict[str, float], spec: SpeciesSpec | None,
                      violations: list[str], repairs: list[str],
                      warnings: list[str] | None = None) -> dict[str, float]:
        """Resolve names to canonical ids and drop what the organism cannot do."""
        warnings = warnings if warnings is not None else []
        out: dict[str, float] = {}
        for token, raw in fluxes.items():
            try:
                v = float(raw)
            except (TypeError, ValueError):
                violations.append(f"nonnumeric: {token}={raw!r}")
                continue
            if v != v or v in (float("inf"), float("-inf")):
                violations.append(f"nonfinite: {token}")
                continue
            met = self.reg.resolve(token)
            if met is None:
                violations.append(f"unknown_metabolite: {token}")
                continue
            if (self.cfg.enforce_possession and spec is not None and spec.exchanges
                    and met not in spec.exchanges):
                # Either a hallucinated capability or a hole in the
                # reconstruction, and the validator cannot tell which. Severity
                # decides who is blamed; possession_blocks decides whether the
                # atoms move.
                if self.cfg.possession_severity == "warn":
                    warnings.append(f"no_transporter: {met}")
                else:
                    violations.append(f"no_transporter: {met}")
                if self.cfg.possession_blocks:
                    continue
            if v > 0 and self._is_sugar(met):
                # Always recorded -- it is not physically realisable. Blocked
                # only on request: the model is being evaluated on what it
                # proposes, and letting the flux through means it is also scored
                # on the consequences of its own error.
                violations.append(f"sugar_export: {met}")
                if self.cfg.sugar_export_blocks:
                    continue
            if met in out:
                repairs.append(f"merged duplicate {token} -> {met}")
            out[met] = out.get(met, 0.0) + v
        return out

    def _is_sugar(self, met: str) -> bool:
        """Carbohydrate, decided from the formula rather than a curated list.

        A carbohydrate here is CHO-only, at least five carbons, with hydrogen
        about twice oxygen -- glucose C6H12O6, arabinose C5H10O5, maltose
        C12H22O11 (22/11), myo-inositol C6H12O6. Every fermentation product a
        gut anaerobe actually excretes falls outside it on carbon number:
        acetate C2, lactate C3, succinate C4, butyrate C4, ethanol C2, formate
        C1. Deriving it from the formula rather than naming ids means a medium
        we have not built yet is covered without anyone remembering to add to a
        list.
        """
        e = self.reg.elements_of(met)
        if not e:
            return False
        c, h, o = e.get("C", 0.0), e.get("H", 0.0), e.get("O", 0.0)
        if c < 5 or o <= 0:
            return False
        if any(v > 0 for k, v in e.items() if k not in ("C", "H", "O")):
            return False
        return 1.7 <= h / o <= 2.3

    def _sanitise_growth(self, mu: float, violations: list[str]) -> float:
        """Hard checks only: a growth rate must be a real, non-negative number."""
        try:
            mu = float(mu)
        except (TypeError, ValueError):
            violations.append("nonnumeric: growth_rate")
            return 0.0
        if mu != mu or mu in (float("inf"), float("-inf")):
            violations.append("nonfinite: growth_rate")
            return 0.0
        if mu < 0:
            violations.append(f"negative_growth: {mu:.3g}")
            return 0.0
        return mu

    def _soft_clip_growth(self, mu: float, warnings: list[str],
                          repairs: list[str]) -> float:
        """Applied only after conservation has had its say."""
        if mu > self.cfg.max_growth:
            warnings.append(f"growth_too_fast: {mu:.3g}")
            repairs.append(f"clipped growth {mu:.3g} -> {self.cfg.max_growth}")
            return self.cfg.max_growth
        return mu

    def _clip_magnitudes(self, fluxes: dict[str, float], warnings: list[str],
                         repairs: list[str]) -> dict[str, float]:
        out = {}
        for met, v in fluxes.items():
            if abs(v) > self.cfg.max_abs_flux:
                warnings.append(f"flux_too_large: {met}={v:.3g}")
                v = self.cfg.max_abs_flux * (1 if v > 0 else -1)
                repairs.append(f"clipped {met} to {v:.3g}")
            out[met] = v
        return out

    def _clip_availability(self, fluxes: dict[str, float], obs: Observation,
                           violations: list[str], repairs: list[str]) -> dict[str, float]:
        out = dict(fluxes)
        for met, v in fluxes.items():
            if v >= 0:
                continue
            conc = obs.concentrations.get(met, 0.0)
            if conc <= 1e-12:
                violations.append(f"uptake_absent: {met}")
                out[met] = 0.0
                repairs.append(f"zeroed uptake of absent {met}")
                continue
            # Judged over the integration substep, matching the simulator's own
            # rationing horizon. Checking against the whole turn would flag any
            # organism drawing on a small, fast-turnover pool such as dissolved O2.
            horizon = obs.substep_h or obs.dt_h
            if obs.biomass_gdw_per_l > 0 and horizon > 0:
                vmax = conc / (obs.biomass_gdw_per_l * horizon)
                if -v > vmax * (1 + 1e-6):
                    violations.append(f"uptake_exceeds_pool: {met}")
                    out[met] = -vmax
                    repairs.append(f"clipped uptake {met} to pool limit {-vmax:.3g}")
        return out

    def _check_aerotolerance(self, fluxes: dict[str, float], spec: SpeciesSpec,
                             warnings: list[str]) -> None:
        # Trace oxygen consumption by an "obligate" anaerobe is normal, not an
        # error: several gut anaerobes carry oxygen-detoxifying reactions and
        # Akkermansia is a nanaerobe that uses low oxygen outright. Only flag
        # respiration at a rate that would imply real aerobic metabolism.
        o2 = fluxes.get("o2", 0.0)
        if spec.aerotolerance == "anaerobe" and o2 < -self.cfg.anaerobe_o2_tolerance:
            warnings.append(f"anaerobe_respiring_o2: {o2:.3g}")

    # Mineral sources a well-mixed medium supplies in vast excess, and which the
    # environment report explicitly describes as "present in excess". An agent
    # that reads that sentence and does not itemise phosphate uptake is following
    # instructions, so charging it a phosphorus imbalance measures the prompt, not
    # the organism. Carbon is never on this list: carbon is the whole question.
    FREE_SOURCES = {"P": "pi", "S": "so4", "N": "nh4"}

    def _credit_free_minerals(self, fluxes: dict[str, float], growth: float,
                              species_id: str, obs: "Observation",
                              spec: SpeciesSpec | None, repairs: list[str]) -> None:
        """Book the uptake of freely-available minerals the agent left implicit."""
        for element, src in self.FREE_SOURCES.items():
            per_gdw = self._biomass_cost(species_id, element)
            if per_gdw <= 0:
                continue
            _, taken, released = self._element_flow(fluxes, element)
            deficit = released + growth * per_gdw - taken * (1 + self.cfg.balance_tol)
            if deficit <= 1e-9:
                continue
            if spec is not None and spec.exchanges and src not in spec.exchanges:
                continue
            n_per = self.reg.elements_of(src).get(element, 0.0)
            if n_per <= 0:
                continue
            add = deficit / n_per
            # Credit only what this patch can actually supply over this turn.
            # The previous test was a flat 10 mM, which is not a property of any
            # medium -- it is an artifact of the gut column seeding bulk minerals
            # at >=100 mM. Clark's DM38 carries a real, measured 6.61 mM
            # phosphate and ~10.3 mM sulfate, so the flat cut credited sulfur and
            # refused phosphorus, and every agent in the first DM38 turn was
            # zeroed by p_imbalance for failing to book an uptake the renderer
            # had unconditionally told it was free. Comparing the draw against
            # the pool needs no threshold and cannot drift with the medium.
            draw = add * max(obs.biomass_gdw_per_l, 0.0) * max(obs.dt_h, 0.0)
            if obs.concentrations.get(src, 0.0) < draw:
                continue
            fluxes[src] = fluxes.get(src, 0.0) - add
            repairs.append(f"{element}: credited {add:.3g} mmol/gDW/h of {src} "
                           f"(declared present in excess)")

    def _enforce_balance(self, fluxes: dict[str, float], growth: float, species_id: str,
                         violations: list[str], repairs: list[str]
                         ) -> tuple[dict[str, float], float]:
        """The core rule: an organism cannot emit atoms it never took in."""
        for element in AUDITED:
            per_gdw = self._biomass_cost(species_id, element)
            coverage, taken, released = self._element_flow(fluxes, element)
            if coverage < self.cfg.min_formula_coverage:
                continue  # too many unknown formulas for the audit to mean anything
            need_biomass = growth * per_gdw
            demand = released + need_biomass
            budget = taken * (1 + self.cfg.balance_tol)
            if demand <= budget + 1e-9:
                continue

            violations.append(
                f"{element.lower()}_imbalance: in={taken:.3g} out={released:.3g} "
                f"biomass={need_biomass:.3g}"
            )
            if self.cfg.mode != "repair":
                continue

            # Surrender growth first: it is the claim most often inflated, and
            # cutting it never invents new mass.
            if need_biomass > 0:
                affordable_mu = max(0.0, (budget - released)) / max(per_gdw, 1e-9)
                if affordable_mu < growth:
                    repairs.append(
                        f"{element}: growth {growth:.3g} -> {affordable_mu:.3g}")
                    growth = affordable_mu
                    need_biomass = growth * per_gdw
            # Still over budget: scale secretions down proportionally.
            if released + need_biomass > budget + 1e-9 and released > 0:
                scale = max(0.0, (budget - need_biomass) / released)
                repairs.append(f"{element}: secretions scaled by {scale:.3g}")
                for met, v in list(fluxes.items()):
                    if v > 0 and self.reg.elements_of(met).get(element, 0.0) > 0:
                        fluxes[met] = v * scale
        return fluxes, growth

    def _element_flow(self, fluxes: dict[str, float], element: str
                      ) -> tuple[float, float, float]:
        """Return (formula coverage, atoms taken in, atoms released) per gDW/h."""
        n_total = n_known = 0
        taken = released = 0.0
        for met, v in fluxes.items():
            if abs(v) < 1e-12:
                continue
            n_total += 1
            elements = self.reg.elements_of(met)
            if not elements:
                continue
            n_known += 1
            count = elements.get(element, 0.0)
            if count <= 0:
                continue
            if v < 0:
                taken += -v * count
            else:
                released += v * count
        coverage = n_known / n_total if n_total else 1.0
        return coverage, taken, released


def summarise(validator: ConservationValidator) -> dict:
    return {sp: st.summary() for sp, st in validator.stats.items()}
