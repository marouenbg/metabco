"""Score the monoculture arm: three models and a dFBA twin against Clark.

Monocultures are the single-organism denominator the project has been missing.
No claim about community error compounding means anything until we know what
the error is with one organism in the tube and nothing to interact with.

Clark ran every one of our 24 species alone, 3-22 replicate wells each, so the
reference here has a measurable spread as well as a mean -- which is why
calibration is reported alongside accuracy. A prediction that lands inside the
replicate range is right in a way that a prediction merely close to the mean is
not.

Five quantities, in decreasing robustness:

    butyrate call   who makes any butyrate at all. Binary, 6 of 24 positive,
                    survives every calibration question.
    growth rank     Spearman rho over 24 species. Does the model know who wins
                    in THIS medium? Rank-only, so units cancel.
    growth fold     OD_final/OD_inoculum against gDW_final/gDW_inoculum. Both
                    sides divided by their own inoculum, so the gDW-per-OD
                    conversion -- the one number nobody measured -- cancels.
    metabolites     the four Clark ran standards for, log10 fold error.
    calibration     fraction of predictions inside Clark's own replicate range.

Propionate and formate are predicted by every arm and measured by none, so they
are reported as unrefereed rather than scored.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.dm38_step as D
import scripts.stage2_step as S

ROOT = Path("runs/dm38/mono")
SCFA = {"Butyrate": "but", "Acetate": "ac", "Lactate": "lac__L",
        "Succinate": "succ"}
BUT_THRESHOLD = 1.0        # mM above which a species is called a butyrate producer


def log_err(pred: float, obs: float, floor: float = 0.1) -> float:
    return abs(math.log10(max(pred, floor) / max(obs, floor)))


def spearman(a: list[float], b: list[float]) -> float:
    """Rank correlation, ties averaged. Written out to avoid a scipy import."""
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    ra, rb = ranks(a), ranks(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    da = math.sqrt(sum((x - ma) ** 2 for x in ra))
    db = math.sqrt(sum((y - mb) ** 2 for y in rb))
    return num / (da * db) if da and db else 0.0


def clark_truth() -> dict[str, dict]:
    df = pd.read_csv("data/experimental/Clark2021_MasterDF.csv", low_memory=False)
    df = df[df["Contamination?"].eq("No")]
    out = {}
    for sid, meta in S.ROSTER.items():
        code = meta[2]
        g = df[df["Treatment"].eq(code)]
        if not len(g):
            continue
        out[code] = {
            "species": meta[3], "n": int(len(g)),
            "od": float(g["OD"].mean()),
            "od_sd": float(g["OD"].std(ddof=1)) if len(g) > 1 else 0.0,
            "mets": {m: float(g[c].mean()) for c, m in SCFA.items() if c in g.columns},
            "mets_sd": {m: (float(g[c].std(ddof=1)) if len(g) > 1 else 0.0)
                        for c, m in SCFA.items() if c in g.columns},
        }
    return out


def llm_endpoint(model: str, code: str) -> dict | None:
    st = ROOT / model / code / "state.json"
    if not st.exists():
        return None
    d = json.loads(st.read_text())
    p = d["patches"]["flask"]
    bm = sum(float(v) for v in p["biomass"].values())
    mets = {m: float(v) for m, v in p["concentrations"].items()}
    mets["lac__L"] = mets.get("lac__L", 0.0) + mets.get("lac__D", 0.0)
    logs = [json.loads(f.read_text())
            for f in sorted((ROOT / model / code).glob("turn_*/validation.json"))]
    recs = [r for l in logs for r in l]
    return {"turn": d["turn"], "biomass": bm, "mets": mets,
            "n_turns": len(logs),
            "clean": sum(1 for r in recs if r["ok"]),
            "n": len(recs),
            "no_answer": sum(1 for r in recs if r.get("no_answer"))}


def fba_endpoint(twin: Path) -> dict[str, dict]:
    if not twin.exists():
        return {}
    d = json.loads(twin.read_text())
    out = {}
    for c in d["conditions"]:
        if c["n_members"] != 1:
            continue
        m = dict(c["metabolites_mM"])
        m["lac__L"] = m.get("lac__L", 0.0) + m.get("lac__D", 0.0)
        out[c["label"]] = {"biomass": c["total_biomass_gdw_L"], "mets": m}
    return out


def score(name: str, pred: dict[str, dict], truth: dict[str, dict]) -> dict:
    """pred: code -> {biomass, mets}. Returns the metric bundle for one arm."""
    x0 = D.OD_START * D.GDW_PER_OD          # monoculture: whole inoculum, one species
    codes = [c for c in truth if c in pred]
    rows, errs = [], {m: [] for m in SCFA.values()}
    inside = tot_checked = 0
    fold_err, pred_growth, obs_growth = [], [], []
    tp = fp = fn = tn = 0

    for c in codes:
        t, p = truth[c], pred[c]
        fo = t["od"] / D.OD_START
        fp_ = p["biomass"] / x0
        fold_err.append(log_err(fp_, fo, floor=1.0))
        pred_growth.append(p["biomass"])
        obs_growth.append(t["od"])

        pm = {m: p["mets"].get(m, 0.0) for m in t["mets"]}
        for m, ov in t["mets"].items():
            errs[m].append(log_err(pm[m], ov))
            sd = t["mets_sd"].get(m, 0.0)
            if sd > 0:
                tot_checked += 1
                inside += abs(pm[m] - ov) <= sd
        obs_but = t["mets"].get("but", 0.0) >= BUT_THRESHOLD
        pr_but = pm.get("but", 0.0) >= BUT_THRESHOLD
        tp += obs_but and pr_but; fp += pr_but and not obs_but
        fn += obs_but and not pr_but; tn += not obs_but and not pr_but
        rows.append((c, fo, fp_, t["mets"].get("but", 0.0), pm.get("but", 0.0)))

    sens = tp / (tp + fn) if (tp + fn) else 0.0
    spec = tn / (tn + fp) if (tn + fp) else 0.0
    return {
        "arm": name, "n_species": len(codes),
        "mean_metabolite_log10_error": round(
            sum(sum(v) for v in errs.values()) / max(sum(len(v) for v in errs.values()), 1), 4),
        "per_metabolite": {m: round(sum(v) / len(v), 3) for m, v in errs.items() if v},
        "mean_growth_fold_log10_error": round(sum(fold_err) / max(len(fold_err), 1), 4),
        "growth_spearman": round(spearman(pred_growth, obs_growth), 4),
        "butyrate_call": {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
                          "balanced_accuracy": round((sens + spec) / 2, 4)},
        "inside_replicate_sd": round(inside / tot_checked, 4) if tot_checked else None,
        "rows": rows,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    # Defaults to the REPAIRED twin. The as-shipped reconstructions grow only
    # 10 of 24 species in Clark's own medium, so scoring against them flatters
    # every other arm; that is a statement about AGORA's annotations, not a
    # baseline. Pass the unrepaired file explicitly to reproduce the old number.
    ap.add_argument("--twin", default="runs/dm38/mono/fba_twin_mono.json")
    # The SFT arm runs through the Brain path, not the render/answer/apply
    # path, because it is queried in the format it was TRAINED on rather than
    # the harness's rendered prompt -- SmileyLlama's invariant, and the
    # difference between 24/25 growing and near-total abstention. That emits a
    # twin-shaped file, so it is read the same way the twin is.
    ap.add_argument("--sft-arm", default="runs/dm38/mono/sft_arm.json",
                    help="twin-format json from dm38_sft_arm.py; skipped if absent")
    args = ap.parse_args()

    truth = clark_truth()
    twin_meta = {}
    tp = Path(args.twin)
    if tp.exists():
        _d = json.loads(tp.read_text())
        twin_meta = {"repair_level": _d.get("repair_level", "?"),
                     "supplemented": _d.get("supplemented", False)}
    # Only the declared model arms. Superseded runs are left on disk rather
    # than deleted, so the scorer must not sweep them up as if they were arms.
    from scripts.dm38_mono import MODELS
    models = [m for m in MODELS if (ROOT / m / "AC" / "state.json").exists()]

    print("=" * 96)
    print("DM38 MONOCULTURES — three models and a dFBA twin against Clark 2021")
    print(f"{len(truth)} species, each alone, 48 h batch; "
          f"{sum(t['n'] for t in truth.values())} clean replicate wells")
    print(f"dFBA twin: {tp.name}  repair level {twin_meta.get('repair_level','?')}"
          + ("  (+ per-species supplement for EL, BA — those two are NOT in "
             "Clark's medium)" if twin_meta.get("supplemented") else ""))
    print("=" * 96)

    arms = []
    for m in models:
        pred = {c: e for c in truth if (e := llm_endpoint(m, c))}
        done = [c for c, e in pred.items() if e["turn"] >= D.N_TURNS]
        if len(done) < len(pred):
            print(f"\n! {m}: only {len(done)}/{len(pred)} runs have reached "
                  f"turn {D.N_TURNS} — scores below are provisional")
        arms.append(score(m, pred, truth))
        st = [llm_endpoint(m, c) for c in pred]
        na = sum(s["no_answer"] for s in st)
        clean = sum(s["clean"] for s in st); n = sum(s["n"] for s in st)
        arms[-1]["harness"] = {"clean": clean, "n": n, "no_answer": na}

    sp = Path(args.sft_arm)
    sft = fba_endpoint(sp)
    if sft:
        arms.append(score("sft(own fmt)", sft, truth))
        # Brain-path arms record their own validation; surface it in the same
        # columns so the row is comparable rather than blank.
        _d = json.loads(sp.read_text())
        _h = {"clean": 0, "n": 0, "no_answer": 0}
        for _c in _d["conditions"]:
            if _c["n_members"] == 1 and _c.get("harness"):
                for k in _h:
                    _h[k] += _c["harness"].get(k, 0)
        if _h["n"]:
            arms[-1]["harness"] = _h

    fba = fba_endpoint(tp)
    if fba:
        arms.append(score("dFBA twin", fba, truth))

    hdr = f"{'metric':38s}" + "".join(f"{a['arm']:>14s}" for a in arms)
    print("\n" + hdr); print("-" * len(hdr))

    def row(label, fn, fmt="{:14.3f}"):
        print(f"{label:38s}" + "".join(
            (fmt.format(v) if (v := fn(a)) is not None else f"{'--':>14s}") for a in arms))

    row("butyrate call, balanced accuracy", lambda a: a["butyrate_call"]["balanced_accuracy"])
    row("growth rank, Spearman rho", lambda a: a["growth_spearman"])
    row("growth fold, mean log10 error", lambda a: a["mean_growth_fold_log10_error"])
    row("metabolites, mean log10 error", lambda a: a["mean_metabolite_log10_error"])
    for m in SCFA.values():
        row(f"    {m}", lambda a, m=m: a["per_metabolite"].get(m))
    row("inside Clark's replicate sd", lambda a: a["inside_replicate_sd"])
    row("species scored", lambda a: a["n_species"], "{:14d}")
    print()
    for a in arms:
        h = a.get("harness")
        if h:
            print(f"  {a['arm']:12s} mass balance {h['clean']}/{h['n']} clean"
                  + (f", {h['no_answer']} turns with no usable answer" if h["no_answer"] else ""))

    out = ROOT / "results_mono.json"
    out.write_text(json.dumps(
        {"truth": truth, "arms": [{k: v for k, v in a.items() if k != "rows"}
                                  for a in arms]}, indent=1))
    print(f"\nwritten -> {out}")


if __name__ == "__main__":
    main()
