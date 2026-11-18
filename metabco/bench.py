"""Benchmark generation: turning genome-scale models into scored questions.

Every item here has a deterministic answer computed by linear programming, which
makes this the rare LLM benchmark with no annotator noise and no ceiling set by
human agreement. Four families, chosen because they are exactly what a curated
metabolic knowledgebase is consulted for:

  carbon_utilization  can this organism grow on X as its main carbon source?
  secretion           what does it release when fed X?
  auxotrophy          which nutrients must be supplied for it to grow at all?
  cross_feeding       can organism B live on what organism A throws away?

A caveat that shapes the whole design: these reconstructions ship with every
exchange wide open, so an unconstrained "will it grow" question measures the
medium the benchmark author chose, not the organism. Every item therefore
carries its full medium definition, and the medium is built by rule rather than
by hand.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

from .chem import MetaboliteRegistry
from .oracle import GEM

# Supplied at a trace rate so an auxotroph can build protein without any one of
# them acting as a hidden carbon source. Short-chain fatty acids are deliberately
# excluded: they are fermentation products and supplying them would confound both
# the secretion and the cross-feeding families.
AMINO_ACIDS = ["ala__L", "arg__L", "asn__L", "asp__L", "cys__L", "gln__L", "glu__L",
               "gly", "his__L", "ile__L", "leu__L", "lys__L", "met__L", "phe__L",
               "pro__L", "ser__L", "thr__L", "trp__L", "tyr__L", "val__L"]
VITAMINS = ["btn", "fol", "ribflv", "thm", "pnto__R", "nac", "pydxn", "pydx", "pydam",
            "cbl1", "adocbl", "4abz", "lipoate", "lipoa", "mqn7", "mqn8", "q8",
            "2dmmq8", "hemeA", "pheme", "sheme", "hem", "chol", "ascb__L"]
NUCLEO = ["ade", "gua", "ura", "thym", "csn", "ins", "hxan", "xan", "orot", "din"]
OTHER_FACTORS = ["ptrc", "spmd", "glyc3p", "12dgr180", "ocdca", "hdca", "ttdca"]
GROWTH_FACTORS = set(AMINO_ACIDS + VITAMINS + NUCLEO + OTHER_FACTORS)

# Fermentation products the community actually trades. Used as the label space
# for secretion questions so scoring is not dominated by trace exotica.
SCFA = ["ac", "ppa", "but", "lac__L", "lac__D", "for", "etoh", "succ", "h2", "co2",
        "isocapr", "ind3ac", "meoh", "acald", "pyr", "fum", "mal__L", "h2s", "nh4"]

# Candidate carbon sources: dietary sugars, host glycans, fibre monomers, acids.
CARBON_PANEL = [
    "glc__D", "fru", "gal", "man", "lcts", "sucr", "malt", "cellb", "melib", "raffin",
    "tre", "arab__L", "xyl__D", "rib__D", "rmn", "fuc__L", "acgam", "acgal", "acnam",
    "glcn", "gam", "sbt__D", "mnl", "glyc", "inost", "starch1", "strch1", "inulin",
    "amylopect900", "amylose300", "dextran40", "pect", "arabinan101", "xylan8",
    "lmn30", "levan1000", "chitin", "cit", "succ", "fum", "mal__L", "pyr", "lac__L",
    "ac", "ppa", "but", "etoh", "for", "urea", "ala__L", "glu__L", "asp__L", "pro__L",
    "gly", "thr__L", "ser__L", "trp__L", "his__L", "arg__L",
]


@dataclass
class BenchItem:
    """One scored question. `answer` is whatever the oracle computed."""

    item_id: str
    task: str
    species_id: str
    species_name: str
    question: str
    answer: object
    medium: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)

    def public(self) -> dict:
        """The item as an agent sees it: everything except the answer."""
        return {"item_id": self.item_id, "task": self.task,
                "species_id": self.species_id, "species_name": self.species_name,
                "question": self.question, "context": self.context}


# Freely available in the gut and in any real medium; leaving them scarce turns
# a mineral limitation into a fake metabolic phenotype.
MINERALS = {"h2o", "h", "pi", "so4", "k", "na1", "cl", "ca2", "mg2", "fe2", "fe3",
            "mn2", "zn2", "cu2", "cobalt2", "mobd", "ni2", "sel", "slnt", "tungs",
            "nh4", "co2", "hco3"}

# Bulk energy sources: sugars, polyols, polysaccharides, organic acids and SCFAs.
# Deliberately excludes amino acids, vitamins and nucleobases, which are building
# blocks -- stripping those from a medium makes auxotrophs unable to grow at all
# and turns a medium-design error into what looks like a phenotype.
ENERGY_SOURCES = {
    "glc__D", "fru", "gal", "man", "lcts", "sucr", "malt", "cellb", "melib", "raffin",
    "tre", "arab__L", "xyl__D", "rib__D", "rmn", "fuc__L", "acgam", "acgal", "acnam",
    "glcn", "gam", "sbt__D", "mnl", "glyc", "inost", "strch1", "starch1", "inulin",
    "amylopect900", "amylose300", "dextran40", "pect", "arabinan101", "xylan8",
    "xylan", "lmn30", "levan1000", "chitin", "cit", "succ", "fum", "mal__L",
    "pyr", "lac__L", "lac__D", "ac", "ppa", "but", "etoh", "for",
}


def base_bounds(gem: GEM, reg: MetaboliteRegistry, diet_bounds: dict[str, float],
                mineral: float = 1000.0, anaerobic: bool = True) -> dict[str, float]:
    """The published diet, restricted to what this organism can transport.

    An earlier version of this built a minimal medium by hand and stripped out
    every carbon source. It failed badly, and the failure is worth recording
    because it is the single easiest way to fabricate results here. Gut anaerobes
    need dipeptides rather than free amino acids, plus meso-diaminopimelate, NMN
    and haem; omit any of them and the organism scores as "cannot grow on
    anything", which reads as biology and is actually a typo in a list. Worse,
    several of these organisms are amino acid auxotrophs, so growth tracks their
    lysine or glutamate supply no matter how much sugar is present, and "growth
    on a sole carbon source" has no well-posed answer for them at all.

    Using the diet as published avoids all of that, and it is citable.
    """
    bounds = {m: v for m, v in diet_bounds.items() if m in gem.exchanges}
    for met in MINERALS:
        if met in gem.exchanges:
            bounds[met] = mineral
    if anaerobic:
        bounds.pop("o2", None)
    elif "o2" in gem.exchanges:
        bounds["o2"] = 20.0
    return bounds


def growth_on(gem: GEM, bounds: dict[str, float]) -> float:
    with gem.model:
        gem.set_medium(bounds)
        return gem.growth()


def max_uptake(gem: GEM, bounds: dict[str, float], met: str, supply: float = 10.0,
               growth_frac: float = 0.1) -> float | None:
    """Largest rate at which the organism can draw down `met` while still growing.

    This, rather than a growth yes/no, is the operational form of the question a
    knowledgebase actually answers: "does this species degrade xylan?" It stays
    well-posed for auxotrophs, whose growth rate is pinned by an amino acid
    supply and therefore says nothing about which sugars they can catabolise. It
    is also immune to the degeneracy of flux balance optima, where an equally
    optimal solution may or may not happen to route through the substrate.

    Returns None if the organism has no transporter for it at all.
    """
    if met not in gem.exchanges:
        return None
    b = dict(bounds)
    b[met] = supply
    with gem.model:
        gem.set_medium(b)
        mu = gem.growth()
        if mu < 1e-4:
            return 0.0
        gem.model.reactions.get_by_id(gem.biomass_reaction).lower_bound = growth_frac * mu
        gem.model.objective = gem.model.reactions.get_by_id(gem.exchanges[met])
        gem.model.objective_direction = "min"  # uptake is a negative flux
        v = gem.model.slim_optimize()
    return abs(float(v)) if v is not None and v == v else 0.0


# ------------------------------------------------------------ task families

def carbon_utilization_items(gem: GEM, reg: MetaboliteRegistry, species_name: str,
                             diet_bounds: dict[str, float],
                             panel: list[str] | None = None, uptake: float = 10.0,
                             anaerobic: bool = True,
                             min_uptake: float = 1.0) -> list[BenchItem]:
    """Biolog-style screen, asked as catabolic capability rather than growth."""
    panel = [c for c in (panel or CARBON_PANEL) if c in gem.exchanges]
    base = base_bounds(gem, reg, diet_bounds, anaerobic=anaerobic)
    items = []
    for carbon in panel:
        u = max_uptake(gem, base, carbon, supply=uptake)
        if u is None:
            continue
        name = reg.get(carbon).name if reg.get(carbon) else carbon
        items.append(BenchItem(
            item_id=f"carbon::{gem.species_id}::{carbon}",
            task="carbon_utilization",
            species_id=gem.species_id,
            species_name=species_name,
            question=(f"Can {species_name} take up and catabolise {name} ({carbon}) "
                      f"under {'anaerobic' if anaerobic else 'aerobic'} conditions "
                      f"in a standard gut medium? Answer yes only if this organism "
                      f"can actually consume it as a substrate."),
            answer=bool(u >= min_uptake),
            medium={"base": "western_diet_gut_agora", "carbon": carbon,
                    "supply": uptake, "anaerobic": anaerobic},
            context={"carbon_source": carbon, "carbon_source_name": name,
                     "anaerobic": anaerobic},
        ))
    return items


def secretion_items(gem: GEM, reg: MetaboliteRegistry, species_name: str,
                    diet_bounds: dict[str, float], carbons: list[str],
                    uptake: float = 10.0, anaerobic: bool = True,
                    threshold: float = 0.1) -> list[BenchItem]:
    """What does it excrete on a given carbon source?"""
    base = base_bounds(gem, reg, diet_bounds, anaerobic=anaerobic)
    items = []
    for carbon in carbons:
        if carbon not in gem.exchanges:
            continue
        b = dict(base)
        b[carbon] = uptake
        with gem.model:
            gem.set_medium(b)
            mu, fluxes = gem.solve()
        if mu < 1e-3:
            continue  # no growth means no meaningful secretion profile
        secreted = sorted(m for m, v in fluxes.items()
                          if v > threshold and m in SCFA and m not in ("co2", "h2o"))
        name = reg.get(carbon).name if reg.get(carbon) else carbon
        items.append(BenchItem(
            item_id=f"secretion::{gem.species_id}::{carbon}",
            task="secretion",
            species_id=gem.species_id,
            species_name=species_name,
            # Open-ended on purpose: no candidate list. Supplying one narrows a
            # very large metabolite space to a menu, and a menu invites picking
            # several items, which is exactly the over-listing behaviour we are
            # trying to measure rather than induce. The only steer given is a
            # reporting convention -- ignore CO2 and water -- because every
            # fermenter releases those and penalising a correct mention of them
            # would measure the convention, not the biology.
            question=(f"{species_name} is growing anaerobically on {name} ({carbon}). "
                      f"Which fermentation products does it secrete? Name them "
                      f"yourself; there is no list to choose from. Ignore carbon "
                      f"dioxide and water."),
            answer=secreted,
            medium={"carbon": carbon, "uptake": uptake, "anaerobic": anaerobic},
            context={"carbon_source": carbon, "carbon_source_name": name,
                     "growth_rate": round(mu, 4)},
        ))
    return items


def auxotrophy_items(gem: GEM, reg: MetaboliteRegistry, species_name: str,
                     diet_bounds: dict[str, float], carbon: str = "glc__D",
                     uptake: float = 10.0, nutrients: list[str] | None = None,
                     anaerobic: bool = True) -> list[BenchItem]:
    """Leave-one-out: which supplements are strictly required for growth?"""
    nutrients = nutrients or AMINO_ACIDS
    base = base_bounds(gem, reg, diet_bounds, anaerobic=anaerobic)
    if carbon not in gem.exchanges:
        return []
    base[carbon] = uptake
    full = growth_on(gem, base)
    if full < 1e-3:
        return []
    items = []
    for nut in nutrients:
        if nut not in base:
            continue
        b = dict(base)
        b.pop(nut)
        g = growth_on(gem, b)
        required = bool(g < 0.01 * full)
        name = reg.get(nut).name if reg.get(nut) else nut
        items.append(BenchItem(
            item_id=f"auxotrophy::{gem.species_id}::{nut}",
            task="auxotrophy",
            species_id=gem.species_id,
            species_name=species_name,
            question=(f"{species_name} is growing anaerobically on glucose with "
                      f"mineral salts and trace amino acids and vitamins. If "
                      f"{name} ({nut}) is removed from the medium, does growth "
                      f"stop? In other words, is it an auxotroph for {name}?"),
            answer=required,
            medium={"carbon": carbon, "removed": nut, "anaerobic": anaerobic},
            context={"nutrient": nut, "nutrient_name": name},
        ))
    return items


def cross_feeding_items(donor: GEM, recipient: GEM, reg: MetaboliteRegistry,
                        donor_name: str, recipient_name: str,
                        diet_bounds: dict[str, float], carbon: str = "glc__D",
                        uptake: float = 10.0, anaerobic: bool = True,
                        min_uptake: float = 1.0, supply: float = 10.0) -> list[BenchItem]:
    """Does the recipient consume what the donor excretes?

    The community question in its smallest form, and the one no existing LLM
    benchmark asks. Phrased as consumption rather than growth for the same reason
    as the carbon screen: most of these organisms are amino acid auxotrophs, so
    their growth rate is pinned by a supplement and would report nothing about
    whether they eat the donor's acetate.
    """
    if carbon not in donor.exchanges:
        return []
    dbase = base_bounds(donor, reg, diet_bounds, anaerobic=anaerobic)
    dbase[carbon] = uptake
    with donor.model:
        donor.set_medium(dbase)
        mu_d, fluxes = donor.solve()
    if mu_d < 1e-4:
        return []
    # CO2 is excluded even though it carries carbon. Nearly every organism here
    # releases it and many fix it anaplerotically, so counting it would mark
    # almost every pair as cross-feeding and measure nothing. Cross-feeding means
    # trading a reduced carbon skeleton, not sharing the atmosphere.
    secreted = {m: v for m, v in fluxes.items()
                if v > 0.1 and m not in ("co2", "h2o", "h")
                and (reg.get(m).carbon if reg.get(m) else 0) > 0}
    if not secreted:
        return []

    # The recipient sits in spent medium: it keeps the diet's building blocks but
    # its only fresh carbon is whatever the donor threw away.
    rbase = base_bounds(recipient, reg, diet_bounds, anaerobic=anaerobic)
    rbase.pop(carbon, None)

    # One item per exchanged metabolite rather than one per pair. Asking "does R
    # consume anything D released" is nearly always true once supply is generous,
    # so it discriminates nothing. Asking which specific metabolite moves is both
    # harder and more useful: those answers are the edges of the cross-feeding
    # graph, which is the object we actually want to predict.
    items = []
    for met in sorted(secreted):
        # Supply is a fixed generous level rather than the donor's own flux.
        # Otherwise the label depends on how fast the donor happened to excrete,
        # and a recipient perfectly able to eat acetate scores negative merely
        # because the donor released 0.6 rather than 1.2 mmol/gDW/h.
        u = max_uptake(recipient, rbase, met, supply=supply)
        name = reg.get(met).name if reg.get(met) else met
        items.append(BenchItem(
            item_id=f"crossfeed::{donor.species_id}->{recipient.species_id}::{met}",
            task="cross_feeding",
            species_id=recipient.species_id,
            species_name=recipient_name,
            question=(f"{donor_name} is fermenting {carbon} anaerobically and "
                      f"excreting {name} ({met}) into the medium. Placed in that "
                      f"spent medium, will {recipient_name} take up and consume the "
                      f"{name}?"),
            answer=bool(u is not None and u >= min_uptake),
            medium={"donor_carbon": carbon, "anaerobic": anaerobic},
            context={"donor": donor.species_id, "donor_name": donor_name,
                     "metabolite": met, "metabolite_name": name,
                     "donor_release_rate": round(secreted[met], 3)},
        ))
    return items


def cross_feeding_realized_items(donor: GEM, recipient: GEM, reg: MetaboliteRegistry,
                                 donor_name: str, recipient_name: str,
                                 diet_bounds: dict[str, float],
                                 metabolites: list[str], carbon: str = "glc__D",
                                 uptake: float = 10.0, anaerobic: bool = True,
                                 threshold: float = 0.05, supplement_scale: float = 20.0,
                                 community=None) -> list[BenchItem]:
    """Does the recipient actually consume the donor's product, in a shared medium?

    Deliberately built over the *same* (donor, recipient, metabolite) triples as
    the capability family, so the two are a matched pair differing only in what
    is being asked. Capability answers "could it, if the metabolite were
    supplied"; this answers "does it, when the two organisms share one medium and
    coexist at a common growth rate".

    They are not the same question and they do not give the same answers. At its
    own optimum Roseburia intestinalis secretes acetate rather than consuming it,
    even though its maximum acetate uptake is 7.1 mmol/gDW/h. Which of the two a
    downstream user wants depends entirely on what they are asking, and the
    disagreement between them is itself a result worth measuring.
    """
    from .community import pair_community

    com = community or pair_community(donor, recipient, reg)
    # The medium is deliberately carbon-limited, and that is not a free
    # parameter. On the diet as published these communities are limited by a
    # single amino acid, carbon sits in large excess, and no organism has any
    # reason to eat another's waste -- measured directly: zero fermentation-
    # product cross-feeding at 1x or 5x supplements, appearing only at 20x once
    # carbon uptake reaches the per-organism ceiling. Asking whether cross-
    # feeding is realized in a carbon-replete medium answers itself, so the
    # family is generated in the regime where the question has content.
    medium = {m: v * supplement_scale for m, v in diet_bounds.items()}
    for met in MINERALS:
        medium[met] = 1000.0
    if anaerobic:
        medium.pop("o2", None)
    if carbon in donor.exchanges or carbon in recipient.exchanges:
        medium[carbon] = uptake

    sol = com.solve(medium)
    d_flux = sol.exchanges.get(donor.species_id, {})
    r_flux = sol.exchanges.get(recipient.species_id, {})

    items = []
    for met in sorted(set(metabolites)):
        released = d_flux.get(met, 0.0)
        consumed = -r_flux.get(met, 0.0)
        # An item is only well-posed if the donor actually releases the
        # metabolite in this community. "Does B consume A's acetate" when A
        # secretes no acetate is not a hard negative, it is a question with no
        # subject, and a family full of them scores a model for saying no to
        # nothing. Dropping them is what makes the remaining negatives real:
        # the product is on the table and the recipient declines it.
        if released <= threshold:
            continue
        realized = bool(consumed > threshold)
        name = reg.get(met).name if reg.get(met) else met
        items.append(BenchItem(
            item_id=f"crossfeed_real::{donor.species_id}->{recipient.species_id}::{met}",
            task="cross_feeding_realized",
            species_id=recipient.species_id,
            species_name=recipient_name,
            question=(f"{donor_name} and {recipient_name} are growing together in one "
                      f"shared medium on {carbon}, coexisting at a steady state. Does "
                      f"{recipient_name} actually take up the {name} ({met}) that "
                      f"{donor_name} releases? Answer yes only if this exchange "
                      f"really occurs between them, not merely if it is possible."),
            answer=realized,
            medium={"carbon": carbon, "anaerobic": anaerobic, "mode": "community"},
            context={"donor": donor.species_id, "donor_name": donor_name,
                     "metabolite": met, "metabolite_name": name,
                     "community_growth_rate": sol.growth_rate,
                     "donor_flux": round(released, 4),
                     "recipient_flux": round(-consumed, 4)},
        ))
    return items


# ------------------------------------------------------------------ storage

def save_benchmark(items: list[BenchItem], path: str | Path,
                   split_answers: bool = True) -> tuple[Path, Path | None]:
    """Write questions and answers to separate files.

    Splitting them is not bureaucracy. It is what makes it possible to evaluate a
    model -- including the one writing this code -- without the answers ever
    being in the same file as the questions.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for it in items:
            fh.write(json.dumps(it.public()) + "\n")
    ans_path = None
    if split_answers:
        ans_path = path.with_suffix(".answers.jsonl")
        with open(ans_path, "w") as fh:
            for it in items:
                fh.write(json.dumps({"item_id": it.item_id, "answer": it.answer,
                                     "medium": it.medium}) + "\n")
    return path, ans_path


def load_questions(path: str | Path) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]


def load_answers(path: str | Path) -> dict[str, object]:
    return {d["item_id"]: d["answer"]
            for d in (json.loads(l) for l in Path(path).read_text().splitlines() if l.strip())}


# ------------------------------------------------------------------ scoring

def score_binary(preds: dict[str, bool], answers: dict[str, bool]) -> dict:
    """Accuracy plus the confusion matrix, because these tasks are unbalanced.

    Accuracy alone is misleading when 80% of carbon sources are unusable: a model
    that answers "no" to everything scores 0.8 and has learned nothing. Balanced
    accuracy and MCC are the numbers to report.
    """
    tp = fp = tn = fn = 0
    n_missing = 0
    for item_id, truth in answers.items():
        if item_id not in preds:
            n_missing += 1
            continue
        p = bool(preds[item_id])
        if p and truth:
            tp += 1
        elif p and not truth:
            fp += 1
        elif not p and truth:
            fn += 1
        else:
            tn += 1
    n = tp + fp + tn + fn
    acc = (tp + tn) / n if n else 0.0
    sens = tp / (tp + fn) if (tp + fn) else 0.0
    spec = tn / (tn + fp) if (tn + fp) else 0.0
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    f1 = 2 * prec * sens / (prec + sens) if (prec + sens) else 0.0
    denom = ((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)) ** 0.5
    mcc = ((tp * tn - fp * fn) / denom) if denom else 0.0
    base_rate = (tp + fn) / n if n else 0.0
    # Balanced accuracy needs both classes. When a slice contains only
    # positives or only negatives, one of sensitivity and specificity is
    # undefined and averaging it as zero silently halves the score -- which
    # reads as a catastrophic failure on exactly the organisms the benchmark
    # happens to have easy labels for. Flag it so callers report accuracy there.
    single_class = (tp + fn) == 0 or (tn + fp) == 0
    return {
        "n": n, "n_missing": n_missing, "accuracy": round(acc, 4),
        "single_class": single_class,
        "balanced_accuracy": (round(acc, 4) if single_class
                              else round((sens + spec) / 2, 4)),
        "sensitivity": round(sens, 4), "specificity": round(spec, 4),
        "precision": round(prec, 4), "f1": round(f1, 4), "mcc": round(mcc, 4),
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "positive_rate_truth": round(base_rate, 4),
        "positive_rate_pred": round((tp + fp) / n, 4) if n else 0.0,
        "majority_baseline": round(max(base_rate, 1 - base_rate), 4),
    }


def score_sets(preds: dict[str, list[str]], answers: dict[str, list[str]]) -> dict:
    """Jaccard and micro-F1 over set-valued answers such as secretion profiles."""
    jac, tp = [], 0
    fp = fn = 0
    n_missing = 0
    for item_id, truth in answers.items():
        if item_id not in preds:
            n_missing += 1
            continue
        t, p = set(truth), set(preds[item_id] or [])
        inter = len(t & p)
        union = len(t | p)
        jac.append(inter / union if union else 1.0)
        tp += inter
        fp += len(p - t)
        fn += len(t - p)
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    return {
        "n": len(jac), "n_missing": n_missing,
        "mean_jaccard": round(sum(jac) / len(jac), 4) if jac else 0.0,
        "micro_precision": round(prec, 4), "micro_recall": round(rec, 4),
        "micro_f1": round(2 * prec * rec / (prec + rec), 4) if (prec + rec) else 0.0,
        "tp": tp, "fp": fp, "fn": fn,
    }


# Metabolites neither credited nor penalised in open-ended secretion answers.
# Every fermenter releases CO2 and water; the question tells the model to ignore
# them, and scoring has to honour that or it measures compliance with a
# convention rather than knowledge of the biology.
SECRETION_IGNORED = {"co2", "h2o", "h", "hco3"}


def normalize_set_answer(answer, reg: MetaboliteRegistry) -> list[str]:
    """Map a free-text product list onto canonical metabolite ids.

    Needed once the secretion question stops offering a menu: the model then
    answers with whatever it calls things -- "acetate", "L-lactate", "butyric
    acid" -- and an exact-string comparison against BiGG ids would score the
    naming convention instead of the prediction. Unresolvable tokens are kept
    verbatim so they still count as wrong rather than silently vanishing.
    """
    if isinstance(answer, str):
        answer = [a.strip() for a in answer.replace(";", ",").split(",")]
    out = []
    for tok in (answer or []):
        if not isinstance(tok, str):
            continue
        tok = tok.strip()
        if not tok:
            continue
        mid = reg.resolve(tok) or tok.lower()
        if mid in SECRETION_IGNORED:
            continue
        out.append(mid)
    return sorted(set(out))
