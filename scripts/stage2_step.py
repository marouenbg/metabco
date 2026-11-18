"""Drive the community simulation one turn at a time, pausing for agents.

There is no API key in this environment, so `LLMBrain` cannot call a model inside
the simulator loop. This script splits the loop instead: `--render` writes one
sheet per species and exits, agents answer them out of band, `--apply` reads the
answers back, validates them against the conservation gate and advances the
world. The physics, the medium, the timestep and the validator are byte-identical
to the FBA twin in `pilot_gut4.py` — only the brain differs, which is the whole
point of the comparison.

State is checkpointed to JSON between turns so a run can be resumed, inspected or
replayed. Every proposal, every violation and every repair is logged, so a
divergence from the twin can be traced to the turn and the species that caused it.

The prompt carries all five sensed layers under a 2,500-token ceiling. That
ceiling is the experiment's main constraint: an agent that cannot see its
neighbours' secretions cannot cross-feed deliberately, and one that cannot see
the oxygen gradient cannot avoid it.
"""

from __future__ import annotations

import argparse
import json
from functools import lru_cache
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metabco.chem import MetaboliteRegistry, apply_common_synonyms
from metabco.diet import load_diet
from metabco.render import BULK as BULK_EXCESS
from metabco.env import Medium, World
from metabco.render import ContextRenderer, RenderConfig, build_system_prompt
from metabco.species import load_species
from metabco.types import FluxProposal
from metabco.validate import ConservationValidator, ValidatorConfig

ROSTER = {
    # mucosa — wall-associated, tolerating the host oxygen gradient
    "caerofaciens":       ("mucosa", 0.02, "CA", "Collinsella aerofaciens ATCC 25986"),
    "elenta":             ("mucosa", 0.02, "EL", "Eggerthella lenta DSM 2243"),
    "dpiger":             ("mucosa", 0.02, "DP", "Desulfovibrio piger ATCC 29098"),
    # mucus — butyrate producers and the lactate/H2 sinks that feed on them
    "fprausnitzii":       ("mucus",  0.02, "FP", "Faecalibacterium prausnitzii A2-165"),
    "rintestinalis":      ("mucus",  0.02, "RI", "Roseburia intestinalis L1-82"),
    "erectale":           ("mucus",  0.02, "ER", "Eubacterium rectale ATCC 33656"),
    "acaccae":            ("mucus",  0.02, "AC", "Anaerostipes caccae DSM 14662"),
    "ccomes":             ("mucus",  0.02, "CC", "Coprococcus comes ATCC 27758"),
    "bhydrogenotrophica": ("mucus",  0.02, "BH", "Blautia hydrogenotrophica DSM 10507"),
    "dlongicatena":       ("mucus",  0.02, "DL", "Dorea longicatena DSM 13814"),
    "dformicigenerans":   ("mucus",  0.02, "DF", "Dorea formicigenerans ATCC 27755"),
    # lumen — primary polysaccharide degraders
    "btheta":             ("lumen",  0.02, "BT", "Bacteroides thetaiotaomicron VPI-5482"),
    "bfragilis":          ("lumen",  0.02, "BF", "Bacteroides fragilis NCTC 9343"),
    "bovatus":            ("lumen",  0.02, "BO", "Bacteroides ovatus ATCC 8483"),
    "buniformis":         ("lumen",  0.02, "BU", "Bacteroides uniformis ATCC 8492"),
    "bvulgatus":          ("lumen",  0.02, "BV", "Bacteroides vulgatus ATCC 8482"),
    "bcaccae":            ("lumen",  0.02, "BC", "Bacteroides caccae ATCC 43185"),
    "bcellulosilyticus":  ("lumen",  0.02, "BY", "Bacteroides cellulosilyticus DSM 14838"),
    "pjohnsonii":         ("lumen",  0.02, "PJ", "Parabacteroides johnsonii DSM 18315"),
    "pcopri":             ("lumen",  0.02, "PC", "Prevotella copri CB7 DSM 18205"),
    "casparagiforme":     ("lumen",  0.02, "CG", "Clostridium asparagiforme DSM 15981"),
    "badolescentis":      ("lumen",  0.02, "BA", "Bifidobacterium adolescentis ATCC 15703"),
    "blongum":            ("lumen",  0.02, "BL", "Bifidobacterium longum NCC2705"),
    "bpseudocatenulatum": ("lumen",  0.02, "BP", "Bifidobacterium pseudocatenulatum DSM 20438"),
    # Clark's 25th. Absent from every run before 28 Aug because the AGORA2
    # filename could not be guessed -- see LITERAL_CARDS.
    "chiranonis":         ("lumen",  0.02, "CH", "Clostridium hiranonis TO-931 (DSM 13275)"),
    # Clark's 26th, added 12 Sep 2026. AGORA2 files it as Eubacterium biforme,
    # the pre-2013 name -- see metabco/species.py.
    #
    # dm38_mono.py pins the registry to `list(S.ROSTER)`, so this entry widens
    # that union to 26 species. MEASURED, not assumed: the registry is unchanged
    # at 709 metabolites and 1,445 synonym keys (HB's 105 exchanges are all
    # already covered), and an F. prausnitzii monoculture prompt is byte
    # identical before and after -- 4,270 chars, sha aee90c146d6f1cb9. No Phase 1
    # re-run is needed.
    #
    # The COUPLING still stands though: a future roster entry carrying a
    # metabolite no other GEM has would silently change every monoculture
    # prompt. If that matters, pin REG_MEMBERS to an explicit 25-species list
    # instead of list(S.ROSTER).
    "hbiformis":          ("lumen",  0.02, "HB", "Holdemanella biformis DSM 3989"),
}
RUN = Path("runs/stage2/clark24")
DT_H = 1.0
N_SUBSTEPS = 10

# The renderer already emits the answer contract (signed `fluxes`, `growth_rate`,
# `rationale`). This only adds the confidence field and restates the audit, so the
# agent never sees two different sign conventions.
# The nutrient list shows only what is *present*, which is the right contract for
# uptake and the wrong one for secretion: a butyrate producer must be able to name
# butyrate on a turn when there is none in the medium yet. R. intestinalis hit this
# on the first run and had to guess `but`, `co2`, `h2` unprompted. Naming the
# product vocabulary is a formatting aid, not a hint — it says nothing about which
# of these any particular organism can actually make.
HOWTO = """Also include `"confidence": <0.0-1.0>` in the same object.

For anything you take **up**, use the ids exactly as they appear above — the id is
the part in parentheses. For anything you **release**, you may use any standard
metabolite id even if it does not appear above, since a product need not already
be present. The usual ones are: `ac` acetate, `but` butyrate, `ppa` propionate,
`lac__L` and `lac__D` lactate, `for` formate, `succ` succinate, `etoh` ethanol,
`co2`, `h2`, `h2s`, `nh4`. Listing one here says nothing about whether you can
make it; that is your decision.

Mass is audited: you cannot release more carbon than you take in, and growth
consumes carbon and nitrogen on top of that."""


# ---------------------------------------------------------------- world set-up

def history_for(run: Path, sid: str, upto_turn: int, n_turns: int = 4,
                top: int = 8) -> list[dict]:
    """The agent's own last few turns, for the temporal layer.

    Read back from the per-turn validation logs rather than kept in memory, so
    a checkpointed run can be resumed without losing the agent's past. The
    APPLIED fluxes are used, not the claimed ones: what the agent actually did
    to the world is what produced the state it is now looking at, and showing
    the repaired vector lets it see that its proposals get corrected.

    Trimmed to the largest-magnitude fluxes because this block is pure context
    cost -- four turns of twenty fluxes each would be a third of the budget.
    """
    out = []
    for t in range(max(0, upto_turn - n_turns), upto_turn):
        f = run / f"turn_{t:02d}" / "validation.json"
        if not f.exists():
            continue
        for r in json.loads(f.read_text()):
            if r["species_id"] != sid:
                continue
            fl = sorted(r["fluxes_applied"].items(), key=lambda kv: -abs(kv[1]))
            out.append({"turn": t, "growth": r["growth_allowed"],
                        "fluxes": {m: v for m, v in fl[:top] if abs(v) > 0.05}})
    return out


@lru_cache(maxsize=8)
def _registry_cached(key: tuple[str, ...]) -> tuple[MetaboliteRegistry, dict]:
    members = list(key)
    species = {sid: load_species(sid) for sid in members}
    reg = MetaboliteRegistry()
    for sp in species.values():
        for m in sp.gem.registry().mets.values():
            reg.add(m)
    apply_common_synonyms(reg)
    return reg, species


def registry_for(members: list[str]) -> tuple[MetaboliteRegistry, dict]:
    """Chemical dictionary + loaded GEMs for a set of species.

    Cached because callers ask for the same set many times per turn. Nothing
    downstream mutates the registry or the species objects -- world state lives
    entirely in World -- so sharing them across calls is safe.
    """
    return _registry_cached(tuple(members))


def fresh_world(members: list[str]) -> World:
    diet = load_diet("western")
    conc = {m: v * 5 for m, v in diet.bounds.items()}
    conc.update({"strch1": 8.0, "glc__D": 5.0, "nh4": 20.0, "pi": 20.0})
    # The environment report tells the agent "bulk salts, water and phosphate are
    # present in excess". That sentence has to be true or the agent is being
    # misled: sulfate sat at 0.5 mM while every organism was audited for the
    # sulfur in its biomass, so B. theta was charged an s_imbalance for believing
    # the prompt. Bulk minerals are therefore actually supplied in excess. Carbon
    # is deliberately untouched -- scarcity of carbon is the experiment.
    for m in BULK_EXCESS:
        conc[m] = max(conc.get(m, 0.0), 100.0)
    medium = Medium(name="gut-fibre", concentrations=conc,
                    feed={m: v * 2 for m, v in diet.bounds.items()},
                    description="Western gut diet, fibre-supplemented, transit inflow")
    pb: dict[str, dict[str, float]] = {}
    for sid in members:
        patch, x0, _, _ = ROSTER[sid]
        pb.setdefault(patch, {})[sid] = x0
    world = World.gut_column(medium, pb, dilution_per_h=0.15, oxygen_flux=0.02)
    # gut_column seeds only the lumen, which leaves anything living in the mucus
    # staring at an empty compartment on turn 0. Seed the whole column with a
    # falling gradient instead: the patches still differ in every way that
    # matters ecologically — oxygen off the wall, mucin release, butyrate
    # absorption, antimicrobial peptides, pH and washout — without one of them
    # being a vacuum.
    for pid, frac in (("mucus", 0.5), ("mucosa", 0.25)):
        world.patches[pid].concentrations = {
            m: v * frac for m, v in medium.concentrations.items()}
    return world


def save_state(world: World, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "time_h": world.time_h, "turn": world.turn,
        "last_secretions": world.last_secretions,
        "last_growth": world.last_growth,
        "patches": {pid: {"concentrations": p.concentrations,
                          "biomass": p.biomass,
                          "ph": p.physio.ph}
                    for pid, p in world.patches.items()},
    }, indent=1))


def load_state(world: World, path: Path) -> World:
    st = json.loads(path.read_text())
    world.time_h = st["time_h"]
    world.turn = st["turn"]
    world.last_secretions = st.get("last_secretions", {})
    world.last_growth = st.get("last_growth", {})
    for pid, rec in st["patches"].items():
        p = world.patches[pid]
        p.concentrations = {k: float(v) for k, v in rec["concentrations"].items()}
        p.biomass = {k: float(v) for k, v in rec["biomass"].items()}
        p.physio.ph = float(rec["ph"])
    return world


# ---------------------------------------------------------------- the two modes

def render(members: list[str], turn_dir: Path) -> None:
    reg, species = registry_for(members)
    world = load_state(fresh_world(members), RUN / "state.json")
    renderer = ContextRenderer(reg, RenderConfig(max_metabolites=28))
    turn_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for sid in members:
        patch = ROSTER[sid][0]
        if world.patches[patch].biomass.get(sid, 0.0) <= 1e-9:
            continue                      # washed out; nothing left to decide
        obs = world.observe(sid, patch, DT_H, substep_h=DT_H / N_SUBSTEPS)
        sp = species[sid]
        comp = sp.gem.biomass_composition()
        sheet = "\n\n".join([
            build_system_prompt(ROSTER[sid][3], card="",
                                c_per_gdw=comp.get("C", 40.0),
                                n_per_gdw=comp.get("N", 10.0)),
            renderer.render(obs),
            HOWTO,
        ])
        path = turn_dir / f"{sid}.md"
        path.write_text(sheet)
        manifest.append({"species_id": sid, "patch": patch, "path": str(path),
                         "approx_tokens": len(sheet) // 4})
        print(f"  {ROSTER[sid][2]:3s} {sid:15s} ~{len(sheet)//4:5d} tokens -> {path}")
    (turn_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    mx = max((m["approx_tokens"] for m in manifest), default=0)
    print(f"\nturn {world.turn}, t={world.time_h:.1f}h — {len(manifest)} agents to answer, "
          f"largest ~{mx} tokens ({'within' if mx < 2500 else 'OVER'} the 2,500 ceiling)")


def apply(members: list[str], turn_dir: Path) -> None:
    reg, species = registry_for(members)
    world = load_state(fresh_world(members), RUN / "state.json")
    specs = {sid: sp.spec for sid, sp in species.items()}
    comps = {sid: sp.gem.biomass_composition() for sid, sp in species.items()}
    # possession_severity="warn": a missing exchange still blocks the flux, but is
    # charged to the reconstruction rather than to the agent. See validate.py.
    validator = ConservationValidator(
        reg, ValidatorConfig(mode="repair", possession_severity="warn"), specs, comps)

    proposals, log = {}, []
    for sid in members:
        f = turn_dir / f"{sid}.json"
        if not f.exists():
            continue
        d = json.loads(f.read_text())
        fluxes: dict[str, float] = {}
        for met, v in (d.get("fluxes") or {}).items():   # signed, the contract
            m = reg.resolve(met) or met
            fluxes[m] = fluxes.get(m, 0.0) + float(v)
        for met, v in (d.get("uptake") or {}).items():   # tolerated fallback
            m = reg.resolve(met) or met
            fluxes[m] = fluxes.get(m, 0.0) - abs(float(v))
        for met, v in (d.get("secretion") or {}).items():
            m = reg.resolve(met) or met
            fluxes[m] = fluxes.get(m, 0.0) + abs(float(v))
        prop = FluxProposal(species_id=sid, turn=world.turn, fluxes=fluxes,
                            growth_rate=float(d.get("growth_rate") or 0.0),
                            rationale=str(d.get("rationale", ""))[:200],
                            confidence=d.get("confidence"))
        patch = ROSTER[sid][0]
        obs = world.observe(sid, patch, DT_H, substep_h=DT_H / N_SUBSTEPS)
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
        # The gate returns the repaired vector; that, not the claim, is what the
        # physics receives. Keeping both in the log is what lets a divergence be
        # attributed to the agent rather than to the repair.
        # World.apply keys on (patch_id, species_id) — reversing this silently
        # drops every proposal and leaves only washout, which looks exactly like
        # four agents that all failed to grow.
        proposals[(patch, sid)] = FluxProposal(
            species_id=sid, turn=world.turn, fluxes=res.fluxes,
            growth_rate=res.growth_rate, rationale=prop.rationale,
            confidence=prop.confidence)

    world.apply(proposals, DT_H, n_substeps=N_SUBSTEPS)
    save_state(world, RUN / "state.json")
    (turn_dir / "validation.json").write_text(json.dumps(log, indent=1))

    nv = sum(len(r["violations"]) for r in log)
    print(f"turn {world.turn - 1} applied -> t={world.time_h:.1f}h   "
          f"{len(proposals)} proposals, {nv} hard violations, "
          f"{sum(1 for r in log if r['ok'])}/{len(log)} clean")
    for r in log:
        print(f"  {r['species_id']:15s} mu {r['growth_claimed']:.3f} -> "
              f"{r['growth_allowed']:.3f}   ac={r['fluxes_applied'].get('ac', 0):+.2f} "
              f"but={r['fluxes_applied'].get('but', 0):+.2f}   "
              f"{len(r['violations'])} viol, {len(r['repairs'])} repairs")
        for v in r["violations"][:2]:
            print(f"      ! {v[:110]}")
    for pid, p in world.patches.items():
        bm = ", ".join(f"{ROSTER[s][2]}={x:.4f}" for s, x in p.biomass.items() if x > 1e-9)
        if bm:
            print(f"  {pid:8s} {bm}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["init", "render", "apply"])
    ap.add_argument("--members", default=",".join(ROSTER))
    args = ap.parse_args()
    members = [m.strip() for m in args.members.split(",") if m.strip()]

    if args.mode == "init":
        RUN.mkdir(parents=True, exist_ok=True)
        save_state(fresh_world(members), RUN / "state.json")
        print(f"initialised -> {RUN/'state.json'}  ({len(members)} species)")
        return
    turn = json.loads((RUN / "state.json").read_text())["turn"]
    turn_dir = RUN / f"turn_{turn:02d}"
    (render if args.mode == "render" else apply)(members, turn_dir)


if __name__ == "__main__":
    main()
