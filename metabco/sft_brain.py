"""A Brain that speaks the corpus format the SFT model was actually trained on.

SmileyLlama's invariant is that training and inference use the SAME prompt --
constraints in, SMILES out, both times. They never bridge formats. We broke that
invariant by training on a terse medium listing and then querying with the
harness's rendered environment, and the model abstained: no usable answer on 75
of 300 turns, empty fluxes on 57 of 64, zero butyrate for 23 of 25 species.

Asked its own question about the SAME world states, the same model abstained on
NONE of 225 turns and returned a median of 28 signed decisions. The abstention
was the format, not the model.

So this Brain restores the invariant. Per turn it renders the corpus prompt from
the live world state -- the medium listing IS the concentrations -- reads back
`will_do`, and decodes the bands to signed fluxes. Everything downstream (the
validator, the world, the scorer) is untouched, so the arm stays comparable with
the prompted ones.

What is deliberately NOT done here: no prose, no deliberation, no rationale.
The corpus has none, so asking for it would reintroduce the mismatch this class
exists to remove. That is a real cost -- the per-round reasoning an interface
would want does not exist in this arm -- and it is SmileyLlama's cost too.
"""

from __future__ import annotations

import json

from .distill import decode_flux_band, decode_growth_band
from .types import FluxProposal, Observation

# Declared "present in excess, never limiting" in the corpus prompt, exactly as in
# gen_fva_corpus.py. Kept byte-identical: prompt structure is what the model keys
# on, and SmileyLlama found structure matters enormously where wording does not.
# The smallest value anywhere in the training prompts is 1.45e-4 and none is
# below 1e-6, so anything under this is a number the model has never seen.
TRAIN_FLOOR = 1.45e-4

BULK = {"h2o", "h", "na1", "cl", "k", "ca2", "mg2", "fe2", "fe3", "zn2", "cu2",
        "mn2", "cobalt2", "ni2", "mobd", "slnt", "tungs", "sel", "so4", "pi"}


def corpus_prompt(name: str, avail: dict[str, float], medium: str = "DM38",
                  bulk_extra: set[str] | frozenset[str] = frozenset(),
                  dropped: str | None = None,
                  as_bounds: tuple[float, float] | None = None,
                  floor: float = 0.0) -> str:
    """Rebuild the training prompt from a live world state.

    Mirrors gen_fva_corpus.prompt_for(). Every keyword defaults to the behaviour
    this function had before they existed, so nothing already run changes unless
    a caller opts in.

    medium      the header label. Training took this as a PARAMETER over three
                diets; this used to be the literal "DM38", which silently
                mislabelled every other medium.
    bulk_extra  metabolites to move from the numbered list onto the "present and
                not limiting" line. Pass the world's UNLIMITED set: those report a
                sentinel concentration of 1000, and cbl1 -- the one member not in
                BULK -- was rendering as `- cbl1: 1e+03`, the largest number in
                the prompt, where training always had 1.
    dropped     emit ", with X REMOVED". 98.4% of training rows carry this clause
                and inference never has; pass it only when something really was
                removed, since the alternative is lying to the model.
    NOTE ON UNITS. `avail` must be UPTAKE BOUNDS in mmol/gDW/h, which is what the
    header promises and what the corpus contains -- the generator renders each
    diet flux, i.e. the exchange lower bound it set. Passing flask CONCENTRATIONS
    in mM instead is the reason the diauxie probe produced byte-identical prompts
    at oxygen caps of 6 and 10: the cap lives in the kinetics, the concentration
    was held at 20 either way, and the manipulated variable never reached the
    model at all. Use SFTBrain(bounds_fn=...).

    floor       concentrations below this are moved to the "also present in
                trace amounts" line instead of being printed as a number.
                Measured over 194,500 training values the minimum is 1.45e-4 and
                NOTHING falls below 1e-6; but a live flask accumulates its own
                secretions, so from turn 1 the prompt carried entries like
                `- etoh: 2.69e-09`, three orders of magnitude off-distribution.
                The fine-tuned model answers a full envelope on turn 0 and
                returns `no_growth` on turn 1 when those appear. Use
                TRAIN_FLOOR.
    as_bounds   (biomass_gdw_per_l, dt_h). The header says mmol/gDW/h but the
                values passed are mM concentrations -- different quantities. Given
                this, each is converted to the uptake bound C/(X*dt) the validator
                would enforce, which is what the header actually promises.
    """
    named = {m: v for m, v in avail.items()
             if m not in BULK and m not in bulk_extra and v > 0}
    below = sorted(m for m, v in named.items() if v < floor)
    named = {m: v for m, v in named.items() if v >= floor}
    if as_bounds is not None:
        X, dt = as_bounds
        denom = max(X * dt, 1e-9)
        named = {m: v / denom for m, v in named.items()}
    items = sorted(named.items(), key=lambda kv: -kv[1])[:50]
    header = f"## Medium: {medium}" + (f", with {dropped} REMOVED" if dropped else "")
    lines = [f"You are {name.replace('_', ' ')}, a single bacterial species.", "",
             header, "Available for uptake (mmol/gDW/h):"]
    lines += [f"- {m}: {v:.3g}" for m, v in items]
    rest = sorted(named, key=lambda k: -named[k])[50:] + below
    if rest:
        lines.append(f"- also present in trace amounts: {', '.join(rest)}")
    bulk = sorted((set(avail) & BULK) | (set(avail) & set(bulk_extra)))
    if bulk:
        lines.append(f"Bulk salts and water, present and not limiting: {', '.join(bulk)}.")
    lines += ["", "## Task",
              "State your metabolic ENVELOPE and your CHOICE within it.",
              "`can_consume` / `can_secrete`: every exchange you could run, with "
              "the magnitude band you could reach.",
              "`cannot_use`: in the medium, but you cannot take it up.",
              "`will_do`: the fluxes you actually run, signed.",
              "Bands: none, micro, trace, low, moderate, high, very_high. "
              "Growth: no_growth, very_slow, slow, moderate, fast, very_fast.",
              "Reply with one JSON object."]
    return "\n".join(lines)


def last_object(text: str) -> dict | None:
    """Last balanced JSON object, the harness's own convention."""
    depth, end = 0, -1
    for i in range(len(text) - 1, -1, -1):
        if text[i] == "}":
            if depth == 0:
                end = i
            depth += 1
        elif text[i] == "{":
            depth -= 1
            if depth == 0 and end >= 0:
                try:
                    return json.loads(text[i:end + 1])
                except json.JSONDecodeError:
                    depth, end = 0, -1
    return None


class SFTBrain:
    """Query the fine-tuned model in its own format; return a FluxProposal."""

    kind = "sft"

    def __init__(self, client, species_id: str, name: str | None = None,
                 max_tokens: int = 1400, temperature: float = 0.0,
                 medium_name: str = "DM38",
                 unlimited: set[str] | frozenset[str] = frozenset(),
                 as_bounds: bool = False, floor: float = 0.0,
                 bounds_fn=None):
        self.client = client
        self.species_id = species_id
        self.name = name or species_id
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.medium_name = medium_name
        self.unlimited = frozenset(unlimited)
        self.as_bounds = as_bounds
        self.floor = floor
        # callable(obs) -> {met: uptake bound mmol/gDW/h}. When given, the prompt
        # reports what the organism may actually take up rather than what is in
        # the flask, so a capacity constraint is visible to the agent instead of
        # being enforced only on the solver.
        self.bounds_fn = bounds_fn
        self.n_calls = 0
        self.n_unparsed = 0
        self.last_envelope: dict | None = None      # kept for the violation audit

    def step(self, obs: Observation) -> FluxProposal:
        avail = (self.bounds_fn(obs) if self.bounds_fn is not None
                 else {m: c for m, c in obs.concentrations.items() if c > 0})
        avail = {m: v for m, v in avail.items() if v > 0}
        reply = self.client.complete(
            "",
            corpus_prompt(self.name, avail, medium=self.medium_name,
                          bulk_extra=self.unlimited,
                          as_bounds=(obs.biomass_gdw_per_l, obs.dt_h)
                          if self.as_bounds else None,
                          floor=self.floor),
                                     max_tokens=self.max_tokens,
                                     temperature=self.temperature)
        self.n_calls += 1
        env = last_object(reply.text)
        self.last_envelope = env
        if not env:
            self.n_unparsed += 1
            return FluxProposal(species_id=self.species_id, turn=obs.turn,
                                fluxes={}, growth_rate=0.0,
                                rationale="no parseable object",
                                meta={"unparsed": True})

        fluxes: dict[str, float] = {}
        for met, label in (env.get("will_do") or {}).items():
            if not isinstance(label, str):
                continue
            v = decode_flux_band(label)
            # decode_flux_band returns 0.0 for an unrecognised label; keeping it
            # would silently assert "no flux" where the model said something we
            # failed to read, which is the kind of quiet coercion that made the
            # last arm look inert.
            if v:
                fluxes[met] = v
        mu = decode_growth_band(env.get("growth") or "no_growth")
        return FluxProposal(
            species_id=self.species_id, turn=obs.turn, fluxes=fluxes,
            growth_rate=mu,
            rationale=(f"envelope: {len(env.get('can_consume') or {})} consumable, "
                       f"{len(env.get('cannot_use') or [])} unusable; "
                       f"growth {env.get('growth')}"),
            meta={"envelope": env},
        )
