"""Value objects shared by every layer of the backtester.

Everything here is a plain dataclass with no third-party dependencies, so the
engine can run under any Python 3.10+ interpreter — including the Wine one that
hosts MetaTrader5.

Time convention: every `datetime` in this package is timezone-aware UTC. Bars
carry their *open* time, which is what MT5 reports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Side(Enum):
    """Direction of a position or order."""

    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        """+1 for long, -1 for short. Multiply price deltas by this to get P&L."""
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY

    @classmethod
    def parse(cls, value: "str | Side") -> "Side":
        if isinstance(value, cls):
            return value
        return cls(str(value).strip().upper())


class ExitReason(Enum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    STRATEGY = "STRATEGY"          # strategy asked to close
    SESSION_END = "SESSION_END"    # end-of-day / max-hold rule
    END_OF_DATA = "END_OF_DATA"    # backtest finished with the position open
    MARGIN_CALL = "MARGIN_CALL"


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLC candle. Prices are bid-side, matching MT5 rate history."""

    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    spread: float = 0.0  # price units, 0 means "use the instrument default"

    def as_row(self) -> dict:
        return {
            "time": self.time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "spread": self.spread,
        }


@dataclass(slots=True)
class OrderRequest:
    """A market order queued by a strategy, filled on the next bar's open.

    Stop and target can be given either as absolute prices or as percentages of
    the fill price. Percentages are resolved by the broker at fill time, which
    is the only moment the entry price is actually known — a strategy that says
    "stop 2%" must not have to guess where it will be filled.
    """

    side: Side
    volume: float
    sl_price: float | None = None
    tp_price: float | None = None
    sl_pct: float | None = None
    tp_pct: float | None = None
    tag: str = ""
    created_at: datetime | None = None


@dataclass(slots=True)
class Position:
    """An open position. One per fill; the engine never nets them together."""

    id: int
    side: Side
    volume: float
    entry_time: datetime
    entry_price: float
    sl: float | None = None
    tp: float | None = None
    tag: str = ""
    commission: float = 0.0
    swap: float = 0.0
    bars_held: int = 0
    mae: float = 0.0  # max adverse excursion, price units, always <= 0
    mfe: float = 0.0  # max favourable excursion, price units, always >= 0

    def unrealised(self, price: float, contract_size: float) -> float:
        delta = (price - self.entry_price) * self.side.sign
        return delta * self.volume * contract_size


@dataclass(slots=True)
class Trade:
    """A closed position, with everything needed for post-hoc analysis."""

    id: int
    side: Side
    volume: float
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    reason: ExitReason
    gross_pnl: float
    commission: float
    swap: float
    bars_held: int
    mae: float
    mfe: float
    balance_after: float
    tag: str = ""

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.commission + self.swap

    @property
    def is_win(self) -> bool:
        return self.net_pnl > 0

    @property
    def return_pct(self) -> float:
        """Price move in percent, signed by direction. Independent of size."""
        if self.entry_price == 0:
            return 0.0
        return (self.exit_price - self.entry_price) / self.entry_price * 100.0 * self.side.sign

    def as_row(self) -> dict:
        return {
            "id": self.id,
            "side": self.side.value,
            "volume": self.volume,
            "entry_time": self.entry_time.isoformat(),
            "entry_price": self.entry_price,
            "exit_time": self.exit_time.isoformat(),
            "exit_price": self.exit_price,
            "reason": self.reason.value,
            "gross_pnl": round(self.gross_pnl, 2),
            "commission": round(self.commission, 2),
            "swap": round(self.swap, 2),
            "net_pnl": round(self.net_pnl, 2),
            "return_pct": round(self.return_pct, 4),
            "bars_held": self.bars_held,
            "mae": round(self.mae, 5),
            "mfe": round(self.mfe, 5),
            "balance_after": round(self.balance_after, 2),
            "tag": self.tag,
        }


@dataclass(slots=True)
class EquityPoint:
    time: datetime
    balance: float   # realised only
    equity: float    # realised + open P&L
    open_positions: int = 0

    def as_row(self) -> dict:
        return {
            "time": self.time.isoformat(),
            "balance": round(self.balance, 2),
            "equity": round(self.equity, 2),
            "open_positions": self.open_positions,
        }


@dataclass(slots=True)
class BacktestResult:
    """Raw output of one run. Metrics are computed separately from this."""

    strategy: str
    symbol: str
    timeframe: str
    params: dict = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    equity: list[EquityPoint] = field(default_factory=list)
    initial_balance: float = 0.0
    final_balance: float = 0.0
    bars_processed: int = 0
    start: datetime | None = None
    end: datetime | None = None
    logs: list[str] = field(default_factory=list)
    #: Extra tables a strategy chose to publish, `name -> rows`. Saved beside
    #: trades.csv as `<name>.csv`, which is how a chart gets at whatever the
    #: strategy knows that a trade list cannot express.
    artifacts: dict[str, list[dict]] = field(default_factory=dict)
