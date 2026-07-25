"""Performance statistics and report rendering."""

from .report import monthly_table, text_report, trades_preview
from .stats import Metrics, compute, monthly_returns

__all__ = ["Metrics", "compute", "monthly_returns", "monthly_table", "text_report", "trades_preview"]
