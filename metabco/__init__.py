"""metabco: bacterial community simulation where each species is an LLM agent.

The organising idea: dynamic FBA with the linear program replaced by a plug. The
environment is the agent's context window; the model weights are its genome.
"""

from .chem import Metabolite, MetaboliteRegistry
from .env import Medium, Patch, World
from .oracle import FBABrain, GEM, Kinetics, ex_to_met
from .sim import Placement, SimConfig, Simulator, SimResult, make_validator
from .trace import Tracer, load_run, latest_run
from .types import (
    Brain,
    FluxProposal,
    HostContext,
    Neighbor,
    Observation,
    Physicochem,
    SpeciesSpec,
    ValidationResult,
)
from .validate import ConservationValidator, ValidatorConfig

__version__ = "0.1.0"

__all__ = [
    "Metabolite", "MetaboliteRegistry", "Medium", "Patch", "World",
    "FBABrain", "GEM", "Kinetics", "ex_to_met",
    "Placement", "SimConfig", "Simulator", "SimResult", "make_validator",
    "Tracer", "load_run", "latest_run",
    "Brain", "FluxProposal", "HostContext", "Neighbor", "Observation",
    "Physicochem", "SpeciesSpec", "ValidationResult",
    "ConservationValidator", "ValidatorConfig",
]
