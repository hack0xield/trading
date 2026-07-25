"""Day-open entry with symmetric percentage stop and target.

    1) at the start of each trading day, open a position
    2) stop loss  `stop_pct` away from the fill
    3) take profit `take_pct` away from the fill

Written as the reference strategy: it is deliberately trivial, so anything odd
in a result is the engine's fault, not the logic's. It is also a useful null
hypothesis — with a symmetric stop and target, the P&L before costs should be
close to a coin flip, and the gap between that and the actual result is exactly
what spread, swap and commission cost you.

Note that "the start of the day" is a choice, not a fact. `session_tz` and
`session_start_hour` place the boundary; the defaults use midnight UTC, but MT5
bar stamps are broker-server time (usually UTC+2/+3), so for a broker-aligned
day set `session_tz=UTC+0` and leave the data as-is, or pass the broker offset.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.context import BarOpen, Context
from ..core.strategy import Strategy
from ..core.types import Bar, ExitReason, Side
from ..utils.params import StrategyParams
from ..utils.timeutil import get_tz, session_day
from .registry import register


@dataclass
class DayOpenParams(StrategyParams):
    volume: float = 0.1
    direction: str = "BUY"           # BUY or SELL
    stop_pct: float = 2.0            # percent of entry price
    take_pct: float = 2.0
    session_tz: str = "UTC"          # IANA name or "UTC+3"
    session_start_hour: int = 0      # hour, in session_tz, that starts the day
    one_position: bool = True        # skip the entry if one is still open
    close_at_day_end: bool = False   # flatten at the boundary before re-entering
    max_hold_bars: int | None = None  # hard time stop, in bars
    weekdays: str = ""               # e.g. "0,1,2,3,4" for Mon-Fri; "" = every day


@register
class DayOpenStrategy(Strategy):
    name = "day_open"
    params_class = DayOpenParams
    description = "Buy (or sell) at each day's open with a percentage stop and target"

    def on_start(self, ctx: Context) -> None:
        self._tz = get_tz(self.p.session_tz)
        self._side = Side.parse(self.p.direction)
        self._last_day = None
        self._weekdays = (
            {int(d) for d in self.p.weekdays.split(",") if d.strip() != ""}
            if self.p.weekdays
            else None
        )
        if self.p.stop_pct <= 0 or self.p.take_pct <= 0:
            raise ValueError("stop_pct and take_pct must both be > 0")

    def on_bar_open(self, ctx: Context, event: BarOpen) -> None:
        day = session_day(event.time, self._tz, self.p.session_start_hour)
        if day == self._last_day:
            return
        self._last_day = day

        if self.p.close_at_day_end and ctx.positions:
            ctx.close_all(ExitReason.SESSION_END)

        if self._weekdays is not None and day.weekday() not in self._weekdays:
            return
        if self.p.one_position and not ctx.is_flat:
            return

        ctx.order(
            self._side,
            volume=self.p.volume,
            sl_pct=self.p.stop_pct,
            tp_pct=self.p.take_pct,
            tag=f"day-open {day}",
        )

    def on_bar(self, ctx: Context, bar: Bar) -> None:
        if not self.p.max_hold_bars:
            return
        for position in ctx.positions:
            if position.bars_held >= self.p.max_hold_bars:
                ctx.close(position, ExitReason.SESSION_END)
