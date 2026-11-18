"""Turn runner for a Clark community in DM38 batch — the arm with real ground truth.

Everything else in Stage 2 is scored against an FBA twin, which answers "does the
agent agree with the solver" but never "is either of them right". This runner
exists to close that gap: DM38 is the literal medium Clark's numbers came out of,
batch is the literal regime, and 48 h is the literal endpoint, so the measured
butyrate, acetate, lactate, succinate and per-species abundances are directly
comparable to what comes out of here.

Deliberately NOT the gut column. There are no patches, no mucus, no oxygen
gradient, no host, no washout — Clark ran 1 mL in a deep-well plate. Those layers
are the interesting part of the thesis and they are all switched off here, on
purpose: this arm asks whether the chemistry is right, and the column arm asks
whether context-as-environment buys anything. Conflating them would let a win on
one masquerade as a win on the other.

Three modelling choices worth stating, all grounded in the recipe or the methods:

  pH is pinned      DM38 carries 71.7 mM MOPS. That is a strong buffer, so the
                    dynamic-pH machinery is switched off rather than left to
                    invent an acidification the buffer would have absorbed.
  batch is closed   dilution_per_h = 0 and no feed. Substrate depletes and is
                    never replenished, which is the strategic fact of a batch
                    culture and is stated to the agent explicitly, because the
                    renderer only volunteers a transit rate when one exists.
  H2 in the gas     Clark's chamber is 2.5% H2 / 15% CO2 / balance N2. The
                    dissolved H2 that implies (~0.02 mM) is seeded, because
                    B. hydrogenotrophica and D. piger both live on it and
                    starting them at zero would be a medium-design artifact.

Inoculum is Clark's: OD600 0.0066 total, split evenly. The OD->gDW conversion is
an assumption (0.33 gDW/L per OD600) and is the one number here not taken from a
measurement, so total biomass is not scored against Clark's OD -- metabolites and
relative abundance are.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metabco.chem import MetaboliteRegistry, apply_common_synonyms
from metabco.env import Medium, World
from metabco.render import ContextRenderer, RenderConfig, build_system_prompt
from metabco.species import load_species
from metabco.types import FluxProposal
from metabco.validate import ConservationValidator, ValidatorConfig

import scripts.stage2_step as S
from scripts.stage2_step import HOWTO, history_for

DM38 = Path("data/media/dm38.json")
RUN = Path("runs/dm38")
MEMORY = True          # temporal layer on/off; --memory sets it
# Which species define the METABOLITE REGISTRY, as opposed to who is in the
# tube. Normally the same thing. It stops being the same thing for a
# monoculture: registry_for() unions the members' GEM registries, so with one
# species present the registry does not contain h2, h2s, lac__L or citr__L, and
# the validator rejects an agent's lactate as an unknown metabolite and DROPS
# the flux. Lactate is one of the four numbers Clark measured. A chemical
# dictionary must not depend on who is in the flask, so the mono driver sets
# this to the full 24-species union. Possession (which exchanges a species
# actually carries) is a separate argument to the validator and stays per-species.
REG_MEMBERS: list[str] | None = None
DT_H = 4.0                 # 12 turns x 4 h = Clark's 48 h endpoint
N_TURNS = 12
N_SUBSTEPS = 10
OD_START = 0.0066          # Clark, methods
GDW_PER_OD = 0.33          # assumption, stated; see module docstring
H2_DISSOLVED = 0.02        # mM, from 2.5% H2 in the anaerobic chamber

# LEVEL 3: the last two reconstructions cannot grow on DM38 even with the
# annotation and biomass repairs. scripts/minimal_supplement.py solves for the
# smallest set of exchanges that would have to be opened; these are the answers.
# They are GENUINE metabolic gaps, not bookkeeping -- E. lenta cannot assemble
# three separate precursors, and B. adolescentis cannot elongate a fatty acid to
# C18:0.
#
# This is the weakest arm in the project and it must be labelled wherever it is
# used: a supplemented flask is NOT Clark's medium, so EL and BA stop being
# scoreable against his measurement. Concentrations are deliberately low -- these
# have to act as required nutrients, not as extra carbon that would shift the
# fermentation products we score.
SUPPLEMENTS = {
    "elenta": {"arab__D": 0.5, "cgly": 0.5, "cit": 0.5},
    "badolescentis": {"ocdca": 0.5},
}
SUPPLEMENT = False         # off unless asked; set by --supplement or level 3

CODE2SID = {v[2]: sid for sid, v in S.ROSTER.items()}
NAME = {sid: v[3] for sid, v in S.ROSTER.items()}

BATCH_NOTE = """## The vessel
This is a **sealed batch culture**: 1 mL, anaerobic, 37 C, buffered at pH 7.
Nothing flows in and nothing flows out. Whatever is listed above is the entire
supply for the whole experiment — it is not replenished, and anything you release
stays in the vessel and accumulates. The culture runs for 48 h in total."""


def community_from_codes(codes: str) -> list[str]:
    out = []
    for c in codes.replace(",", "-").split("-"):
        c = c.strip().upper()
        if not c:
            continue
        if c not in CODE2SID:
            raise SystemExit(f"Clark code {c!r} is not in our 24-species roster")
        out.append(CODE2SID[c])
    return out


def dm38_medium() -> Medium:
    d = json.loads(DM38.read_text())
    conc = dict(d["concentrations_mM"])
    conc["h2"] = conc.get("h2", 0.0) + H2_DISSOLVED
    return Medium(name="DM38", concentrations=conc, feed={},
                  description=f"Clark 2021 defined medium, {d['n_components']} "
                              f"components ({d['doi']}), batch")


def fresh_world(members: list[str]) -> World:
    x0 = OD_START * GDW_PER_OD / max(len(members), 1)
    med = dm38_medium()
    if SUPPLEMENT:
        for sid in members:
            for m, c in SUPPLEMENTS.get(sid, {}).items():
                med.concentrations[m] = med.concentrations.get(m, 0.0) + c
    world = World.single_patch(med, {s: x0 for s in members},
                               dilution_per_h=0.0, ph=7.0)
    world.ph_dynamic = False           # 71.7 mM MOPS; see docstring
    return world


def render(members: list[str], turn_dir: Path) -> None:
    reg, species = S.registry_for(REG_MEMBERS or members)
    world = S.load_state(fresh_world(members), RUN / "state.json")
    # host=False is not cosmetic. RenderConfig's host layer always emits a
    # "location: in the lumen, away from the wall" line, which is simply false in
    # a deep-well plate -- there is no wall and no lumen. Of the five sensed
    # layers, this is the one the flask genuinely does not have, so it is
    # switched off rather than left to describe an anatomy that is not there.
    # detection_limit is a SENSING model: below this, nothing is perceived.
    # That is the right model for a secreted product diffusing into a pool, and
    # the wrong one for a defined medium, whose recipe is simply known. At the
    # 1e-4 default, DM38's cobalamin (1e-6), THF (3e-6) and nucleobases (6e-5)
    # were invisible, and B. theta duly asserted "the absence of cobalamin in
    # this medium" and set its succinate/propionate split on that. Dropped here
    # only; the gut column keeps the sensing model.
    renderer = ContextRenderer(reg, RenderConfig(max_metabolites=28, host=False,
                                                 detection_limit=1e-7))
    turn_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for sid in members:
        if world.patches["flask"].biomass.get(sid, 0.0) <= 1e-12:
            continue
        # The temporal layer. Every runner in this project used to call
        # observe() without history=, so the block rendered empty and every
        # agent was stateless -- see docs/HARNESS_NOTES.md.
        hist = history_for(RUN, sid, world.turn) if MEMORY else []
        obs = world.observe(sid, "flask", DT_H, substep_h=DT_H / N_SUBSTEPS,
                            history=hist)
        comp = species[sid].gem.biomass_composition()
        sheet = "\n\n".join([
            build_system_prompt(NAME[sid], card="",
                                c_per_gdw=comp.get("C", 40.0),
                                n_per_gdw=comp.get("N", 10.0)),
            BATCH_NOTE,
            renderer.render(obs),
            HOWTO,
        ])
        path = turn_dir / f"{sid}.md"
        path.write_text(sheet)
        manifest.append({"species_id": sid, "patch": "flask", "path": str(path),
                         "approx_tokens": len(sheet) // 4})
    (turn_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    mx = max((m["approx_tokens"] for m in manifest), default=0)
    print(f"turn {world.turn}, t={world.time_h:.1f}h — {len(manifest)} agents, "
          f"largest ~{mx} tokens ({'within' if mx < 2500 else 'OVER'} the 2,500 ceiling)")


def apply(members: list[str], turn_dir: Path) -> None:
    reg, species = S.registry_for(REG_MEMBERS or members)
    world = S.load_state(fresh_world(members), RUN / "state.json")
    specs = {sid: species[sid].spec for sid in members}
    comps = {sid: species[sid].gem.biomass_composition() for sid in members}
    # possession_blocks=False: see ValidatorConfig. AGORA's exchange lists are
    # measurably wrong in this medium, so an absent transporter is recorded
    # against the reconstruction but does not delete the agent's flux. Mass
    # balance is untouched and still binds.
    validator = ConservationValidator(
        reg, ValidatorConfig(mode="repair", possession_severity="warn",
                             possession_blocks=False), specs, comps)

    proposals, log = {}, []
    for sid in members:
        f = turn_dir / f"{sid}.json"
        if not f.exists():
            # A prompt was put and no usable answer came back -- the model
            # looped, or emitted nothing parseable. That is a failure of the
            # method, not a decision by the organism, so it is recorded as such
            # and the turn proceeds with this species doing nothing: no uptake,
            # no secretion, no growth, biomass decays. Silently skipping the
            # whole turn instead would stall the run and hide the failure.
            if (turn_dir / f"{sid}.md").exists():
                log.append({"species_id": sid, "turn": world.turn,
                            "growth_claimed": 0.0, "growth_allowed": 0.0,
                            "ok": False, "no_answer": True,
                            "violations": ["no_answer"], "warnings": [],
                            "repairs": [], "rationale": "", "confidence": None,
                            "fluxes_claimed": {}, "fluxes_applied": {}})
            continue
        d = json.loads(f.read_text())
        # A malformed flux value costs its own entry, never the run. The SFT arm
        # emitted "secrete_20.59" -- its trained band prefix glued to a number --
        # and the bare float() raised, killing a 25-species sweep at turn 4 of 12.
        # Any arm can emit a bad value; dropping one entry and recording it keeps
        # the failure visible to the validator instead of fatal to the harness.
        fluxes: dict[str, float] = {}
        dropped: list[str] = []
        for met, v in (d.get("fluxes") or {}).items():
            m = reg.resolve(met) or met
            try:
                x = float(v)
            except (TypeError, ValueError):
                dropped.append(f"{met}={v!r}")
                continue
            if x != x or x in (float("inf"), float("-inf")):
                dropped.append(f"{met}={v!r}")      # NaN/inf balance to nothing
                continue
            fluxes[m] = fluxes.get(m, 0.0) + x
        try:
            mu = float(d.get("growth_rate") or 0.0)
        except (TypeError, ValueError):
            dropped.append(f"growth_rate={d.get('growth_rate')!r}")
            mu = 0.0
        if dropped:
            print(f"    {sid} turn {world.turn}: dropped "
                  f"{len(dropped)} unparseable flux value(s): "
                  f"{', '.join(dropped[:4])}", flush=True)
        prop = FluxProposal(species_id=sid, turn=world.turn, fluxes=fluxes,
                            growth_rate=mu,
                            rationale=str(d.get("rationale", ""))[:200],
                            confidence=d.get("confidence"),
                            meta={"dropped_flux_values": dropped} if dropped else {})
        obs = world.observe(sid, "flask", DT_H, substep_h=DT_H / N_SUBSTEPS)
        res = validator.validate(prop, obs)
        log.append({"species_id": sid, "turn": world.turn,
                    "growth_claimed": round(prop.growth_rate, 4),
                    "growth_allowed": round(res.growth_rate, 4),
                    "ok": bool(res.ok),
                    "violations": list(res.violations), "warnings": list(res.warnings),
                    "repairs": list(res.repairs),
                    "rationale": prop.rationale, "confidence": prop.confidence,
                    "fluxes_claimed": {k: round(v, 4) for k, v in fluxes.items()},
                    "fluxes_applied": {k: round(v, 4) for k, v in res.fluxes.items()}})
        proposals[("flask", sid)] = FluxProposal(
            species_id=sid, turn=world.turn, fluxes=res.fluxes,
            growth_rate=res.growth_rate, rationale=prop.rationale,
            confidence=prop.confidence)

    world.apply(proposals, DT_H, n_substeps=N_SUBSTEPS)
    S.save_state(world, RUN / "state.json")
    (turn_dir / "validation.json").write_text(json.dumps(log, indent=1))

    p = world.patches["flask"]
    nv = sum(len(r["violations"]) for r in log)
    print(f"turn {world.turn - 1} applied -> t={world.time_h:.1f}h   "
          f"{len(proposals)} proposals, {nv} hard violations, "
          f"{sum(1 for r in log if r['ok'])}/{len(log)} clean")
    watch = ("but", "ac", "lac__L", "succ", "glc__D", "arab__L", "arg__L")
    print("  pools: " + "  ".join(f"{m}={p.conc(m):.2f}" for m in watch))
    print("  biomass: " + ", ".join(
        f"{S.ROSTER[s][2]}={x:.5f}" for s, x in sorted(p.biomass.items()) if x > 1e-12))


def main() -> None:
    global RUN, MEMORY
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["init", "render", "apply"])
    ap.add_argument("--community", default="BV-BO-BL-CA-EL-FP-AC-BH-ER-RI-CC",
                    help="Clark treatment code, e.g. BV-BO-BL-CA-EL-FP-AC-BH-ER-RI-CC")
    ap.add_argument("--run", default=str(RUN))
    ap.add_argument("--supplement", action="store_true",
                    help="add the minimal per-species supplement (EL, BA only)")
    ap.add_argument("--memory", choices=["on", "off"], default="on",
                    help="feed the temporal layer (the agent's own last 4 turns)")
    args = ap.parse_args()
    RUN = Path(args.run)
    globals()["MEMORY"] = args.memory == "on"
    globals()["SUPPLEMENT"] = bool(getattr(args, "supplement", False))
    members = community_from_codes(args.community)

    if args.mode == "init":
        RUN.mkdir(parents=True, exist_ok=True)
        S.save_state(fresh_world(members), RUN / "state.json")
        (RUN / "community.json").write_text(json.dumps(
            {"treatment": args.community, "members": members,
             "codes": [S.ROSTER[s][2] for s in members],
             "dt_h": DT_H, "n_turns": N_TURNS, "medium": "DM38",
             "od_start": OD_START, "gdw_per_od": GDW_PER_OD}, indent=1))
        w = fresh_world(members)
        print(f"initialised {args.community} -> {RUN}")
        print(f"  {len(members)} species at {OD_START * GDW_PER_OD / len(members):.6f} gDW/L each")
        print(f"  {DT_H:.0f} h x {N_TURNS} turns = {DT_H * N_TURNS:.0f} h")
        return

    turn = json.loads((RUN / "state.json").read_text())["turn"]
    turn_dir = RUN / f"turn_{turn:02d}"
    (render if args.mode == "render" else apply)(members, turn_dir)


if __name__ == "__main__":
    main()
