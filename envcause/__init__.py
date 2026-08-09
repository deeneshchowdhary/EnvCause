"""EnvCause: delta-debug environment configuration failures."""

from .adapters import DockerAdapter, KubernetesAdapter
from .core import EnvCauseError, EnvChange, ReductionResult, diff_envs, parse_dotenv, reduce_environment

__all__ = [
    "EnvCauseError",
    "EnvChange",
    "DockerAdapter",
    "KubernetesAdapter",
    "ReductionResult",
    "diff_envs",
    "parse_dotenv",
    "reduce_environment",
]

__version__ = "0.2.0"
