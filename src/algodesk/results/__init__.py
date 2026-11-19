"""Results package for session artifacts, metrics calculation, and reporting."""

from __future__ import annotations

from .compare import compare_runs
from .index import ResultsIndex
from .roundtrips import RoundTrip, compute_round_trip_metrics
from .screen_writer import write_screen_results
from .writer import ResultsWriter

__all__ = [
    "ResultsIndex",
    "ResultsWriter",
    "RoundTrip",
    "compare_runs",
    "compute_round_trip_metrics",
    "write_screen_results",
]
