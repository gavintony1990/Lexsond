"""Versioned, deterministic text evaluation domain.

Evaluation accuracy is intentionally separate from the native probe dimension
scores.  This package owns untrusted dataset compilation, deterministic
sampling/scoring, and safe aggregate facts; transports remain in ``probe.py``.
"""

from .compiler import CompiledDataset, DatasetValidationError, EvaluationItem
from .scorers import ScoreResult, ScoreStatus, get_scorer, list_scorers

__all__ = [
    "CompiledDataset",
    "DatasetValidationError",
    "EvaluationItem",
    "ScoreResult",
    "ScoreStatus",
    "get_scorer",
    "list_scorers",
]
