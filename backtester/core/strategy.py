"""Strategy base class.

A strategy overrides the hooks it cares about and leaves the rest alone. The
split between `on_bar_open` and `on_bar` is the whole discipline of the
framework:

* `on_bar_open(ctx, event)` — you are standing at the open of a new bar. You
  know the time and the current price, nothing about how this bar ends. Orders
  raised here fill at this open. This is where "at the start of the day, buy"
  belongs.
* `on_bar(ctx, bar)` — the bar just closed and you can inspect all of it.
  Orders raised here fill at the *next* bar's open, because that is the first
  price you could really have traded after seeing this close.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..utils.params import StrategyParams
from .context import BarOpen, Context
from .types import Bar, Trade


@dataclass
class BaseParams(StrategyParams):
    """Parameters every strategy understands."""

    volume: float = 0.1


class Strategy:
    """Subclass this. Set `name` and `params_class`, override the hooks you need."""

    name: str = "base"
    params_class: type[StrategyParams] = BaseParams
    description: str = ""

    def __init__(self, params: dict | StrategyParams | None = None, **overrides):
        if isinstance(params, StrategyParams):
            base = params.to_dict()
        else:
            base = dict(params or {})
        base.update(overrides)
        self.p = self.params_class.from_dict(base)

    # ------------------------------------------------------------- lifecycle

    def on_start(self, ctx: Context) -> None:
        """Called once before the first bar."""

    def on_bar_open(self, ctx: Context, event: BarOpen) -> None:
        """Called at each bar's open, before the bar plays out."""

    def on_bar(self, ctx: Context, bar: Bar) -> None:
        """Called after each bar closes."""

    def on_trade(self, ctx: Context, trade: Trade) -> None:
        """Called whenever a position closes, for any reason."""

    def on_finish(self, ctx: Context) -> None:
        """Called after the last bar, before open positions are force-closed."""

    # ------------------------------------------------------------------ misc

    @property
    def params(self) -> dict:
        return self.p.to_dict()

    def describe(self) -> str:
        return f"{self.name}({self.p.describe()})"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.p.describe()}>"
