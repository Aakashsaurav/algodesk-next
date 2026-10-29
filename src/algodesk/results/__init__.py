"""Results package for session artifacts, metrics calculation, and reporting."""

from __future__ import annotations

from .compare import compare_runs
from .writer import ResultsWriter

__all__ = ["ResultsWriter", "compare_runs"]
