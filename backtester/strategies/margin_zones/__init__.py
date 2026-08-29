"""Margin zones — everything specific to the strategy family.

Contract specifications, the dated CME margin log, the [FMZ, IMZ] envelope
projection, and the report that draws them. Anything usable without knowing
what a margin zone is belongs elsewhere: bars come from `data/`, swing pivots
from `indicators/`.

Both execution strategies and analysis-only tools for margin zones live here.
If a second classification of margin-zone strategy appears, split then.

See `impl-spec/MarginZones_revised.md` for the specification this implements.
"""

from .approach25 import Senior25Params, Senior25Strategy, tag_for
from .crossing import CrossingSignal, CrossingTracker
from .provisional import ProvisionalZoneTracker, ZoneVersion
from .crossing50 import Crossing50Params, Crossing50Strategy
from .envelopes import Envelope, build_envelopes, envelope_from, margin_coverage, summarise
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
from .report import build_payload, render_chart, report_name, write_report
from .rollover import (
    RolloverCrossing, RolloverPoint, RolloverTracker, envelope_at,
    rollover_crossings, rollover_points,
)
from .senior import (
    LEVEL_FRACTION,
    TOLERANCE_FRACTION,
    ApproachEvent,
    SeniorApproachTracker,
    SeniorExtremum,
    first_approach,
    senior_extremums,
)

__all__ = [
    "CONTRACT_DIR", "ContractSpec", "DEFAULT_INITIAL_RATIO", "Envelope",
    "LEVEL_FRACTION", "MARGIN_LOG", "MarginLog",
    "MarginObservation", "MarginZones", "RolloverCrossing", "RolloverPoint",
    "ApproachEvent", "Crossing50Params", "Crossing50Strategy", "CrossingSignal",
    "CrossingTracker", "ProvisionalZoneTracker", "RolloverTracker", "ZoneVersion", "Senior25Params", "Senior25Strategy", "SeniorApproachTracker",
    "SeniorExtremum", "TOLERANCE_FRACTION",
    "build_envelopes", "build_payload", "compute_zones", "envelope_at",
    "envelope_from",
    "first_approach", "list_specs", "load_spec", "margin_coverage", "render_chart",
    "report_name", "rollover_crossings", "rollover_points", "save_spec",
    "senior_extremums", "summarise", "tag_for", "validate_observation", "write_report",
]
