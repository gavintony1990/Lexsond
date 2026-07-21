"""Core package for the Lexsond AI API quality platform."""

from .models import (
    Dimension,
    DimensionScore,
    NormalizedRunResult,
    ProbeCaseResult,
    RequestMeasurement,
    RunStatus,
)
from .probe import OpenAIChatProbe, ProbeConfig
from .scoring import ScoringPolicy, score_run
from .suite import (
    ProbeSuite,
    SuiteExecutionError,
    SuiteValidationError,
    compile_suite,
    run_suite,
)

__all__ = [
    "Dimension",
    "DimensionScore",
    "NormalizedRunResult",
    "OpenAIChatProbe",
    "ProbeConfig",
    "RequestMeasurement",
    "ScoringPolicy",
    "ProbeSuite",
    "ProbeCaseResult",
    "RunStatus",
    "SuiteExecutionError",
    "SuiteValidationError",
    "compile_suite",
    "run_suite",
    "score_run",
]
