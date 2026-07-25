"""Performance statistics, computed from the equity curve and the trade list.

Pure Python on purpose: this runs inside optimizer workers thousands of times,
and importing pandas per worker costs more than every calculation here put
together.

Two conventions worth stating, because they differ between tools:

* Drawdown is measured on **equity** (mark-to-market), not on closed balance.
  A balance-only drawdown hides how much heat a position took before it worked.
* Sharpe and Sortino are annualised from **daily** equity returns regardless of
  the bar timeframe, so an M5 run and a D1 run are directly comparable.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import date

from ..core.types import BacktestResult, EquityPoint, Trade

TRADING_DAYS = 252


@dataclass
class Metrics:
    # headline
    net_profit: float = 0.0
    return_pct: float = 0.0
    cagr_pct: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_days: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    recovery_factor: float = 0.0

    # trades
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate_pct: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    payoff_ratio: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    avg_bars_held: float = 0.0
    total_commission: float = 0.0
    total_swap: float = 0.0

    # context
    initial_balance: float = 0.0
    final_balance: float = 0.0
    final_equity: float = 0.0
    exposure_pct: float = 0.0
    bars: int = 0
    days: int = 0
    exits: dict = None  # type: ignore[assignment]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["exits"] = self.exits or {}
        return data


def compute(result: BacktestResult) -> Metrics:
    trades = result.trades
    equity = result.equity
    m = Metrics(exits={})

    m.initial_balance = result.initial_balance
    m.final_balance = result.final_balance
    m.final_equity = equity[-1].equity if equity else result.final_balance
    m.bars = result.bars_processed
    m.net_profit = m.final_equity - m.initial_balance
    if m.initial_balance:
        m.return_pct = m.net_profit / m.initial_balance * 100.0

    _drawdown(m, equity)
    _returns(m, equity, result)
    _trades(m, trades)

    if equity:
        with_position = sum(1 for p in equity if p.open_positions > 0)
        m.exposure_pct = with_position / len(equity) * 100.0

    if m.max_drawdown > 0:
        m.recovery_factor = m.net_profit / m.max_drawdown
    if m.max_drawdown_pct > 0:
        m.calmar = m.cagr_pct / m.max_drawdown_pct

    m.exits = dict(Counter(t.reason.value for t in trades))
    return m


def _drawdown(m: Metrics, equity: list[EquityPoint]) -> None:
    if not equity:
        return
    peak = equity[0].equity
    peak_time = equity[0].time
    worst = 0.0
    worst_pct = 0.0
    longest = 0.0

    for point in equity:
        if point.equity > peak:
            peak = point.equity
            peak_time = point.time
        drop = peak - point.equity
        if drop > worst:
            worst = drop
        if peak > 0:
            pct = drop / peak * 100.0
            if pct > worst_pct:
                worst_pct = pct
        # Underwater time, measured from the peak that has not been recovered.
        under = (point.time - peak_time).total_seconds() / 86400.0
        if under > longest:
            longest = under

    m.max_drawdown = worst
    m.max_drawdown_pct = worst_pct
    m.max_drawdown_days = longest


def _returns(m: Metrics, equity: list[EquityPoint], result: BacktestResult) -> None:
    """Sharpe, Sortino and CAGR from a daily-sampled equity curve."""
    if len(equity) < 2:
        return

    daily: dict[date, float] = {}
    for point in equity:
        daily[point.time.date()] = point.equity  # last value of each day wins
    series = [daily[key] for key in sorted(daily)]
    m.days = len(series)

    returns = []
    for previous, current in zip(series, series[1:]):
        if previous > 0:
            returns.append(current / previous - 1.0)
    if not returns:
        return

    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / max(1, len(returns) - 1)
    stdev = math.sqrt(variance)
    if stdev > 0:
        m.sharpe = mean / stdev * math.sqrt(TRADING_DAYS)

    downside = [r for r in returns if r < 0]
    if downside:
        dvar = sum(r * r for r in downside) / len(downside)
        ddev = math.sqrt(dvar)
        if ddev > 0:
            m.sortino = mean / ddev * math.sqrt(TRADING_DAYS)

    if result.start and result.end:
        years = (result.end - result.start).total_seconds() / (365.25 * 86400)
        if years > 0 and m.initial_balance > 0 and m.final_equity > 0:
            m.cagr_pct = ((m.final_equity / m.initial_balance) ** (1 / years) - 1) * 100.0


def _trades(m: Metrics, trades: list[Trade]) -> None:
    m.trades = len(trades)
    if not trades:
        return

    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    m.wins = len(wins)
    m.losses = len(losses)
    m.win_rate_pct = m.wins / m.trades * 100.0

    gross_win = sum(t.net_pnl for t in wins)
    gross_loss = abs(sum(t.net_pnl for t in losses))
    # A run with no losers has no meaningful ratio; inf is the honest answer.
    m.profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")

    m.avg_win = gross_win / m.wins if m.wins else 0.0
    m.avg_loss = -gross_loss / m.losses if m.losses else 0.0
    m.expectancy = sum(t.net_pnl for t in trades) / m.trades
    m.payoff_ratio = abs(m.avg_win / m.avg_loss) if m.avg_loss else float("inf")
    m.largest_win = max((t.net_pnl for t in trades), default=0.0)
    m.largest_loss = min((t.net_pnl for t in trades), default=0.0)
    m.avg_bars_held = sum(t.bars_held for t in trades) / m.trades
    m.total_commission = sum(t.commission for t in trades)
    m.total_swap = sum(t.swap for t in trades)

    streak_win = streak_loss = 0
    for trade in trades:
        if trade.net_pnl > 0:
            streak_win += 1
            streak_loss = 0
        else:
            streak_loss += 1
            streak_win = 0
        m.max_consecutive_wins = max(m.max_consecutive_wins, streak_win)
        m.max_consecutive_losses = max(m.max_consecutive_losses, streak_loss)


def monthly_returns(result: BacktestResult) -> dict[str, float]:
    """Equity change per calendar month, in percent. Useful for spotting regimes."""
    if not result.equity:
        return {}
    months: dict[str, list[float]] = {}
    for point in result.equity:
        months.setdefault(f"{point.time:%Y-%m}", []).append(point.equity)

    out = {}
    previous_close = result.initial_balance
    for key in sorted(months):
        values = months[key]
        if previous_close > 0:
            out[key] = (values[-1] / previous_close - 1.0) * 100.0
        previous_close = values[-1]
    return out
