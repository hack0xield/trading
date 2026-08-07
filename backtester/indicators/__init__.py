"""Generic indicators — strategy-agnostic by construction.

Anything here must be usable by a strategy that knows nothing about the
others. That is the test for belonging: if a module needs a particular
strategy's data model, it belongs in that strategy's package instead.

`backtester/analysis/` used to hold ZigZag alongside margin-zone envelopes,
which meant importing the indicator dragged in CME margin machinery — 23
modules for a function that turns bars into pivots.
"""

from .zigzag import Pivot, Provisional, legs, pivots_known_by, provisional, swing_sizes, zigzag

__all__ = [
    "Pivot", "Provisional", "legs", "pivots_known_by", "provisional",
    "swing_sizes", "zigzag",
]
