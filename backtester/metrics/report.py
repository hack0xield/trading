"""Human-readable rendering of a run."""

from __future__ import annotations

from ..core.types import BacktestResult
from .stats import Metrics, monthly_returns


def _fmt(value: float, digits: int = 2) -> str:
    if value == float("inf"):
        return "inf"
    if value == float("-inf"):
        return "-inf"
    return f"{value:,.{digits}f}"


def text_report(result: BacktestResult, m: Metrics, width: int = 66) -> str:
    line = "=" * width
    thin = "-" * width
    period = "-"
    if result.start and result.end:
        period = f"{result.start:%Y-%m-%d} -> {result.end:%Y-%m-%d}"

    params = ", ".join(f"{k}={v}" for k, v in sorted(result.params.items()))

    rows = [
        line,
        f" {result.strategy}  |  {result.symbol} {result.timeframe}",
        f" {period}   ({m.bars:,} bars, {m.days:,} days)",
        f" params: {params}" if params else "",
        line,
        " PERFORMANCE",
        thin,
        f"  Initial balance        {_fmt(m.initial_balance):>18}",
        f"  Final equity           {_fmt(m.final_equity):>18}",
        f"  Net profit             {_fmt(m.net_profit):>18}",
        f"  Return                 {_fmt(m.return_pct):>17}%",
        f"  CAGR                   {_fmt(m.cagr_pct):>17}%",
        f"  Max drawdown           {_fmt(m.max_drawdown):>18}  ({_fmt(m.max_drawdown_pct)}%)",
        f"  Longest drawdown       {_fmt(m.max_drawdown_days, 1):>18} days",
        f"  Sharpe / Sortino       {_fmt(m.sharpe):>18} / {_fmt(m.sortino)}",
        f"  Calmar                 {_fmt(m.calmar):>18}",
        f"  Recovery factor        {_fmt(m.recovery_factor):>18}",
        f"  Exposure               {_fmt(m.exposure_pct):>17}%",
        "",
        " TRADES",
        thin,
        f"  Total                  {m.trades:>18,}",
        f"  Wins / losses          {m.wins:>18,} / {m.losses:,}",
        f"  Win rate               {_fmt(m.win_rate_pct):>17}%",
        f"  Profit factor          {_fmt(m.profit_factor):>18}",
        f"  Expectancy / trade     {_fmt(m.expectancy):>18}",
        f"  Avg win / avg loss     {_fmt(m.avg_win):>18} / {_fmt(m.avg_loss)}",
        f"  Payoff ratio           {_fmt(m.payoff_ratio):>18}",
        f"  Largest win / loss     {_fmt(m.largest_win):>18} / {_fmt(m.largest_loss)}",
        f"  Max consec. W / L      {m.max_consecutive_wins:>18} / {m.max_consecutive_losses}",
        f"  Avg bars held          {_fmt(m.avg_bars_held, 1):>18}",
        f"  Commission / swap      {_fmt(m.total_commission):>18} / {_fmt(m.total_swap)}",
    ]

    if m.exits:
        rows.append("")
        rows.append(" EXITS")
        rows.append(thin)
        for reason, count in sorted(m.exits.items(), key=lambda kv: -kv[1]):
            share = count / max(1, m.trades) * 100
            rows.append(f"  {reason:<22} {count:>18,}  ({share:.1f}%)")

    rows.append(line)
    return "\n".join(r for r in rows if r != "" or True)


def monthly_table(result: BacktestResult, width: int = 66) -> str:
    """Year-by-month grid of returns; the fastest way to see a strategy break."""
    returns = monthly_returns(result)
    if not returns:
        return ""

    by_year: dict[str, dict[int, float]] = {}
    for key, value in returns.items():
        year, month = key.split("-")
        by_year.setdefault(year, {})[int(month)] = value

    header = "  Year " + "".join(f"{m:>7}" for m in
                                 ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]) + f"{'Year':>9}"
    rows = ["", " MONTHLY RETURNS (%)", "-" * len(header), header]
    for year in sorted(by_year):
        cells = []
        compound = 1.0
        for month in range(1, 13):
            value = by_year[year].get(month)
            if value is None:
                cells.append(f"{'':>7}")
            else:
                cells.append(f"{value:>7.1f}")
                compound *= 1 + value / 100
        rows.append(f"  {year} " + "".join(cells) + f"{(compound - 1) * 100:>9.1f}")
    return "\n".join(rows)


def trades_preview(result: BacktestResult, limit: int = 10) -> str:
    if not result.trades:
        return "\n  (no trades)"
    rows = [
        "",
        f" FIRST {min(limit, len(result.trades))} TRADES",
        "-" * 66,
        f"  {'entry':<17}{'side':<6}{'in':>10}{'out':>10}{'pnl':>11}  reason",
    ]
    for trade in result.trades[:limit]:
        rows.append(
            f"  {trade.entry_time:%Y-%m-%d %H:%M} {trade.side.value:<5}"
            f"{trade.entry_price:>10.2f}{trade.exit_price:>10.2f}"
            f"{trade.net_pnl:>11.2f}  {trade.reason.value}"
        )
    if len(result.trades) > limit:
        rows.append(f"  ... {len(result.trades) - limit:,} more")
    return "\n".join(rows)
