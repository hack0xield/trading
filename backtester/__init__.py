"""A dependency-light backtesting framework for MetaTrader 5 symbols.

    from backtester import Backtester, ExecutionConfig, load_bars
    from backtester.strategies.day_open import DayOpenStrategy

    bars = load_bars("XAUUSD", "M15", data="parquet://data/bars")
    result = Backtester(DayOpenStrategy(stop_pct=2, take_pct=2),
                        "XAUUSD", "M15").run(bars)

The core engine imports nothing outside the standard library, so it runs
anywhere — including the Wine Python that hosts the MetaTrader5 package.
Storage backends pull in pyarrow or SQLAlchemy only when actually used.
"""

from .core.broker import ExecutionConfig, SimulatedBroker
from .core.context import BarOpen, Context
from .core.engine import Backtester, EngineConfig, run_backtest
from .core.instrument import Instrument, load_instrument
from .core.strategy import Strategy
from .core.types import Bar, BacktestResult, ExitReason, Side, Trade
from .data.loader import load_bars
from .data.registry import store_from_uri
from .metrics.stats import Metrics, compute

__version__ = "0.1.0"

__all__ = [
    "Backtester",
    "BacktestResult",
    "Bar",
    "BarOpen",
    "Context",
    "EngineConfig",
    "ExecutionConfig",
    "ExitReason",
    "Instrument",
    "Metrics",
    "Side",
    "SimulatedBroker",
    "Strategy",
    "Trade",
    "compute",
    "load_bars",
    "load_instrument",
    "run_backtest",
    "store_from_uri",
]
