"""The backtest loop.

Per bar, in this exact order:

  1. fill orders queued while the previous bar was closing, at this open
  2. `strategy.on_bar_open` — may trade at this open
  3. fill anything it just queued, still at this open
  4. walk the bar: swap, stop/target checks, excursion tracking
  5. `strategy.on_bar` — sees the finished bar, orders wait for the next open
  6. record the equity point

Steps 1 and 3 use the same price, so an order is never filled at a price that
postdates the decision to place it.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass

from ..utils import timeframes
from .broker import ExecutionConfig, SimulatedBroker
from .context import BarHistory, BarOpen, Context
from .instrument import Instrument, load_instrument
from .strategy import Strategy
from .types import Bar, BacktestResult


@dataclass
class EngineConfig:
    warmup_bars: int = 0        # bars fed to history before the strategy trades
    close_at_end: bool = True   # force-close open positions on the last bar
    progress_every: int = 0     # print a heartbeat every N bars (0 = silent)


class Backtester:
    """Runs one strategy over one symbol's bars."""

    def __init__(
        self,
        strategy: Strategy,
        symbol: str,
        timeframe: str,
        instrument: Instrument | None = None,
        execution: ExecutionConfig | None = None,
        engine: EngineConfig | None = None,
    ):
        self.strategy = strategy
        self.symbol = symbol
        self.timeframe = timeframes.normalize(timeframe)
        self.instrument = instrument or load_instrument(symbol)
        self.execution = execution or ExecutionConfig()
        self.config = engine or EngineConfig()

    def run(self, bars: list[Bar]) -> BacktestResult:
        if not bars:
            raise ValueError(f"No bars to backtest for {self.symbol} {self.timeframe}")

        self.start(bars)
        while self.processed < len(bars):
            bar = self.step()
            every = self.config.progress_every
            if every and (self.processed - 1) % every == 0:
                print(
                    f"  {bar.time:%Y-%m-%d %H:%M}  "
                    f"equity={self.broker.equity:,.2f}  trades={len(self.broker.trades)}",
                    flush=True,
                )
            if self.broker.stopped_out:
                self.logs.append(f"{bar.time.isoformat()}  stopped out, halting run")
                break
        return self.finish()

    def start(self, bars: list[Bar]) -> Context:
        """Prepare a run over `bars` and call `on_start`.

        The list is read by reference, so bars appended later can still be stepped.
        """
        self.bars = bars
        self.broker = SimulatedBroker(self.instrument, self.execution)
        self.logs: list[str] = []
        self.history = BarHistory(bars)
        self.ctx = Context(self.broker, self.symbol, self.timeframe, self.history, self.logs)
        self.processed = 0
        self._seen_trades = 0
        self._started = _time.perf_counter()
        self.strategy.on_start(self.ctx)
        return self.ctx

    def step(self) -> Bar:
        """Play the next unprocessed bar through the six steps."""
        index = self.processed
        bar = self.bars[index]
        ctx, broker = self.ctx, self.broker
        warm = index >= max(0, self.config.warmup_bars)

        ctx.now = bar.time
        ctx.bar = bar
        ctx.price = bar.open

        # 1. Orders queued at the previous close fill here, at this open.
        broker.fill_pending(bar.open, bar.time, bar)

        if warm:
            # 2 + 3. Decide on the open, then fill at that same open.
            self.strategy.on_bar_open(ctx, BarOpen(time=bar.time, price=bar.open))
            broker.fill_pending(bar.open, bar.time, bar)

        # 4. Advance positions through the bar's range.
        broker.process_bar(bar)

        # The bar is now finished, so the close is the tradeable price.
        ctx.price = bar.close
        self.history._advance(index + 1)
        self.processed = index + 1

        self._flush_trades()

        # 5. React to the closed bar; orders wait for the next open.
        if warm:
            self.strategy.on_bar(ctx, bar)
            self._flush_trades()

        # 6.
        broker.record_equity(bar)
        return bar

    def finish(self) -> BacktestResult:
        """Call `on_finish`, settle what is still open, and collect the result."""
        ctx, broker = self.ctx, self.broker
        last = self.bars[self.processed - 1]
        ctx.price = last.close
        self.strategy.on_finish(ctx)
        if self.config.close_at_end and broker.positions:
            broker.finalize(last)
        self._flush_trades()
        broker.record_equity(last)

        return BacktestResult(
            strategy=self.strategy.name,
            symbol=self.symbol,
            timeframe=self.timeframe,
            params=self.strategy.params,
            trades=broker.trades,
            equity=broker.equity_curve,
            initial_balance=self.execution.initial_balance,
            final_balance=broker.balance,
            bars_processed=self.processed,
            start=self.bars[0].time,
            end=last.time,
            artifacts=self.strategy.artifacts(),
            logs=self.logs
            + [f"run took {_time.perf_counter() - self._started:.2f}s"]
            + [f"rejected: {reason}" for _, reason in broker.rejected[:20]],
        )

    def _flush_trades(self) -> None:
        """Deliver on_trade for anything closed since the last check."""
        for trade in self.broker.trades[self._seen_trades:]:
            self.strategy.on_trade(self.ctx, trade)
        self._seen_trades = len(self.broker.trades)


def run_backtest(
    strategy: Strategy,
    bars: list[Bar],
    symbol: str,
    timeframe: str,
    instrument: Instrument | None = None,
    execution: ExecutionConfig | None = None,
    engine: EngineConfig | None = None,
) -> BacktestResult:
    """One-call helper for notebooks and tests."""
    return Backtester(
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        instrument=instrument,
        execution=execution,
        engine=engine,
    ).run(bars)
