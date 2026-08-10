"""Margin zones — everything specific to the strategy family.

Contract specifications, the dated CME margin log, the [FMZ, IMZ] envelope
projection, and the report that draws them. Anything usable without knowing
what a margin zone is belongs elsewhere: bars come from `data/`, swing pivots
from `indicators/`.

Both execution strategies and analysis-only tools for margin zones live here.
If a second classification of margin-zone strategy appears, split then.

See `MarginZones_revised.md` for the specification this implements.
"""

from .envelopes import Envelope, build_envelopes, margin_coverage, summarise
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
from .rollover import RolloverPoint, rollover_points

__all__ = [
    "CONTRACT_DIR", "ContractSpec", "DEFAULT_INITIAL_RATIO", "Envelope",
    "MARGIN_LOG", "MarginLog",
    "MarginObservation", "MarginZones", "RolloverPoint", "build_envelopes",
    "build_payload", "compute_zones", "list_specs", "load_spec", "margin_coverage",
    "render_chart", "report_name", "rollover_points", "save_spec", "summarise",
    "validate_observation", "write_report",
]
