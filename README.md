# metabco

Single-species language-model agents of gut bacteria.

One prompted language model stands for one bacterial species grown alone in the
defined medium DM38. Over 12 turns of 4 h, the agent receives the composition of
the medium and proposes uptake fluxes, secretion fluxes and a growth rate. A
validator checks the proposal for mass balance, and the medium is updated. The 25
species and the measurements used for comparison are those of Clark et al.,
Nat. Commun. 12, 3254 (2021).

## Contents

| Path | Content |
|---|---|
| `metabco/` | simulator, validator, prompt rendering, model backends |
| `scripts/dm38_mono.py` | runs the prompted agents (`init`, `render`, `answer`, `apply`, `status`, `sweep`) |
| `scripts/dm38_twin.py` | dynamic flux balance analysis of the same cultures |
| `scripts/score_mono.py` | metrics against the measurements |
| `runs/dm38/mono/<model>/<species>/turn_XX/` | prompt (`.md`), answer (`.json`), model text (`.reasoning.txt`) and validation log of every turn |
| `runs/dm38/mono/fba_twin_mono.json` | dynamic flux balance analysis result |
| `data/media/dm38.json` | the medium |

Model directories: `llama70` is Llama-3.3-70B-Instruct, `llama8` is
Llama-3.1-8B-Instruct, `qwen72` is Qwen2.5-72B-Instruct and `haiku45` is Claude
Haiku 4.5.

## Requirements

Python 3.12, cobra, pandas and Gurobi. Two inputs are not included:

- the AGORA2 v2.01 reconstructions, expected in `data/gems/agora2_v201`
  (then run `python scripts/fix_agora2_encoding.py`)
- `Clark2021_MasterDF.csv`, the measurements of Clark et al., expected in
  `data/experimental/`

## Usage

```bash
export HF_TOKEN=...             # Hugging Face inference API
export ANTHROPIC_API_KEY=...    # only for haiku45
python scripts/dm38_mono.py init --arms llama70
python scripts/dm38_mono.py sweep --arms llama70
python scripts/score_mono.py
```

`METABCO_REPAIR_GEMS` sets the repair level of the reconstructions (see
`metabco/repair.py`).
