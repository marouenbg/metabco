"""Refinements applied to a draft reconstruction in memory, never on disk.

READ THIS FIRST. Unrepaired AGORA2 grows 25/25 on the VMH western diet and 1/25
on DM38. The reconstructions are not broken -- DM38 is a 64-component minimal
medium far outside the rich-gut regime they were validated in, and it does not
supply the meso-DAP, purines and peptides they expect to be handed. Nothing here
should be described as "fixing AGORA2".

What these edits DO establish: Clark measured all 25 species growing in DM38, so
the organisms synthesise those compounds themselves and the models cannot. The
edits are the price of moving a draft reconstruction to a minimal defined medium,
and their size is the measurement.


The dFBA twin refuses to grow 14 of Clark's 24 species in Clark's own defined
medium. `scripts/diagnose_abstention.py` shows none of those is a real metabolic
incapability -- all 24 grow once every exchange is opened -- so the abstention is
an annotation gap. Two gaps account for nine of the fourteen:

  nicotinamide    Clark supplies nicotinamide (ncam, 0.034 mM, verified in Supp
                  Data 4). Every one of the 24 models carries a NICOTINATE
                  exchange and only two carry a nicotinamide one, though 21
                  already have cytoplasmic ncam and ten already have the
                  nicotinamidase that converts it. The organisms unquestionably
                  use nicotinamide -- PncA is near-universal in bacteria -- so
                  the missing piece is the transporter annotation, not the
                  metabolism.
  sulfide         Cysteine desulfhydrase, which releases H2S from cysteine, is
                  annotated in only 4 of 24. Cysteine is abundant in DM38
                  (8.4 mM) and H2S production from it is a documented general
                  property of the gut microbiota.

Both edits name a specific enzyme with a specific EC activity, which is what
separates this from gap-filling by search: nothing here was chosen because it
made a particular species grow.

Applied UNIFORMLY to every model missing the reaction, not only to the ones it
rescues. Repairing only where it helps would be fitting to the outcome, which is
the practice these repairs exist to avoid.

Nothing is written to data/gems. Enable with load_species(..., repair=True) or
METABCO_REPAIR_GEMS=1, so the unrepaired arm stays the default and the two are
never conflated.
"""

from __future__ import annotations

import os

# (id, name, stoichiometry, bounds). Stoichiometry copied verbatim from the
# models that already carry these reactions -- B. longum for both -- so the
# repaired models are internally consistent with the unrepaired ones.
NNAM = ("NNAM", "nicotinamidase",
        {"h2o[c]": -1.0, "ncam[c]": -1.0, "nac[c]": 1.0, "nh4[c]": 1.0},
        (0.0, 1000.0))
CYSDS = ("CYSDS", "Cysteine Desulfhydrase",
         {"cys_L[c]": -1.0, "h2o[c]": -1.0,
          "h2s[c]": 1.0, "nh4[c]": 1.0, "pyr[c]": 1.0},
         (0.0, 1000.0))
NCAMT = ("NCAMt2r", "nicotinamide transport",
         {"h[c]": -1.0, "ncam[c]": -1.0, "h[e]": 1.0, "ncam[e]": 1.0},
         (-1000.0, 1000.0))
EX_NCAM = ("EX_ncam(e)", "nicotinamide exchange", {"ncam[e]": -1.0},
           (-1000.0, 1000.0))

# meso-diaminopimelate dehydrogenase, EC 1.4.1.16. B. vulgatus and
# C. aerofaciens carry DHDPS and DHDPR -- so they make tetrahydrodipicolinate --
# and then stop, with meso-DAP reachable only through DAPabc, an ABC IMPORTER.
# No defined medium contains free diaminopimelate, so as published the models
# demand a compound nothing can supply. Their peptidoglycan contains meso-DAP,
# so the organisms unquestionably synthesise it; only the route is uncertain
# (this one-step dehydrogenase, or the four-step succinylase branch that
# B. longum carries). Adding the shortest completion from an intermediate they
# already produce is the minimal coherent repair.
DAPDH = ("DAPDH", "meso-diaminopimelate dehydrogenase",
         {"thdp[c]": -1.0, "nh4[c]": -1.0, "nadph[c]": -1.0, "h[c]": -1.0,
          "26dap_M[c]": 1.0, "nadp[c]": 1.0, "h2o[c]": 1.0},
         (0.0, 1000.0))

# CTP synthase (glutamine-dependent), EC 6.3.4.2, gene pyrG. C. hiranonis has
# NO producing reaction for ctp[c] at all -- while UTP is perfectly producible --
# so it cannot make RNA, and dCTP (via RNTR3) is unreachable, which was the
# single blocked biomass component keeping it from growing. Every other
# reconstruction in the roster carries CTPS1 and CTPS2. An organism without CTP
# synthase cannot exist; this is a missing essential gene, not a metabolic
# choice. Stoichiometry copied from C. aerofaciens.
CTPS2 = ("CTPS2", "CTP Synthase (Glutamine)",
         {"atp[c]": -1.0, "gln_L[c]": -1.0, "h2o[c]": -1.0, "utp[c]": -1.0,
          "adp[c]": 1.0, "ctp[c]": 1.0, "glu_L[c]": 1.0, "h[c]": 2.0,
          "pi[c]": 1.0},
         (0.0, 1000.0))

# GMP reductase, EC 1.7.1.7. Carried by ZERO of the 25 reconstructions, and its
# absence is what keeps seven species from growing on DM38.
#
# None of the 25 has de novo purine biosynthesis either -- in every one, imp[c]
# is produced only by HXPRT, which is salvage from hypoxanthine, and DM38
# contains no hypoxanthine. So adenine nucleotides can only come from free
# adenine, which DM38 supplies at 6e-5 mM. Meanwhile xanthine is present at
# 0.025 mM, 400x more, but IMPD is irreversible (IMP -> XMP) in all 25, so
# xanthine can only ever feed the GUANINE branch and never reach AMP.
#
# GMP reductase is the physiological link between the two branches, and adding
# it lets abundant xanthine serve the adenine branch: xan -> XMP -> GMP -> IMP
# -> AMP. Growth in the seven stuck species goes from 0.13-0.32 to 1.6-3.4 /h.
# Reversing IMPD gives the same numbers but is not real chemistry -- IMP + NAD
# -> XMP is strongly favourable forward and does not run backwards in cells.
GMPR = ("GMPR", "GMP reductase",
        {"gmp[c]": -1.0, "nadph[c]": -1.0, "h[c]": -1.0,
         "imp[c]": 1.0, "nadp[c]": 1.0, "nh4[c]": 1.0},
        (0.0, 1000.0))

NCAM_FORMULA, NCAM_CHARGE = "C6H6N2O", 0

# Nutrients DM38 supplies that some models simply cannot import. Every one is a
# near-universal bacterial uptake route for a compound sitting in the medium, so
# the missing piece is the transporter annotation and not the metabolism -- the
# same class of gap as nicotinamide. Listed as (model-side base id, the id the
# registry/medium uses). Applied uniformly to every model missing the route.
#
# BV alone is missing four of these: pantothenate, cysteine, methionine and
# thiamine. That is not a species with unusual requirements, it is an
# under-annotated reconstruction.
NUTRIENT_UPTAKES = [
    ("cys_L", "cys__L"), ("met_L", "met__L"), ("pnto_R", "pnto__R"),
    ("thm", "thm"), ("cbl1", "cbl1"),
    # Nucleobases and nucleosides. DM38 supplies adenine, guanine, xanthine,
    # cytosine, uracil, thymidine and orotate; the July-2024 AGORA2 release has
    # the cytoplasmic form of every one of them in all twelve species that still
    # abstain, and a transporter for almost none -- adenine and orotate have an
    # exchange in ZERO of the twelve. Purine and pyrimidine salvage transport is
    # near-universal in bacteria, so this is the nicotinamide gap again, in a
    # different vitamin class.
    ("ade", "ade"), ("gua", "gua"), ("xan", "xan"), ("csn", "csn"),
    ("ura", "ura"), ("thymd", "thymd"), ("orot", "orot"),
    # AMINO ACIDS. DM38 supplies all twenty, and every species that still
    # abstains carries the cytoplasmic form of the ones it cannot import --
    # B. caccae and E. lenta are missing transporters for FOURTEEN of the
    # twenty, B. vulgatus for twelve. Amino-acid uptake is universal in
    # bacteria and these are gut anaerobes sitting in an amino-acid-rich
    # medium; a reconstruction that metabolises serine but cannot take it up
    # is describing a transporter annotation gap, not an organism.
    ("ala_L", "ala__L"), ("arg_L", "arg__L"), ("asn_L", "asn__L"),
    ("asp_L", "asp__L"), ("gln_L", "gln__L"), ("glu_L", "glu__L"),
    ("gly", "gly"), ("his_L", "his__L"), ("ile_L", "ile__L"),
    ("leu_L", "leu__L"), ("lys_L", "lys__L"), ("phe_L", "phe__L"),
    ("pro_L", "pro__L"), ("ser_L", "ser__L"), ("thr_L", "thr__L"),
    ("trp_L", "trp__L"), ("tyr_L", "tyr__L"), ("val_L", "val__L"),
]


# LEVEL 2 -- a weaker category of edit, off by default. Biomass substrates the
# model demands but can NEITHER synthesise NOR import. That is an internal
# inconsistency in the reconstruction whichever side is wrong: either the
# requirement is spurious, or the biosynthesis pathway is missing. Deleting the
# requirement resolves it in the direction that does not invent chemistry.
#
# Applied UNIFORMLY wherever the component appears as a biomass substrate, never
# per species. q8 is the clearest case and stands on physiology alone.
BIOMASS_REMOVE = {
    "q8": "ubiquinone-8, the AEROBIC respiratory quinone; every species in this "
          "roster is an obligate anaerobe. Template inheritance.",
    "mqn8": "menaquinone-8: demanded with no synthesis route and no exchange",
    "2dmmq8": "demethylmenaquinone-8: same",
    "sheme": "siroheme: same",
    "spmd": "spermidine: same",
    "ptrc": "putrescine: same",
}


# LEVEL 3 -- per-species gap fill, the weakest tier and off by default.
# Six reconstructions still cannot grow on DM38 after every transport and
# biomass repair, yet Clark measured all six growing in that exact medium
# (22x to 254x from inoculum). The missing capability is biosynthetic, so the
# evidence source is the other 24 gut-anaerobe reconstructions in the roster:
# scripts/panroster_gapfill.py grafts the pan-roster network, confirms growth,
# then prunes to the smallest set that still works.
#
# Unlike levels 1 and 2 this is PER SPECIES, which is exactly the outcome-fitted
# gap filling this project otherwise avoids -- so every added reaction is stored
# with how many of the 25 siblings carry it, and that number is the honesty
# check. Phosphoglucomutase in 24/25 is a different claim from a fatty-acid
# reaction in 1/25, and the paper must report the distribution, not the count.
GAPFILL_FILE = "data/gapfill_reactions.json"
_GAPFILL: dict | None = None


def gapfill_spec() -> dict:
    global _GAPFILL
    if _GAPFILL is None:
        import json
        from pathlib import Path
        p = Path(GAPFILL_FILE)
        _GAPFILL = json.loads(p.read_text()) if p.exists() else {
            "per_species": {}, "reactions": {}, "metabolites": {}}
    return _GAPFILL


def apply_gapfill(gem, species_id: str) -> list[str]:
    from cobra import Metabolite, Reaction
    spec = gapfill_spec()
    rids = next((v["added"] for v in spec["per_species"].values()
                 if v["species_id"] == species_id), [])
    model, added = gem.model, []
    for rid in rids:
        if rid in model.reactions or rid not in spec["reactions"]:
            continue
        d = spec["reactions"][rid]
        for k in d["stoich"]:
            if k not in model.metabolites:
                t = spec["metabolites"].get(k, {})
                model.add_metabolites([Metabolite(
                    k, formula=t.get("formula"), charge=t.get("charge"),
                    compartment=k.rsplit("[", 1)[-1].rstrip("]"),
                    name=t.get("name", k))])
        r = Reaction(rid, name=d.get("name") or rid,
                     lower_bound=d["bounds"][0], upper_bound=d["bounds"][1])
        model.add_reactions([r])
        r.add_metabolites({model.metabolites.get_by_id(k): v
                           for k, v in d["stoich"].items()})
        added.append(rid)
    return added


def level() -> int:
    v = os.environ.get("METABCO_REPAIR_GEMS", "").lower()
    if v in ("3", "gapfill"):
        return 3
    if v in ("2", "biomass"):
        return 2
    return 1 if v in ("1", "true", "yes") else 0


def enabled() -> bool:
    return level() > 0


def _ensure_metabolite(model, met_id: str, formula: str, charge: int, comp: str,
                       name: str):
    """Create a metabolite if absent. `name` is what the AGENT will read.

    It used to be hardcoded to "nicotinamide" because this helper was written
    for exactly that job. The registry is the union over all 25 models, so a
    repaired ncam[e] carrying a lowercase name outvoted the "Nicotinamide" the
    other reconstructions use, and the prompt silently changed case. Harmless
    there, but any future caller would have relabelled its metabolite
    "nicotinamide" in the agent's own nutrient list.
    """
    from cobra import Metabolite
    if met_id in model.metabolites:
        return model.metabolites.get_by_id(met_id)
    m = Metabolite(met_id, formula=formula, charge=charge, compartment=comp,
                   name=name)
    model.add_metabolites([m])
    return m


def _ensure_reaction(model, spec) -> bool:
    """Add one reaction if absent. Returns True if it was added."""
    from cobra import Reaction
    rid, name, stoich, bounds = spec
    if rid in model.reactions:
        return False
    if any(m not in model.metabolites for m in stoich):
        return False          # a precursor is genuinely absent; do not invent it
    r = Reaction(rid, name=name, lower_bound=bounds[0], upper_bound=bounds[1])
    model.add_reactions([r])
    r.add_metabolites({model.metabolites.get_by_id(m): c for m, c in stoich.items()})
    return True


def _add_uptake(model, base: str) -> list[str]:
    """Give a model an exchange + transport for a cytoplasmic metabolite it
    already uses but cannot import. Mirrors the cytoplasmic form's formula and
    charge so the new extracellular species is not a fresh invention."""
    from cobra import Metabolite, Reaction
    cyt, ext = f"{base}[c]", f"{base}[e]"
    exid, tid = f"EX_{base}(e)", f"{base.upper()}t_repair"
    if cyt not in model.metabolites:
        return []
    c = model.metabolites.get_by_id(cyt)
    added: list[str] = []
    if ext not in model.metabolites:
        model.add_metabolites([Metabolite(ext, formula=c.formula, charge=c.charge,
                                          compartment="e", name=c.name)])
    e = model.metabolites.get_by_id(ext)

    # An exchange with no TRANSPORTER is a dead end: the compound reaches the
    # extracellular compartment and cannot cross the membrane. Several models
    # are in exactly that state -- C. comes carries EX_trp_L(e) and trp_L[e],
    # while the only producers of cytoplasmic tryptophan are the synthases, so
    # 0.73 mM of medium tryptophan is unreachable and the cell starves for an
    # amino acid it is standing in. The earlier version of this function
    # returned early whenever the exchange already existed, which skipped
    # precisely these cases.
    if not any({e.id, c.id} <= {mm.id for mm in r.metabolites} for r in c.reactions):
        t = Reaction(tid, name=f"{c.name} transport",
                     lower_bound=-1000, upper_bound=1000)
        model.add_reactions([t])
        t.add_metabolites({e: -1.0, c: 1.0})
        added.append(tid)

    if exid not in model.reactions and not any(
            r.id.startswith("EX_") and e in r.metabolites for r in e.reactions):
        x = Reaction(exid, name=f"{c.name} exchange",
                     lower_bound=-1000, upper_bound=1000)
        model.add_reactions([x])
        x.add_metabolites({e: -1.0})
        added.append(exid)
    return added


def repair(gem, biomass: bool = False, gapfill: bool = False,
           species_id: str | None = None) -> list[str]:
    """Apply repairs to a loaded GEM. Returns what changed.

    biomass=True additionally applies the LEVEL 2 biomass edits.
    """
    model = gem.model
    added: list[str] = []

    # Three models (CA, CC, BA) lack cytoplasmic nicotinamide entirely while
    # still carrying nac[c] and the rest of NAD salvage. Adding it is
    # bookkeeping that follows from the enzyme: a cell with nicotinamidase has
    # cytoplasmic nicotinamide by definition. Gated on nac[c] so the pathway
    # must already be there -- we are completing an annotation, not inventing a
    # capability.
    if "nac[c]" in model.metabolites and "ncam[c]" not in model.metabolites:
        _ensure_metabolite(model, "ncam[c]", NCAM_FORMULA, NCAM_CHARGE, "c",
                           "Nicotinamide")
    if "ncam[c]" in model.metabolites:
        _ensure_metabolite(model, "ncam[e]", NCAM_FORMULA, NCAM_CHARGE, "e",
                               "Nicotinamide")
        for spec in (NCAMT, EX_NCAM, NNAM):
            if _ensure_reaction(model, spec):
                added.append(spec[0])
    if _ensure_reaction(model, CYSDS):
        added.append(CYSDS[0])
    if _ensure_reaction(model, DAPDH):
        added.append(DAPDH[0])
    if _ensure_reaction(model, CTPS2):
        added.append(CTPS2[0])
    if _ensure_reaction(model, GMPR):
        added.append(GMPR[0])
    for base, _ in NUTRIENT_UPTAKES:
        added.extend(_add_uptake(model, base))

    if biomass:
        bio = model.reactions.get_by_id(gem.biomass_reaction)
        drop = {}
        for base in BIOMASS_REMOVE:
            mid = f"{base}[c]"
            if mid in model.metabolites:
                mm = model.metabolites.get_by_id(mid)
                if bio.metabolites.get(mm, 0.0) < 0:
                    drop[mm] = -bio.metabolites[mm]
        if drop:
            bio.add_metabolites(drop, combine=True)
            added.extend(f"-biomass:{m.id[:-3]}" for m in drop)

    if gapfill and species_id:
        added.extend(apply_gapfill(gem, species_id))

    if added:
        # exchanges is built once at load; refresh it so the new exchange is
        # visible to bounds_for, possession checks and the renderer.
        from metabco.oracle import ex_to_met
        gem.exchanges = {ex_to_met(r.id): r.id for r in model.exchanges}
    return added
