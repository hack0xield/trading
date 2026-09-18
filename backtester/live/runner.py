"""One strategy trading a live account bar by bar, in the backtest's order of steps.

Startup replays the configured history through the backtest engine, so the
strategy reaches the present in exactly the state the backtest would. From then
on the runner is in one of two modes:

* **shadow** — the backtest keeps stepping each newly closed bar; nothing is
  sent to the account. `paper` stays here for good.
* **live** — the same strategy object trades through a `LiveBroker`.

Shadow hands over to live at a bar's open once the account can hold what the
replay holds: positions and orders under this runner's magic number that match
the replay are adopted, and the rest are closed or cancelled. A replayed
position the account lacks is never opened late; the runner waits for the
replay to close it.

Each new bar runs the engine's six steps split across the boundary: stops,
targets and limit fills since the last bar are read back, `on_trade` and
`on_bar` see the finished bar, then queued orders go out at the new open around
`on_bar_open`. Every poll rewrites `state.json`.
"""

from __future__ import annotations

import math
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from ..core.broker import ExecutionConfig
from ..core.context import BarOpen
from ..core.engine import Backtester, EngineConfig
from ..core.instrument import Instrument
from ..core.strategy import Strategy
from ..core.types import Bar, OrderRequest, Position, Side
from ..data.loader import validate_bars
from ..utils import timeframes
from .broker import (
    DEFAULT_RETRY_SECONDS,
    BrokerUnavailable,
    LiveBroker,
    comment_for,
    order_row,
    position_row,
    trade_mode_name,
)
from .events import (
    BAR_CLOSED,
    ERROR,
    LIVE,
    MODE,
    ORDER_FILLED,
    ORDER_INTENT,
    POSITION_CLOSED,
    SHADOW,
    STARTED,
    STOPPED,
    Notifier,
)
from .state import DEFAULT_STALE_AFTER_DAYS, MarginWatch, StateFile


class BarFeed(Protocol):
    def history(self, start: datetime) -> list[Bar]:
        """Every bar from `start`, the one still forming last."""

    def latest(self, count: int) -> list[Bar]:
        """The newest `count` bars, the one still forming last."""


class LiveRunner:
    def __init__(
        self,
        mt5,
        feed: BarFeed,
        strategy: Strategy,
        symbol: str,
        timeframe: str,
        start: datetime,
        magic: int,
        notify: Notifier,
        instrument: Instrument,
        execution: ExecutionConfig | None = None,
        engine: EngineConfig | None = None,
        deviation: int = 20,
        poll_seconds: float = 2.0,
        paper: bool = False,
        stop_file: str | Path | None = None,
        state: StateFile | None = None,
        margin_stale_days: int = DEFAULT_STALE_AFTER_DAYS,
        reconnect: Callable[[], None] | None = None,
        reconnect_seconds: float = 30.0,
        retry_seconds: float = DEFAULT_RETRY_SECONDS,
        clock: Callable[[], float] = _time.monotonic,
        log: Callable[[str], None] = print,
    ):
        self.mt5 = mt5
        self.feed = feed
        self.strategy = strategy
        self.symbol = symbol
        self.timeframe = timeframes.normalize(timeframe)
        self.start = start
        self.magic = magic
        self.notify = notify
        self.instrument = instrument
        self.deviation = deviation
        self.poll_seconds = poll_seconds
        self.paper = paper
        self.stop_file = Path(stop_file) if stop_file else None
        self.state = state
        self.margin = MarginWatch(strategy, margin_stale_days)
        self.reconnect = reconnect
        self.reconnect_seconds = reconnect_seconds
        self.retry_seconds = retry_seconds
        self.clock = clock
        self.log = log

        self.backtest = Backtester(strategy, symbol, self.timeframe, instrument, execution, engine)
        self.bars: list[Bar] = []
        self.live: LiveBroker | None = None
        self.login: int | None = None
        self.account: dict | None = None
        self.balance: float | None = None
        self.equity: float | None = None
        self.failure: str | None = None
        self._open_time: datetime | None = None
        self._seen_trades = 0
        self._last_reconnect = -math.inf

    @property
    def ctx(self):
        return self.backtest.ctx

    # -------------------------------------------------------------- lifecycle

    def run(self) -> None:
        """Replay, then trade until the stop file appears or the process is interrupted.

        A failure, at startup or later, is reported as an `error` event and in
        the final state before it is raised.
        """
        if self.stop_file is not None:
            self.stop_file.unlink(missing_ok=True)
        try:
            self.warm_up()
            while not self._stop_requested():
                try:
                    self.tick()
                except BrokerUnavailable as exc:
                    self._unavailable(exc)
                self.write_state()
                _time.sleep(self.poll_seconds)
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            self.failure = f"{type(exc).__name__}: {exc}"
            self.notify.emit(ERROR, error=self.failure, retrying=False)
            raise
        finally:
            self.shutdown()

    def warm_up(self) -> None:
        """Replay history through the backtest, then hand over if the account agrees."""
        info = self.mt5.account_info()
        if info is None:
            raise BrokerUnavailable(f"terminal not logged in: {self.mt5.last_error()}")
        self.login = int(info.login)
        self.account = {"login": self.login, "server": info.server,
                        "trade_mode": trade_mode_name(self.mt5, info.trade_mode)}
        self.balance, self.equity = float(info.balance), float(info.equity)

        bars = self.feed.history(self.start)
        if len(bars) < 2:
            raise ValueError(f"No closed {self.symbol} {self.timeframe} bars since {self.start}")
        forming = bars.pop()
        errors = [p for p in validate_bars(bars, self.timeframe) if p.startswith("ERROR")]
        if errors:
            raise ValueError("Bad bar data:\n  " + "\n  ".join(errors))

        self.bars = bars
        self.backtest.start(self.bars)
        while self.backtest.processed < len(self.bars):
            self.backtest.step()
        self._open_time = forming.time

        sim = self.backtest.broker
        self.log(
            f"replayed {len(bars):,} bars {bars[0].time:%Y-%m-%d} .. "
            f"{bars[-1].time:%Y-%m-%d %H:%M}: {len(sim.trades)} trades, "
            f"{len(sim.positions)} open, {len(sim.pending)} pending"
        )
        self.notify.emit(
            STARTED, magic=self.magic, paper=self.paper, account=self.account,
            replay_from=bars[0].time, last_closed_bar=bars[-1].time,
            replay_trades=len(sim.trades), replay_open=[position_row(p) for p in sim.positions],
            replay_pending=[order_row(o) for o in sim.pending], margin=self.margin.status(),
        )
        if not self.paper:
            self._handover(at_startup=True)
        self.write_state()

    def tick(self) -> None:
        """One poll: settle newly closed bars and the new open, or watch the one in progress."""
        if self.live is None:
            self._read_account()
        latest = self._latest()
        forming = latest[-1]
        for bar in latest[:-1]:
            if bar.time > self.bars[-1].time:
                self._close(bar)

        if forming.time > self._open_time:
            if self.live is None and not self.paper:
                self._handover(at_startup=False)
            if self.live is not None:
                self._open(forming)
            self._open_time = forming.time
        elif self.live is not None:
            self._watch()

    def shutdown(self) -> None:
        """Leave positions to their server-side stops; resting orders need this process."""
        if self.live is not None:
            try:
                self.live.withdraw_resting("runner stopped")
            except BrokerUnavailable as exc:
                self.log(f"could not withdraw resting orders: {exc}")
        if self.stop_file is not None:
            self.stop_file.unlink(missing_ok=True)
        self.notify.emit(STOPPED, failure=self.failure)
        self.write_state(running=False)

    def write_state(self, running: bool = True) -> None:
        if self.state is not None:
            self.state.write(running=running, failure=self.failure, **self._snapshot())

    # --------------------------------------------------------------- the bars

    def _latest(self) -> list[Bar]:
        """Recent bars reaching back to the last one processed."""
        count = 3
        while True:
            latest = self.feed.latest(count)
            if latest[0].time <= self.bars[-1].time or count >= 50_000:
                return latest
            count *= 8

    def _close(self, bar: Bar) -> None:
        if self.live is None:
            self._shadow_step(bar)
        else:
            self._live_close(bar)

    def _shadow_step(self, bar: Bar) -> None:
        """The backtest's own step, reported as it happens."""
        sim = self.backtest.broker
        held = {p.id for p in sim.positions}
        closed_before = len(sim.trades)
        self.bars.append(bar)
        self.backtest.step()

        new_trades = sim.trades[closed_before:]
        for position in sim.positions:
            if position.id not in held:
                self.notify.emit(ORDER_FILLED, **position_row(position))
        for trade in new_trades:
            if trade.id not in held:
                self.notify.emit(ORDER_FILLED, ticket=trade.id, side=trade.side,
                                 volume=trade.volume, price=trade.entry_price, sl=trade.sl,
                                 tp=trade.tp, tag=trade.tag, entry_time=trade.entry_time)
            self.notify.emit(POSITION_CLOSED, **trade.as_row(), ticket=trade.id)
        for order in sim.pending:
            if order.created_at == bar.time:
                self.notify.emit(ORDER_INTENT, **order_row(order))
        self._bar_closed(bar, sim.balance, sim.equity, len(sim.positions))

    def _live_close(self, bar: Bar) -> None:
        """Engine steps 4-6 for a bar the account has just lived through."""
        live, ctx = self.live, self.ctx
        live.sync()                      # raises, untouched, when the terminal is away
        live.retry()
        live.expire_queued()
        self.bars.append(bar)
        ctx.now, ctx.bar = bar.time, bar
        live.void_reached(bar.high, bar.low)
        live.track_bar(bar)
        ctx.price = bar.close
        self.backtest.history._advance(len(self.bars))

        self._flush_trades()
        self.strategy.on_bar(ctx, bar)
        self._flush_trades()
        point = live.record_equity(bar)
        self._bar_closed(bar, point.balance, point.equity, point.open_positions)

    def _open(self, forming: Bar) -> None:
        """Engine steps 1-3 at the new bar's open."""
        live, ctx = self.live, self.ctx
        price = live.tick().bid
        ctx.now, ctx.price = forming.time, price
        live.execute_pending(resend=False)
        self.strategy.on_bar_open(ctx, BarOpen(time=forming.time, price=price))
        live.execute_pending(resend=False)

    def _watch(self) -> None:
        """Between bars: read back fills and closes, retry what is owed, watch void levels."""
        live = self.live
        live.sync()
        live.retry()
        tick = live.tick()
        live.void_reached(tick.bid, tick.bid)
        live.execute_pending()

    def _flush_trades(self) -> None:
        for trade in self.live.trades[self._seen_trades:]:
            self.strategy.on_trade(self.ctx, trade)
        self._seen_trades = len(self.live.trades)

    def _bar_closed(self, bar: Bar, balance: float, equity: float, open_positions: int) -> None:
        self.notify.emit(BAR_CLOSED, bar_time=bar.time, close=bar.close, balance=round(balance, 2),
                         equity=round(equity, 2), open_positions=open_positions)

    # ------------------------------------------------------------- handover

    def _handover(self, at_startup: bool) -> bool:
        """Move the strategy onto the account if the account can hold what the replay holds.

        At startup a replayed order still waiting to be sent is not chased: its
        open has already passed, so the runner waits for the next boundary.
        """
        sim = self.backtest.broker
        live = LiveBroker(self.mt5, self.symbol, self.instrument, self.magic, self.login,
                          self.notify, self.deviation, self.retry_seconds, self.clock)
        rows, order_rows = live.our_positions(), live.our_orders()

        held, spare = _pair(sim.positions, rows, self._same_position)
        if len(held) < len(sim.positions):
            return self._stay("the account does not hold the replay's position")
        limits = [o for o in sim.pending if o.limit_price is not None]
        resting, spare_orders = _pair(limits, order_rows, self._same_order)
        unsent = [o for o in sim.pending if all(o is not order for order, _ in resting)]
        if at_startup and unsent:
            return self._stay("the replay's orders were due at an open already past")

        for row in spare:
            live.close_leftover(row)
        for row in spare_orders:
            live.remove_leftover(row)
        for position, row in held:
            live.adopt_position(row, position.tag)
        for order, row in resting:
            live.adopt_order(order, row)
        for order in unsent:
            live.submit(order)

        self.live = live
        self.ctx.broker = live
        self._seen_trades = 0
        self.notify.mode = LIVE
        self.notify.emit(MODE, adopted_positions=[r.ticket for _, r in held],
                         adopted_orders=[r.ticket for _, r in resting],
                         closed_leftovers=[r.ticket for r in spare],
                         removed_leftovers=[r.ticket for r in spare_orders],
                         queued=[order_row(o) for o in unsent])
        self.log(f"trading live on magic {self.magic}")
        return True

    def _stay(self, reason: str) -> bool:
        self.log(f"staying in shadow: {reason}")
        return False

    def _same_position(self, position: Position, row) -> bool:
        kind = (self.mt5.POSITION_TYPE_BUY if position.side is Side.BUY
                else self.mt5.POSITION_TYPE_SELL)
        return int(row.type) == kind and row.comment == comment_for(position.tag)

    def _same_order(self, order: OrderRequest, row) -> bool:
        kind = (self.mt5.ORDER_TYPE_BUY_LIMIT if order.side is Side.BUY
                else self.mt5.ORDER_TYPE_SELL_LIMIT)
        return (int(row.type) == kind and row.comment == comment_for(order.tag)
                and abs(float(row.price_open) - order.limit_price) < self.instrument.tick_size / 2)

    # ----------------------------------------------------------------- state

    def _snapshot(self) -> dict:
        snapshot = {
            "mode": LIVE if self.live is not None else SHADOW,
            "paper": self.paper,
            "account": self.account,
            "last_closed_bar": self.bars[-1].time if self.bars else None,
            "forming_bar": self._open_time,
            "margin": self.margin.status(),
        }
        if self.live is not None:
            live = self.live
            return {**snapshot, "balance": live.balance, "equity": live.equity,
                    "source": "account", "positions": live.position_rows(),
                    "resting_orders": live.resting_rows(), "queued_orders": live.queued_rows()}

        # The replay's book, priced at the last close: nothing here is on the account.
        sim = getattr(self.backtest, "broker", None)
        positions, resting, queued = [], [], []
        if sim is not None and self.bars:
            last = self.bars[-1]
            for p in sim.positions:
                exit_price = last.close if p.side is Side.BUY else sim.ask(last.close, last)
                profit = sim.instrument.value_of((exit_price - p.entry_price) * p.side.sign,
                                                 p.volume)
                positions.append({**position_row(p), "profit": round(profit, 2)})
            for order in sim.pending:
                if order.limit_price is None:
                    queued.append(order_row(order))
                else:
                    resting.append({"ticket": None, **order_row(order)})
        return {**snapshot, "balance": self.balance, "equity": self.equity, "source": "replay",
                "positions": positions, "resting_orders": resting, "queued_orders": queued}

    def _read_account(self) -> None:
        """The account's balance and equity, while no live broker is reading them."""
        info = self.mt5.account_info()
        if info is not None and int(info.login) == self.login:
            self.balance, self.equity = float(info.balance), float(info.equity)

    # ------------------------------------------------------------------ misc

    def _unavailable(self, exc: BrokerUnavailable) -> None:
        self.notify.emit(ERROR, error=str(exc), retrying=True, **exc.detail)
        now = _time.monotonic()
        if self.reconnect is None or now - self._last_reconnect < self.reconnect_seconds:
            return
        self._last_reconnect = now
        try:
            self.reconnect()
        except (Exception, SystemExit) as failed:
            self.log(f"reconnect failed: {failed}")

    def _stop_requested(self) -> bool:
        return self.stop_file is not None and self.stop_file.exists()


def _pair(items: list, rows: list, same) -> tuple[list[tuple], list]:
    """Match each item to one row; returns the pairs and the rows left over."""
    left = list(rows)
    pairs = []
    for item in items:
        row = next((r for r in left if same(item, r)), None)
        if row is not None:
            left.remove(row)
            pairs.append((item, row))
    return pairs, left
