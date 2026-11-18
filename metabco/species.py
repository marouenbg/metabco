"""The species roster: eleven gut organisms, and how an agent learns what it is.

Each entry pairs a curated genome-scale model with a short natural-language
"card". The card is the knowledgebase we are trying to replace, so it exists in
graded strengths -- that grading is the experiment:

  none         the agent is told only its species name (parametric knowledge only)
  literal      two lines of textbook physiology a microbiologist would recite
  derived      a capability inventory generated mechanically from the organism's GEM
  behavioural  what the organism *does* under a fixed panel of conditions
  full         derived plus the explicit transporter list

The two GEM-derived tiers are the interesting ones, and the difference between
them is the point. Both are lossy projections of the same stoichiometric model
into text; they differ only in framing. `derived` describes capability -- what
the organism can exchange, what it cannot, how many carbon-bearing metabolites
it has. `behavioural` describes realisation -- what it consumed and released
under glucose, under a mucin sugar, under a rich diet.

Measured on 462 paired benchmark items, the capability framing was worse than
telling the model nothing at all. Given a list of secretable products it
asserted 100% of them as actually secreted; given a short list of absent
transporters it read the list as exhaustive and denied everything unnamed. Both
failures are the same mistake -- a static inventory being read as a prediction of
behaviour -- which is why `behavioural` exists and why it never enumerates a
capability.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from pathlib import Path

from .oracle import GEM
from .types import SpeciesSpec

# AGORA2 v2.01, sbml_files_fixed (2024-07-04). The previous path was
# named "agora2" but held AGORA 1.03 -- the version is in the name now
# so that cannot recur.
GEM_DIR = "data/gems/agora2_v201_utf8"

# Short physiology, the kind found in the first paragraph of a review. Kept
# deliberately terse: this is the "prompt engineering" tier, and if it were long
# enough to encode the model we would be smuggling the answer in.
LITERAL_CARDS: dict[str, dict] = {
    "acaccae": {
        "name": "Anaerostipes caccae DSM 14662",
        "file": "Anaerostipes_caccae_DSM_14662",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe. Lactate- and acetate-consuming butyrate producer; a key sink that keeps lactate from accumulating in the colon.",
    },
    "ccomes": {
        "name": "Coprococcus comes ATCC 27758",
        "file": "Coprococcus_comes_ATCC_27758",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe, butyrate producer, ferments a range of dietary sugars.",
    },
    "elenta": {
        "name": "Eggerthella lenta DSM 2243",
        "file": "Eggerthella_lenta_DSM_2243",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe, Actinobacterium. Non-saccharolytic; known for reductive drug and bile-acid metabolism rather than fibre fermentation.",
    },
    "dpiger": {
        "name": "Desulfovibrio piger ATCC 29098",
        "file": "Desulfovibrio_piger_ATCC_29098",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe, sulfate-reducing. Consumes hydrogen and lactate and releases hydrogen sulfide.",
    },
    "bhydrogenotrophica": {
        "name": "Blautia hydrogenotrophica DSM 10507",
        "file": "Blautia_hydrogenotrophica_DSM_10507",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe, reductive acetogen. Fixes CO2 and H2 into acetate via the Wood-Ljungdahl pathway.",
    },
    "caerofaciens": {
        "name": "Collinsella aerofaciens ATCC 25986",
        "file": "Collinsella_aerofaciens_ATCC_25986",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe, Actinobacterium. Ferments sugars to lactate, formate and hydrogen.",
    },
    "pcopri": {
        "name": "Prevotella copri CB7 DSM 18205",
        "file": "Prevotella_copri_CB7_DSM_18205",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe. Fibre-degrading generalist, the marker of the Prevotella enterotype; makes succinate and acetate.",
    },
    "pjohnsonii": {
        "name": "Parabacteroides johnsonii DSM 18315",
        "file": "Parabacteroides_johnsonii_DSM_18315",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe. Polysaccharide degrader, succinate and acetate producer.",
    },
    "dlongicatena": {
        "name": "Dorea longicatena DSM 13814",
        "file": "Dorea_longicatena_DSM_13814",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe. Sugar fermenter releasing formate, acetate and hydrogen; does not make butyrate.",
    },
    # Clark's 25th species, available for the first time. Every earlier probe
    # failed because AGORA2's filename concatenates BOTH strain designators --
    # Clostridium_hiranonis_TO_931_DSM_13275, not TO_931 or DSM_13275 alone.
    # Reclassified as Peptacetobacter hiranonis; AGORA2 keeps the old genus.
    "chiranonis": {
        "name": "Clostridium hiranonis TO-931 (DSM 13275)",
        "file": "Clostridium_hiranonis_TO_931_DSM_13275",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe; bile-acid 7-alpha-dehydroxylating Clostridium.",
    },
    # Clark's 26th species column, unmodelled until 12 Sep 2026. Every earlier
    # probe searched AGORA2 for "Holdemanella" and found nothing: the organism
    # was reclassified out of Eubacterium in 2013 and AGORA2 indexes it under
    # the pre-reclassification name. Exactly the C. hiranonis failure again --
    # the reconstruction was never missing, the NAME was.
    #
    # It matters more than one species usually would: 82% of HB's pairwise
    # interaction coefficients in Clark are FACILITATIVE, against 23% in the
    # pairs we already model, so excluding it made our benchmark 76.9%
    # antagonistic where Clark's full dataset is 70.6%.
    "hbiformis": {
        "name": "Holdemanella biformis DSM 3989",
        "file": "Eubacterium_biforme_DSM_3989",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe, saccharolytic Erysipelotrichaceae; "
                "ferments sugars to lactate and acetate.",
    },
    "casparagiforme": {
        "name": "Clostridium asparagiforme DSM 15981",
        "file": "Clostridium_asparagiforme_DSM_15981",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe, saccharolytic Clostridium.",
    },
    "bovatus": {
        "name": "Bacteroides ovatus ATCC 8483",
        "file": "Bacteroides_ovatus_ATCC_8483",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe. Broad polysaccharide degrader, notably of hemicelluloses; releases acetate, propionate and succinate.",
    },
    "buniformis": {
        "name": "Bacteroides uniformis ATCC 8492",
        "file": "Bacteroides_uniformis_ATCC_8492",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe. Abundant generalist fibre degrader of the human colon.",
    },
    "bvulgatus": {
        "name": "Bacteroides vulgatus ATCC 8482",
        "file": "Bacteroides_vulgatus_ATCC_8482",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe. Among the most abundant Bacteroides in the human gut; polysaccharide degrader.",
    },
    "bcaccae": {
        "name": "Bacteroides caccae ATCC 43185",
        "file": "Bacteroides_caccae_ATCC_43185",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe. Polysaccharide and host-glycan degrader.",
    },
    "bcellulosilyticus": {
        "name": "Bacteroides cellulosilyticus DSM 14838",
        "file": "Bacteroides_cellulosilyticus_DSM_14838",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe with an unusually large repertoire of fibre-degrading enzymes.",
    },
    "dformicigenerans": {
        "name": "Dorea formicigenerans ATCC 27755",
        "file": "Dorea_formicigenerans_ATCC_27755",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe. Sugar fermenter releasing formate and acetate.",
    },
    "bpseudocatenulatum": {
        "name": "Bifidobacterium pseudocatenulatum DSM 20438",
        "file": "Bifidobacterium_pseudocatenulatum_DSM_20438",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe. Bifid shunt: ferments hexoses to acetate and lactate in a 3:2 ratio.",
    },
    "btheta": {
        "name": "Bacteroides thetaiotaomicron VPI-5482",
        "file": "Bacteroides_thetaiotaomicron_VPI_5482",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe, Gram-negative. The archetypal gut glycan "
                "generalist: an enormous repertoire of polysaccharide utilisation "
                "loci lets it degrade dietary fibre and host mucin O-glycans. "
                "Ferments sugars to acetate, propionate and succinate. Does not "
                "make butyrate.",
    },
    "bfragilis": {
        "name": "Bacteroides fragilis NCTC 9343",
        "file": "Bacteroides_fragilis_NCTC_9343",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe but unusually aerotolerant for a Bacteroides. "
                "Bile-resistant, capsule-forming, common opportunistic pathogen "
                "outside the gut. Ferments sugars to acetate, propionate and "
                "succinate.",
    },
    "badolescentis": {
        "name": "Bifidobacterium adolescentis ATCC 15703",
        "file": "Bifidobacterium_adolescentis_ATCC_15703",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe, Gram-positive. Uses the bifid shunt "
                "(fructose-6-phosphate phosphoketolase) rather than glycolysis, "
                "giving acetate and lactate in roughly a 3:2 ratio. Degrades "
                "starch and fructans. No butyrate, no propionate.",
    },
    "blongum": {
        "name": "Bifidobacterium longum NCC2705",
        "file": "Bifidobacterium_longum_NCC2705",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe, Gram-positive. Bifid shunt fermenter yielding "
                "acetate and lactate. Prominent in the infant gut, where it "
                "consumes human milk oligosaccharides. No butyrate, no propionate.",
    },
    "fprausnitzii": {
        "name": "Faecalibacterium prausnitzii A2-165",
        "file": "Faecalibacterium_prausnitzii_A2_165",
        "aerotolerance": "anaerobe",
        "card": "Extremely oxygen-sensitive obligate anaerobe, Gram-positive. One "
                "of the most abundant butyrate producers in the healthy colon and "
                "depleted in Crohn's disease. Consumes acetate and lactate and "
                "converts them, with sugars, to butyrate.",
    },
    "rintestinalis": {
        "name": "Roseburia intestinalis L1-82",
        "file": "Roseburia_intestinalis_L1_82",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe, Gram-positive, motile. Butyrate producer that "
                "cross-feeds on acetate; degrades dietary fibre including resistant "
                "starch and xylan. Uses the butyryl-CoA:acetate CoA-transferase route.",
    },
    "erectale": {
        "name": "Eubacterium rectale ATCC 33656",
        "file": "Eubacterium_rectale_ATCC_33656",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe, Gram-positive. Major colonic butyrate producer, "
                "acetate-consuming, specialised on starch and other dietary "
                "polysaccharides.",
    },
    "amuciniphila": {
        "name": "Akkermansia muciniphila ATCC BAA-835",
        "file": "Akkermansia_muciniphila_ATCC_BAA_835",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe (nanaerobe, tolerates trace oxygen), "
                "Gram-negative. Mucin specialist: lives in the mucus layer and "
                "degrades host mucin O-glycans as its carbon and nitrogen source, "
                "releasing acetate and propionate. Grows poorly on dietary sugars.",
    },
    "lplantarum": {
        "name": "Lactiplantibacillus plantarum WCFS1",
        "file": "Lactobacillus_plantarum_WCFS1",
        "aerotolerance": "facultative",
        "card": "Aerotolerant, facultatively heterofermentative lactic acid "
                "bacterium, Gram-positive. Ferments a wide range of sugars mainly "
                "to lactate, with acetate and ethanol under some conditions. "
                "Nutritionally fastidious: multiple amino acid and vitamin "
                "auxotrophies.",
    },
    "cdifficile": {
        "name": "Clostridioides difficile 630",
        "file": "Clostridioides_difficile_630",
        "aerotolerance": "anaerobe",
        "card": "Obligate anaerobe, Gram-positive, spore-forming toxigenic "
                "pathogen. Expands after antibiotic disruption. Ferments "
                "carbohydrates and, distinctively, performs Stickland fermentation "
                "of amino acid pairs (proline and glycine as acceptors, leucine "
                "and others as donors). Produces butyrate, acetate and isocaproate.",
    },
    "ecoli": {
        "name": "Escherichia coli K-12 MG1655",
        "file": "Escherichia_coli_str_K_12_substr_MG1655",
        "aerotolerance": "facultative",
        "card": "Facultative anaerobe, Gram-negative. Respires oxygen or nitrate "
                "when available and otherwise performs mixed-acid fermentation "
                "giving acetate, formate, ethanol, lactate and succinate. "
                "Prototrophic: grows on a single sugar plus mineral salts.",
    },
}


@dataclass
class Species:
    """A roster entry: identity, model, and cards at every grounding tier."""

    species_id: str
    spec: SpeciesSpec
    gem: GEM

    @property
    def name(self) -> str:
        return self.spec.name


def synth_meta(species_id: str, gem_dir: str = GEM_DIR,
               name: str | None = None) -> dict:
    """Metadata for a strain with no hand-written card.

    LITERAL_CARDS covers 29 organisms, 25 of which are Clark's -- so a corpus
    built from it and evaluated on Clark has almost nothing left to train on.
    Reaching the 964-strain roster needs the three fields load_species actually
    uses, and all three are derivable:

        file           the species_id IS the filename for roster strains
        name           the id with underscores read back as words
        aerotolerance  inferred from whether the reconstruction carries an
                       oxygen exchange at all. Coarse, but it only reaches the
                       literal card text, and derive_card -- the tier used for
                       scale -- reads nothing from meta.
    """
    import os
    has_o2 = False
    path = os.path.join(gem_dir, species_id + ".xml")
    if os.path.exists(path):
        try:
            import re
            head = open(path, "r", errors="replace").read(400_000)
            has_o2 = bool(re.search(r'id="R_EX_o2\(e\)"|id="R_EX_o2_e"', head))
        except OSError:
            pass
    return {"name": name or species_id.replace("_", " "),
            "file": species_id,
            "aerotolerance": "facultative" if has_o2 else "anaerobe",
            "card": ""}


def load_species(species_id: str, gem_dir: str = GEM_DIR,
                 card_tier: str = "literal", repair: bool | None = None,
                 meta: dict | None = None) -> Species:
    # A caller may supply meta for a strain that has no literal card. Falling
    # back to synth_meta keeps every existing call site unchanged while making
    # the roster reachable; card_tier="derived" then writes the card from the
    # GEM, so nothing hand-written is required.
    if meta is None:
        meta = LITERAL_CARDS.get(species_id) or synth_meta(species_id, gem_dir)
    path = os.path.join(gem_dir, meta["file"] + ".xml")
    gem = GEM(path, species_id=species_id, name=meta["name"])
    # Annotation repairs, in memory only -- see metabco/repair.py. Off unless
    # asked for, so the unrepaired reconstructions stay the default arm and
    # their incompleteness remains a reportable result rather than something
    # quietly patched away.
    from metabco import repair as _repair
    if repair or (repair is None and _repair.enabled()):
        _lvl = _repair.level()
        gem.repairs_applied = _repair.repair(
            gem, biomass=(repair == 2 or _lvl >= 2),
            gapfill=(repair == 3 or _lvl >= 3), species_id=species_id)
    spec = SpeciesSpec(
        species_id=species_id,
        name=meta["name"],
        gem_path=path,
        exchanges=tuple(sorted(gem.exchanges)),
        biomass_reaction=gem.biomass_reaction,
        aerotolerance=meta["aerotolerance"],
        card=build_card(gem, meta, card_tier),
    )
    return Species(species_id=species_id, spec=spec, gem=gem)


def load_roster(species_ids: list[str] | None = None, gem_dir: str = GEM_DIR,
                card_tier: str = "literal") -> dict[str, Species]:
    ids = species_ids or list(LITERAL_CARDS)
    return {sid: load_species(sid, gem_dir, card_tier) for sid in ids}


# --------------------------------------------------------------------- cards

# Products worth naming explicitly when describing a fermentative organism.
FERMENTATION_PRODUCTS = ["ac", "ppa", "but", "lac__L", "lac__D", "for", "etoh",
                         "succ", "h2", "co2", "ind3ac", "isocapr"]


def build_card(gem: GEM, meta: dict, tier: str) -> str:
    """Assemble a species card at the requested grounding tier."""
    if tier == "none":
        return ""
    if tier == "literal":
        return meta["card"]
    if tier == "behavioural":
        return derive_behavioural_card(gem, meta)
    derived = derive_card(gem, meta)
    if tier == "derived":
        return derived
    if tier == "full":
        transporters = ", ".join(sorted(gem.exchanges))
        return f"{derived}\n\nComplete list of exchangeable metabolites:\n{transporters}"
    raise ValueError(f"unknown card tier {tier!r}")


# Six conditions, fixed and identical for every organism, chosen the way a
# microbiologist would characterise an isolate: a simple sugar, a dietary
# polysaccharide, a host-derived glycan, a fermentation product, a rich medium,
# and the same medium with oxygen. Held constant across species so the card is a
# general-purpose description rather than something tuned per organism.
PROBE_CONDITIONS: list[tuple[str, dict[str, float] | None, bool]] = [
    ("glucose", {"glc__D": 10.0}, False),
    ("maltose", {"malt": 10.0}, False),
    ("N-acetylglucosamine, a host mucin sugar", {"acgam": 10.0}, False),
    ("L-lactate", {"lac__L": 10.0}, False),
    # The two probes below exist because of a measured failure. With only
    # sugar-fed conditions on the card, every line showed the organism
    # *releasing* short-chain fatty acids, and a model reading it concluded the
    # organism therefore never consumes them -- cross-feeding sensitivity
    # collapsed to 0.137. A card that samples only one metabolic regime is
    # actively misleading about every other one, so the panel now includes the
    # regime cross-feeding actually asks about: living on someone else's waste.
    ("acetate as the only added carbon source", {"ac": 10.0}, False),
    ("a spent medium holding other bacteria's fermentation products "
     "(acetate, lactate, formate, succinate) and no sugar at all",
     {"ac": 8.0, "lac__L": 5.0, "lac__D": 5.0, "for": 5.0, "succ": 5.0}, False),
    ("a complete gut diet", None, False),
    ("a complete gut diet with oxygen available", None, True),
]


def derive_behavioural_card(gem: GEM, meta: dict, diet_bounds: dict | None = None,
                            probe_uptake: float = 10.0) -> str:
    """Describe what the organism *does*, condition by condition.

    This replaces an earlier card that inventoried what the organism *could*
    exchange, and which measurably made predictions worse. The failure was
    specific and reproducible: given a list of secretable products the model
    asserted 100% of them as actually secreted, and given a short list of absent
    transporters it treated the list as exhaustive and denied everything
    unnamed. Both are the same error -- a static capability inventory being read
    as a prediction of behaviour.

    So nothing here enumerates capabilities. Each line is an observation of a
    solved flux distribution under a named condition, including conditions where
    the organism does nothing, because "tested and did not grow" is an
    observation whereas "cannot" is a claim about everything untested.
    """
    from .bench import ENERGY_SOURCES, MINERALS, base_bounds
    from .diet import load_diet

    reg = gem.registry()
    diet = diet_bounds if diet_bounds is not None else load_diet("western").bounds
    # Names that carry no information in a behaviour description: bulk salts,
    # water, protons, and AGORA's biomass pseudo-exchange.
    NOISE = MINERALS | {"biomass", "h2o", "h"}

    def name(met: str) -> str:
        m = reg.get(met)
        return (m.name or met).strip().lower() if m else met

    def probe(carbons: dict[str, float] | None, aerobic: bool):
        base = base_bounds(gem, reg, diet, anaerobic=not aerobic)
        if carbons is not None:
            # Other bulk energy sources are removed so the named substrates are
            # actually the ones under test. Without this the diet's own starch
            # dominates every probe and all conditions read identically.
            for met in ENERGY_SOURCES:
                if met not in carbons:
                    base.pop(met, None)
            for met, v in carbons.items():
                if met in gem.exchanges:
                    base[met] = v
        with gem.model:
            gem.set_medium(base)
            # Same total-uptake ceiling the simulator uses. Without it the
            # organism draws thousands of mmol of carbon from a rich medium,
            # growth is unphysiological, and there is no pressure to ferment --
            # which is why an earlier version had E. coli releasing nothing.
            gem.add_carbon_cap(60.0, reg)
            return gem.solve()

    lines = [f"{meta['name']}. Behaviour computed from its genome-scale "
             f"reconstruction under a standard panel of conditions."]

    for label, carbons, aerobic in PROBE_CONDITIONS:
        if carbons is not None and not any(m in gem.exchanges for m in carbons):
            lines.append(f"- On {label}: has no transporter for it, so it is not a "
                         f"usable substrate.")
            continue
        mu, fluxes = probe(carbons, aerobic)
        if mu is None or mu < 1e-4:
            lines.append(f"- On {label}: does not grow.")
            continue

        outs = sorted([(m, v) for m, v in fluxes.items()
                       if v > 0.1 and m not in NOISE],
                      key=lambda kv: -kv[1])[:4]
        rate = ("grows quickly" if mu > 0.4 else "grows steadily" if mu > 0.1
                else "grows slowly")
        bits = [f"- On {label}: {rate}"]
        if carbons is not None:
            taken = [m for m in carbons if -fluxes.get(m, 0.0) > 0.1]
            if len(carbons) == 1:
                only = next(iter(carbons))
                bits.append("consumes it" if taken else
                            f"but does not consume the {name(only)} itself")
            elif taken:
                bits.append("consumes " + ", ".join(name(m) for m in taken))
            else:
                bits.append("but consumes none of them")
        co2 = fluxes.get("co2", 0.0)
        if outs:
            bits.append("releases " + ", ".join(name(m) for m, _ in outs))
        elif co2 > 0.1:
            bits.append("releases only carbon dioxide")
        else:
            bits.append("releases nothing measurable")
        lines.append("; ".join(bits) + ".")

    # Oxygen response stated as an observation of two matched probes rather than
    # as a label, because the reconstructions disagree with the textbook for
    # several nanaerobes and the observation is what we can actually defend.
    mu_ana, _ = probe(None, False)
    mu_aer, _ = probe(None, True)
    if mu_ana > 1e-4 and mu_aer > mu_ana * 1.15:
        lines.append(f"- Oxygen: grows about {mu_aer / mu_ana:.1f} times faster with "
                     f"oxygen available than without it.")
    elif mu_ana > 1e-4:
        lines.append("- Oxygen: gains no growth advantage from oxygen; it ferments "
                     "either way.")
    return "\n".join(lines)


def derive_card(gem: GEM, meta: dict) -> str:
    """Project a genome-scale model into a few lines of text.

    Everything here is read off the model, never written by hand. That makes the
    tier scalable to the thousands of organisms that have a reconstruction but no
    review article -- and it is the honest test of whether a GEM survives being
    compressed into language.
    """
    reg = gem.registry()
    carbon_sources = []
    for met in gem.exchanges:
        m = reg.get(met)
        if m and m.carbon > 0 and m.known:
            carbon_sources.append(met)
    products = [p for p in FERMENTATION_PRODUCTS if p in gem.exchanges]
    aerobic = "o2" in gem.exchanges

    lines = [
        f"{meta['name']}, reconstructed from its genome: "
        f"{len(gem.model.reactions)} reactions, {len(gem.model.metabolites)} "
        f"metabolites, {len(gem.model.genes)} genes.",
        f"Oxygen tolerance: {meta['aerotolerance']}"
        + ("; an oxygen transporter is present." if aerobic
           else "; no oxygen transporter is encoded."),
        f"It can exchange {len(gem.exchanges)} metabolites with its environment, "
        f"of which {len(carbon_sources)} are carbon-bearing.",
    ]
    if products:
        names = [(reg.get(p).name if reg.get(p) else p) for p in products]
        lines.append("Fermentation products it is able to secrete: "
                     + ", ".join(f"{n} ({p})" for n, p in zip(names, products)) + ".")
    absent = [p for p in ("but", "ppa", "lac__L") if p not in gem.exchanges]
    if absent:
        lines.append("It has no transporter for, and therefore cannot exchange: "
                     + ", ".join(absent) + ".")
    comp = gem.biomass_composition()
    lines.append(
        f"Producing one gram of dry weight consumes about {comp.get('C', 40):.0f} mmol "
        f"carbon and {comp.get('N', 10):.0f} mmol nitrogen.")
    return "\n".join(lines)


def available_gems(gem_dir: str = GEM_DIR) -> list[str]:
    return sorted(Path(p).stem for p in glob.glob(os.path.join(gem_dir, "*.xml")))
