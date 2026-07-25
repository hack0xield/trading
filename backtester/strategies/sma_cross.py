"""Moving-average crossover — the second strategy, present to prove the shape.

Where `day_open` trades on the calendar and needs no history, this one trades
on a signal computed from closed bars. Between them they exercise both halves
of the strategy API:

* decisions taken at a bar's open (`on_bar_open`), filled at that open
* decisions taken from a closed bar (`on_bar`), filled at the next open

`warmup_bars` on the engine keeps the strategy silent until the slow average
has enough data, so the first trades are not taken on a half-formed indicator.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.context import Context
from ..core.strategy import Strategy
from ..core.types import Bar, ExitReason, Side
from ..utils.params import StrategyParams
from .registry import register


@dataclass
class SmaCrossParams(StrategyParams):
    volume: float = 0.1
    fast: int = 20
    slow: int = 50
    stop_pct: float = 1.0
    take_pct: float = 2.0
    allow_short: bool = True
    close_on_opposite: bool = True


@register
class SmaCrossStrategy(Strategy):
    name = "sma_cross"
    params_class = SmaCrossParams
    description = "Long above / short below a fast-slow SMA crossover"

    def on_start(self, ctx: Context) -> None:
        if self.p.fast >= self.p.slow:
            raise ValueError(f"fast ({self.p.fast}) must be shorter than slow ({self.p.slow})")
        self._previous: int = 0  # last observed sign of (fast - slow)

    def on_bar(self, ctx: Context, bar: Bar) -> None:
        if len(ctx.history) < self.p.slow:
            return

        closes = ctx.history.closes(self.p.slow)
        fast = sum(closes[-self.p.fast :]) / self.p.fast
        slow = sum(closes) / self.p.slow
        signal = 1 if fast > slow else -1 if fast < slow else 0

        crossed = self._previous != 0 and signal != 0 and signal != self._previous
        self._previous = signal
        if not crossed:
            return

        if ctx.positions and self.p.close_on_opposite:
            ctx.close_all(ExitReason.STRATEGY)

        if signal < 0 and not self.p.allow_short:
            return

        # Raised after the close, so the engine fills it at the next bar's open.
        ctx.order(
            Side.BUY if signal > 0 else Side.SELL,
            volume=self.p.volume,
            sl_pct=self.p.stop_pct,
            tp_pct=self.p.take_pct,
            tag=f"sma {self.p.fast}/{self.p.slow}",
        )
