"""Bar storage: one interface, several backends.

See `registry.store_from_uri` for the URI forms, and `backtester/data/README.md`
for why Parquet is the default.
"""

from .base import BarStore, SeriesInfo
from .loader import load_bars, resample, validate_bars
from .registry import store_from_uri

__all__ = ["BarStore", "SeriesInfo", "load_bars", "resample", "store_from_uri", "validate_bars"]
