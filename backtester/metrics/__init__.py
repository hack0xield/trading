"""Performance statistics and report rendering."""

from . import casebook  # noqa: F401
from .report import monthly_table, text_report, trades_preview
from .stats import Metrics, compute, monthly_returns

__all__ = [
    "casebook","Metrics", "compute", "monthly_returns", "monthly_table", "text_report", "trades_preview"]
