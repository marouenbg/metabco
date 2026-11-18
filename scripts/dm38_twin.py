"""FBA twin for a Clark community in DM38 batch — and the feasibility check.

Run this before spending any agent-turns on a new community. It answers the
question that decides whether the arm is worth running at all: can these
reconstructions grow in this medium? DM38 is a real defined medium with no
starch, no mucin and no complex polysaccharide -- carbon is glucose, arabinose,
maltose, lactate and free amino acids. Several of our organisms were chosen for a
fibre-rich gut column and may simply have nothing to eat here, and that is much
cheaper to discover with a solver than with twenty-four LLM calls.

Shares fresh_world with dm38_step, so the twin and the agents see the identical
flask: same medium, same inoculum, same timestep, same conservation gate, same
absence of washout. Only the brain differs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metabco import FBABrain, Kinetics, Placement, SimConfig, Simulator
from metabco.validate import ConservationValidator, ValidatorConfig

import scripts.dm38_step as D
import scripts.stage2_step as S

WATCH = ["but", "ac", "lac__L", "lac__D", "succ", "for", "etoh", "h2", "co2",
         "glc__D", "arab__L", "malt", "arg__L"]


# Mahadevan SOA rate-of-change bound, |v(t)-v(t-1)| <= MAX_DVDT*dt. None is the
# naive sequential FBA that oracle.FBABrain's docstring warns against holding up
# as "what a solver can do". Set from --dvdt.
MAX_DVDT: float | None = None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--community", default="BV-BO-BL-CA-EL-FP-AC-BH-ER-RI-CC")
    ap.add_argument("--out", default="runs/dm38/c11/fba_twin.json")
    ap.add_argument("--supplement", action="store_true",
                    help="minimal per-species supplement for EL and BA")
    ap.add_argument("--monoculture", action="store_true",
                    help="also run every member alone, to see who can grow at all")
    ap.add_argument("--dvdt", default="none",
                    help="Mahadevan SOA bound |v(t)-v(t-1)| <= dvdt*dt; "
                         "'none' is naive sequential FBA")
    args = ap.parse_args()

    global MAX_DVDT
    MAX_DVDT = None if args.dvdt == "none" else float(args.dvdt)
    D.SUPPLEMENT = bool(args.supplement)
    members = D.community_from_codes(args.community)
    reg, species = S.registry_for(members)
    specs = {sid: sp.spec for sid, sp in species.items()}
    comps = {sid: sp.gem.biomass_composition() for sid, sp in species.items()}

    conditions = [members]
    if args.monoculture:
        conditions = [[m] for m in members] + [members]

    results = []
    for cond in conditions:
        world = D.fresh_world(cond)
        validator = ConservationValidator(
            reg, ValidatorConfig(mode="repair", possession_severity="warn"),
            {s: specs[s] for s in cond}, {s: comps[s] for s in cond})
        placements = [Placement(s, "flask",
                                FBABrain(species[s].gem, Kinetics(vmax=10.0, km=0.02),
                                         specs[s], max_dvdt=MAX_DVDT))
                      for s in cond]
        sim = Simulator(world, placements, validator,
                        SimConfig(n_turns=D.N_TURNS, dt_h=D.DT_H,
                                  n_substeps=D.N_SUBSTEPS),
                        specs={s: specs[s] for s in cond})
        res = sim.run()
        bm = res.biomass_series()
        mets = res.metabolite_series("flask")
        x0 = D.OD_START * D.GDW_PER_OD / len(cond)
        rec = {
            "label": "-".join(S.ROSTER[s][2] for s in cond),
            "members": cond, "n_members": len(cond),
            "biomass": {s: {"start": round(bm[s][0], 7), "end": round(bm[s][-1], 6),
                            "fold": round(bm[s][-1] / max(bm[s][0], 1e-12), 2)}
                        for s in cond},
            "biomass_series": {s: [round(v, 7) for v in bm[s]] for s in cond},
            "metabolites_mM": {m: round(mets[m][-1], 4) for m in WATCH if m in mets},
            # The whole trajectory, not just the endpoint. The agent arms can
            # have theirs reconstructed by replaying applied fluxes; the solver
            # arm cannot, because its per-turn fluxes are never written to disk.
            # Without this the twin can only ever appear as a dot on a plot.
            "metabolites_series": {m: [round(v, 4) for v in mets[m]]
                                   for m in WATCH if m in mets},
            # Per-turn APPLIED fluxes, so the solver arm can be put on the same
            # cross-feeding-network figures as the agents. Until 13 Sep 2026 the
            # comment above was literally true -- the fluxes existed only inside
            # Simulator.history and were dropped on the floor -- which left dFBA
            # unable to appear in any dynamics plot. Simulator.history keeps the
            # six largest fluxes per species per turn, so a transfer of a very
            # minor metabolite can be missed; the dominant ones cannot.
            "flux_history": {s: sim.flux_full.get(s, []) for s in cond},
            "time_h": [round(i * D.DT_H, 2) for i in range(len(bm[cond[0]]))],
            "total_biomass_gdw_L": round(sum(bm[s][-1] for s in cond), 6),
            "total_fold": round(sum(bm[s][-1] for s in cond) / (x0 * len(cond)), 2),
        }
        results.append(rec)
        if len(cond) == 1:
            print(f"  mono {rec['label']:3s} fold {rec['total_fold']:8.1f}x   "
                  + " ".join(f"{m}={rec['metabolites_mM'].get(m, 0):.1f}"
                             for m in ("but", "ac", "lac__L")))
        else:
            print(f"\nCOMMUNITY {rec['label']}")
            print(f"  total biomass {rec['total_biomass_gdw_L']:.4f} gDW/L "
                  f"({rec['total_fold']:.1f}x from inoculum)")
            for s in cond:
                b = rec["biomass"][s]
                print(f"    {S.ROSTER[s][2]:3s} {s:20s} {b['end']:.5f}  {b['fold']:7.1f}x")
            print("  endpoint mM: " + "  ".join(
                f"{m}={v:.2f}" for m, v in rec["metabolites_mM"].items() if abs(v) > 0.01))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    from metabco import repair as _rep
    out.write_text(json.dumps({"medium": "DM38", "regime": "batch",
                               # so a scorecard can never be produced without
                               # saying which reconstructions it came from
                               "repair_level": _rep.level(),
                               "supplemented": bool(args.supplement),
                               "dt_h": D.DT_H, "n_turns": D.N_TURNS,
                               "treatment": args.community,
                               "conditions": results}, indent=1))
    print(f"\nwritten -> {out}")


if __name__ == "__main__":
    main()
