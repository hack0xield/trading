"""Chart analysis: swing detection and margin-zone envelopes.

Kept apart from `core/` because none of it is part of the execution engine — a
strategy may use it, a plot may use it, neither is required to.
"""

from .envelopes import Envelope, build_envelopes
from .zigzag import Pivot, Provisional, legs, pivots_known_by, provisional, swing_sizes, zigzag

__all__ = [
    "Envelope", "Pivot", "Provisional", "build_envelopes", "legs", "pivots_known_by", "provisional",
    "swing_sizes", "zigzag",
]
