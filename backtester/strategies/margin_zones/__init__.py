"""Margin zones: contract margin turned into price levels, and what watches them.

`margins.py` turns a CME maintenance margin into FMZ/IMZ distances, `zones.py`
anchors those on the ZigZag candidate and versions them as it moves,
`rollover.py` samples the daily CFD break, `crossing.py` pairs those samples
against a zone's E50 level, and `report.py` draws the result.

See `impl-spec-old/MarginZones_revised.md` for the specification this implements.
"""

from .crossing import DEFAULT_MAX_GAP_DAYS, Crossing, CrossingTracker
from .margins import (
    CONTRACT_DIR,
    DEFAULT_INITIAL_RATIO,
    MARGIN_LOG,
    ContractSpec,
    MarginLog,
    MarginObservation,
    MarginZones,
    compute_zones,
    list_specs,
    load_spec,
    save_spec,
    validate_observation,
)
from .mz50 import CLOSE_ON_CANDIDATE_UPDATE, KEEP_OPEN, MZ50Params, MZ50Strategy
from .report import build_payload, read_metrics, read_trades, render_chart, write_chart
from .rollover import RolloverPoint, RolloverTracker
from .zones import (
    INITIAL,
    STRICT_EXTENSION,
    ZONE_INPUT_CHANGE,
    ZoneTracker,
    ZoneVersion,
    summarise,
    zone_spans,
)

__all__ = [
    "CLOSE_ON_CANDIDATE_UPDATE", "CONTRACT_DIR", "ContractSpec", "Crossing",
    "CrossingTracker", "DEFAULT_INITIAL_RATIO", "DEFAULT_MAX_GAP_DAYS",
    "INITIAL", "KEEP_OPEN", "MARGIN_LOG", "MZ50Params", "MZ50Strategy",
    "MarginLog", "MarginObservation", "MarginZones", "RolloverPoint",
    "RolloverTracker", "STRICT_EXTENSION", "ZONE_INPUT_CHANGE",
    "ZoneTracker", "ZoneVersion", "build_payload", "compute_zones", "list_specs",
    "load_spec", "read_metrics", "read_trades", "render_chart",
    "save_spec", "summarise", "validate_observation", "write_chart", "zone_spans",
]
