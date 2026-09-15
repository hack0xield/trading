"""The API a strategy is allowed to use.

Strategies get this object and nothing else. In particular they never touch the
broker's internals or the bar list beyond the current index, which is what keeps
lookahead bugs from being expressible in strategy code at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .broker import SimulatedBroker
from .instrument import Instrument
from .types import Bar, ExitReason, OrderRequest, Position, Side, Trade


@dataclass(frozen=True, slots=True)
class BarOpen:
    """What is knowable at the instant a bar opens: the time and the price.

    Deliberately *not* a `Bar` — a strategy acting at the open must not be able
    to read the high, low or close of the bar it is about to trade into.
    """

    time: datetime
    price: float


class BarHistory:
    """Read-only view of closed bars, newest at index -1.

    Backed by the engine's list with a moving bound, so slicing costs nothing.
    """

    __slots__ = ("_bars", "_count")

    def __init__(self, bars: list[Bar]):
        self._bars = bars
        self._count = 0

    def _advance(self, count: int) -> None:
        self._count = count

    def __len__(self) -> int:
        return self._count

    def __getitem__(self, index):
        if isinstance(index, slice):
            return self._bars[: self._count][index]
        if index < 0:
            index += self._count
        if not 0 <= index < self._count:
            raise IndexError("bar index out of range")
        return self._bars[index]

    def __iter__(self):
        return iter(self._bars[: self._count])

    def closes(self, count: int) -> list[float]:
        return [b.close for b in self._bars[max(0, self._count - count) : self._count]]

    def highs(self, count: int) -> list[float]:
        return [b.high for b in self._bars[max(0, self._count - count) : self._count]]

    def lows(self, count: int) -> list[float]:
        return [b.low for b in self._bars[max(0, self._count - count) : self._count]]


class Context:
    """Handed to every strategy hook; the only way to place or close orders."""

    def __init__(
        self,
        broker: SimulatedBroker,
        symbol: str,
        timeframe: str,
        history: BarHistory,
        logs: list[str],
    ):
        self.broker = broker
        self.symbol = symbol
        self.timeframe = timeframe
        self.history = history
        self.now: datetime | None = None
        self.price: float = 0.0
        self.bar: Bar | None = None
        self._logs = logs

    # ------------------------------------------------------------------ state

    @property
    def instrument(self) -> Instrument:
        return self.broker.instrument

    @property
    def balance(self) -> float:
        return self.broker.balance

    @property
    def equity(self) -> float:
        return self.broker.equity

    @property
    def positions(self) -> list[Position]:
        return list(self.broker.positions)

    @property
    def position(self) -> Position | None:
        """The oldest open position, or None. Convenient for single-shot strategies."""
        return self.broker.positions[0] if self.broker.positions else None

    @property
    def is_flat(self) -> bool:
        return not self.broker.positions and not self.broker.pending

    @property
    def trades(self) -> list[Trade]:
        return self.broker.trades

    # ----------------------------------------------------------------- orders

    def buy(
        self,
        volume: float,
        sl: float | None = None,
        tp: float | None = None,
        sl_pct: float | None = None,
        tp_pct: float | None = None,
        tag: str = "",
    ) -> None:
        """Queue a market buy. Fills at the next price the engine can honestly offer."""
        self._order(Side.BUY, volume, sl, tp, sl_pct, tp_pct, tag)

    def sell(
        self,
        volume: float,
        sl: float | None = None,
        tp: float | None = None,
        sl_pct: float | None = None,
        tp_pct: float | None = None,
        tag: str = "",
    ) -> None:
        self._order(Side.SELL, volume, sl, tp, sl_pct, tp_pct, tag)

    def order(self, side: str | Side, volume: float, **kwargs) -> None:
        self._order(Side.parse(side), volume, **kwargs)

    def _order(
        self,
        side: Side,
        volume: float,
        sl: float | None = None,
        tp: float | None = None,
        sl_pct: float | None = None,
        tp_pct: float | None = None,
        tag: str = "",
        limit: float | None = None,
        cancel_at: float | None = None,
    ) -> None:
        self.broker.submit(
            OrderRequest(
                side=side,
                volume=volume,
                sl_price=sl,
                tp_price=tp,
                sl_pct=sl_pct,
                tp_pct=tp_pct,
                tag=tag,
                created_at=self.now,
                limit_price=limit,
                cancel_price=cancel_at,
            )
        )

    def cancel_pending(self) -> int:
        """Drop every order queued but not yet filled."""
        return self.broker.cancel_pending()

    def close(self, position: Position | None = None, reason: ExitReason = ExitReason.STRATEGY):
        """Close one position (default: the oldest) at the current price."""
        target = position or self.position
        if target is None:
            return None
        return self.broker.close_position(target, self.price, self.now, reason, self.bar)

    def close_all(self, reason: ExitReason = ExitReason.STRATEGY) -> list[Trade]:
        return self.broker.close_all(self.price, self.now, reason, self.bar)

    def modify(self, position: Position, sl: float | None = None, tp: float | None = None) -> None:
        """Move a stop or target — the hook a trailing stop hangs off."""
        self.broker.modify(position, sl, tp)

    # -------------------------------------------------------------------- log

    def log(self, message: str) -> None:
        stamp = self.now.isoformat() if self.now else "-"
        self._logs.append(f"{stamp}  {message}")
