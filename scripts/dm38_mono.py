"""Drive all 24 monocultures in DM38, in both memory arms, from one process.

Monocultures are the cheapest thing Clark can referee and the most replicated:
24 of our species, 206 clean wells, median 7 replicates each, against 4 for the
one 11-member community. They also supply something the project does not have —
a single-organism denominator. No claim about community error compounding is
meaningful until we know the single-organism error.

Two arms, identical in every other respect:

    mem     the temporal layer is fed: each agent sees its own last four turns
    nomem   the temporal layer is empty, as every run in this project has been

That comparison is the reason to run both. It is also the cleanest place to ask
it: with one species in the flask there are no community effects to confound
memory with, so a difference between the arms is memory and nothing else.

Batched deliberately. Each monoculture is one agent per turn, so running them
one at a time would mean 288 sequential process launches per arm; this drives
all 48 runs from a single interpreter, loading the GEMs once.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.dm38_step as D
import scripts.stage2_step as S

ROOT = Path("runs/dm38/mono")

# One arm per model, not per memory setting. Memory is ON everywhere now; the
# open question moved from "does history help" to "does the answer depend on
# which model is reading the same sheet". Three models: the teacher, the
# fine-tuning target (SmileyLlama's exact base, so its recipe transfers), and a
# cross-family control so "is this a Llama artifact?" is answerable.
MODELS = {
    "llama70": "hf:meta-llama/Llama-3.3-70B-Instruct",
    "llama8":  "hf:meta-llama/Llama-3.1-8B-Instruct",
    "qwen72":  "hf:Qwen/Qwen2.5-72B-Instruct",
    # A frontier-lab small model, for the axis the three open ones cannot give:
    # the c11 run showed Claude-with-an-agent-loop is perfectly coherent where
    # the open models are not, but that confounded model with inference setup.
    # Haiku here runs the SAME single forward pass as the others, so the
    # comparison is model quality alone.
    "haiku45": "anthropic:claude-haiku-4-5-20251001",
    # Phase B. Same base weights as the llama8 arm plus our LoRA, so
    # llama8-vs-sft is base-vs-tuned with everything else identical -- the
    # only controlled test of whether training on FVA envelopes transfers to
    # Clark's flask. Runs in-process, so this arm needs a GPU node.
    "sft": "peft:" + str(Path(os.environ.get(
        "METABCO_SFT_ADAPTER",
        "/path/to/metabco/runs/sft_natural_noleak"))),
}
ARMS = tuple(MODELS)
CODES = [v[2] for v in S.ROSTER.values()]
ALL_SIDS = list(S.ROSTER)

# see dm38_step.REG_MEMBERS: the registry is the 24-species union even though
# only one species is ever in the tube.
D.REG_MEMBERS = ALL_SIDS


SYSTEM = ("You are the bacterial species named in the message, deciding its own "
          "metabolism for the next interval. Follow the instructions in the "
          "message exactly, including its request that you reason in prose "
          "before answering. End your reply with the JSON object it describes "
          "and nothing after it.")
FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def _balanced_spans(t: str):
    """Yield every brace-balanced span in the text, LAST first.

    The prompt asks for the JSON object as the last thing in the reply, so a
    long deliberation legitimately contains earlier braces -- in prose, in a
    worked example, in a discarded draft. Taking first-brace-to-last-brace
    swallows all of it and parses nothing, which is what silently failed five
    turn-0 answers whose JSON was in fact perfectly well formed.
    """
    opens = [i for i, ch in enumerate(t) if ch == "{"]
    for i in reversed(opens):
        depth = 0
        in_str = esc = False
        for j in range(i, len(t)):
            ch = t[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield t[i:j + 1]
                    break


def extract_json(text: str) -> dict | None:
    """Recover a JSON object from a reply that may be fenced or prefaced."""
    t = text.strip()
    for c in (t, *FENCE.findall(t), *_balanced_spans(t)):
        try:
            o = json.loads(c)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(o, dict) and ("fluxes" in o or "growth_rate" in o):
            return o
    return None


def answer_all(arms, spec: str | None, workers: int, temperature: float) -> None:
    """Fill in every pending agent answer for the current turn, over the API.

    This replaces the subagent step. The prompt sheet is sent verbatim -- it
    already carries its own persona, rules and output contract -- so answers
    collected here are comparable with the ones collected by subagents, and the
    only thing that changed is which model is reading them.

    Answers are written next to the prompt exactly as before, so `apply` cannot
    tell the difference. A `.meta.json` sidecar records model, provider,
    temperature and token usage, because "which model said this" is not
    recoverable from the flux set afterwards.
    """
    from metabco.llm import build_client

    jobs = []           # (prompt path, model spec for that arm)
    for arm, code, rd in each(arms):
        turn = json.loads((rd / "state.json").read_text())["turn"]
        if turn >= D.N_TURNS:
            continue
        td = rd / f"turn_{turn:02d}"
        for md in sorted(td.glob("*.md")):
            if not (td / f"{md.stem}.json").exists():
                jobs.append((md, spec or MODELS[arm]))
    if not jobs:
        print("nothing pending")
        return
    by_spec: dict[str, int] = {}
    for _, sp in jobs:
        by_spec[sp] = by_spec.get(sp, 0) + 1
    print(f"{len(jobs)} pending, {workers} workers: "
          + "; ".join(f"{n} x {sp.split('/')[-1]}" for sp, n in by_spec.items()))

    # METABCO_CACHE_DIR exists for replicates. The disk cache keys on
    # (system, user, max_tokens, temperature) and NOT on any seed, so re-running
    # an arm at the same temperature replays the cached answer rather than
    # drawing a new sample, and a "replicate" sweep silently produces identical
    # numbers. Point each replicate at its own directory.
    cache_dir = os.environ.get("METABCO_CACHE_DIR", ".cache/llm")
    clients = {sp: build_client(sp, cache_dir=cache_dir) for sp in by_spec}

    def one(job) -> tuple[str, bool, int]:
        md, sp = job
        try:
            r = clients[sp].complete(SYSTEM, md.read_text(), max_tokens=2500,
                                     temperature=temperature)
        except Exception:
            return md.stem, False, 0
        # Keep the deliberation. It is 500-1,200 tokens of the model's actual
        # reasoning, it is what the trace interface renders, and it cannot be
        # recovered after the fact -- the parsed flux set does not contain it.
        (md.parent / f"{md.stem}.reasoning.txt").write_text(r.text)
        data = extract_json(r.text)
        if data is None:
            return md.stem, False, 0
        (md.parent / f"{md.stem}.json").write_text(json.dumps(data, indent=1))
        (md.parent / f"{md.stem}.meta.json").write_text(json.dumps(
            {"model": r.model, "spec": sp, "temperature": temperature,
             "cached": bool(getattr(r, "cached", False)),
             "usage": r.usage, "latency_ms": getattr(r, "latency_ms", None)}, indent=1))
        return md.stem, True, int(r.usage.get("total_tokens", 0) or 0)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(one, jobs))
    ok = sum(1 for _, k, _ in res if k)
    tok = sum(t for _, _, t in res)
    print(f"  {ok}/{len(jobs)} answered, {tok} tokens")
    for sid, k, _ in res:
        if not k:
            print(f"  FAILED {sid} (reasoning kept, JSON unparseable)")


def run_dir(arm: str, code: str) -> Path:
    return ROOT / arm / code


def each(arms: tuple[str, ...] = ARMS):
    for arm in arms:
        for code in CODES:
            yield arm, code, run_dir(arm, code)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["init", "render", "answer", "apply",
                                     "status", "sweep"])
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--model", default=None,
                    help="override every arm's model; normally omit")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--temperature", type=float, default=0.7)
    args = ap.parse_args()
    arms = tuple(a.strip() for a in args.arms.split(",") if a.strip())

    if args.mode == "init":
        for arm, code, rd in each(arms):
            rd.mkdir(parents=True, exist_ok=True)
            D.RUN = rd
            members = D.community_from_codes(code)
            S.save_state(D.fresh_world(members), rd / "state.json")
            (rd / "community.json").write_text(json.dumps(
                {"treatment": code, "members": members, "codes": [code],
                 "dt_h": D.DT_H, "n_turns": D.N_TURNS, "medium": "DM38",
                 "memory": True, "model": MODELS.get(arm, arm),
                 "od_start": D.OD_START, "gdw_per_od": D.GDW_PER_OD}, indent=1))
        print(f"initialised {len(arms) * len(CODES)} monoculture runs under {ROOT}")
        print(f"  arms: {', '.join(arms)}   species: {len(CODES)}")
        return

    if args.mode == "answer":
        answer_all(arms, args.model, args.workers, args.temperature)
        return

    if args.mode == "sweep":
        # render -> answer -> apply, until every run reaches N_TURNS. The whole
        # experiment in one process; no per-turn orchestration.
        for _ in range(D.N_TURNS + 1):
            turns = [json.loads((run_dir(a, c) / "state.json").read_text())["turn"]
                     for a in arms for c in CODES]
            if min(turns) >= D.N_TURNS:
                break
            for mode in ("render", "answer", "apply"):
                _run(mode, arms, args)
        print("sweep complete")
        return

    if args.mode == "status":
        for arm in arms:
            turns = [json.loads((run_dir(arm, c) / "state.json").read_text())["turn"]
                     for c in CODES]
            pend = sum(1 for arm2, c, rd in each((arm,))
                       if not (rd / f"turn_{json.loads((rd/'state.json').read_text())['turn']:02d}"
                               / f"{D.community_from_codes(c)[0]}.json").exists())
            print(f"  {arm:6s} turns {min(turns)}-{max(turns)}   {pend}/{len(CODES)} awaiting an answer")
        return

    _run(args.mode, arms, args)


def _run(mode: str, arms, args) -> None:
    if mode == "answer":
        answer_all(arms, args.model, args.workers, args.temperature)
        return
    done, skipped = 0, 0
    for arm, code, rd in each(arms):
        D.RUN = rd
        D.MEMORY = True
        members = D.community_from_codes(code)
        turn = json.loads((rd / "state.json").read_text())["turn"]
        if turn >= D.N_TURNS:
            skipped += 1
            continue
        turn_dir = rd / f"turn_{turn:02d}"
        # Skip only if nothing was ASKED this turn. If the prompt exists and
        # the answer does not, that is a recorded non-answer (see
        # dm38_step.apply) and the run must still advance -- otherwise one
        # model's failure silently freezes one species at turn 0 while its 71
        # siblings run to 48 h.
        if mode == "apply" and not (turn_dir / f"{members[0]}.md").exists():
            skipped += 1
            continue
        # dm38_step prints per-run; monocultures are 48 runs, so stay quiet and
        # summarise instead.
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            (D.render if mode == "render" else D.apply)(members, turn_dir)
        done += 1
    print(f"{mode}: {done} runs advanced, {skipped} skipped")

    if mode == "apply":
        for arm in arms:
            tot = 0.0
            clean = n = 0
            for _, code, rd in each((arm,)):
                t = json.loads((rd / "state.json").read_text())["turn"]
                st = json.loads((rd / "state.json").read_text())
                tot += sum(st["patches"]["flask"]["biomass"].values())
                v = rd / f"turn_{t-1:02d}" / "validation.json"
                if v.exists():
                    for r in json.loads(v.read_text()):
                        n += 1
                        clean += bool(r["ok"])
            print(f"  {arm:6s} total biomass across 24 monocultures {tot:.3f} gDW/L"
                  f"   clean {clean}/{n}")


if __name__ == "__main__":
    main()
