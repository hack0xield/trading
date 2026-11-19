"""Generic indicators — strategy-agnostic by construction.

Anything here must be usable by a strategy that knows nothing about the others.
That is the test for belonging: a module needing a particular strategy's data
model belongs in that strategy's package instead.
"""

from .zigzag import (
    Candidate, Pivot, Provisional, ZigZagTracker, legs, pivots_known_by, provisional,
    swing_sizes, zigzag,
)

__all__ = [
    "Candidate", "Pivot", "Provisional", "ZigZagTracker", "legs", "pivots_known_by",
    "provisional", "swing_sizes", "zigzag",
]
