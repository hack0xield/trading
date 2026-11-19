"""Simulated broker: fills, stops, targets, costs and equity.

Where a backtest lies to you, it usually lies here. Three decisions are made
explicitly rather than by accident:

* **No lookahead.** A market order can only be filled at a price the strategy
  could not yet see: orders raised at a bar's *open* fill at that open, orders
  raised after a bar closes fill at the *next* bar's open.
* **Intrabar ambiguity.** When a bar's range covers both the stop and the
  target, the bar itself cannot say which came first. The policy is a knob
  (`intrabar`), defaulting to the pessimistic answer, and a run that changes
  materially between `conservative` and `optimistic` is a run that needs finer
  data rather than a prettier assumption.
* **Bid/ask.** MT5 bars are bid prices. Longs enter at ask (bid + spread) and
  exit at bid; shorts do the reverse. Ignoring this makes a 2%/2% strategy look
  symmetric when it is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .instrument import Instrument
from .types import (
    Bar,
    EquityPoint,
    ExitReason,
    OrderRequest,
    Position,
    Side,
    Trade,
)


@dataclass
class ExecutionConfig:
    """Cost and fill assumptions for a run."""

    initial_balance: float = 10_000.0
    leverage: float = 100.0
    spread_points: float | None = None      # None -> instrument default / bar spread
    slippage_points: float = 0.0            # applied against the trader on every fill
    commission_per_lot: float | None = None  # None -> instrument default, per side
    apply_swap: bool = True
    intrabar: str = "conservative"          # conservative | optimistic | ohlc
    stop_out_level: float = 0.5             # close-out when equity < 50% of margin
    use_bar_spread: bool = True             # prefer the spread recorded on the bar

    def __post_init__(self) -> None:
        valid = {"conservative", "optimistic", "ohlc"}
        if self.intrabar not in valid:
            raise ValueError(f"intrabar must be one of {sorted(valid)}, got {self.intrabar!r}")


@dataclass
class _Fill:
    price: float
    time: datetime


class SimulatedBroker:
    """Single-symbol broker simulation. Positions are tracked individually."""

    def __init__(self, instrument: Instrument, config: ExecutionConfig | None = None):
        self.instrument = instrument
        self.config = config or ExecutionConfig()
        self.balance = self.config.initial_balance
        self.equity = self.balance
        self.positions: list[Position] = []
        self.trades: list[Trade] = []
        self.equity_curve: list[EquityPoint] = []
        self.pending: list[OrderRequest] = []
        self.cancelled: list[OrderRequest] = []   # limit orders voided before filling
        self.rejected: list[tuple[OrderRequest, str]] = []
        self._next_id = 1
        self._last_price = 0.0
        self._last_time: datetime | None = None
        self._last_swap_date = None
        self.stopped_out = False

    # ------------------------------------------------------------------ costs

    def _spread(self, bar: Bar | None) -> float:
        if self.config.spread_points is not None:
            return self.config.spread_points * self.instrument.tick_size
        if bar is not None and self.config.use_bar_spread and bar.spread > 0:
            return bar.spread
        return self.instrument.spread

    @property
    def _slippage(self) -> float:
        return self.config.slippage_points * self.instrument.tick_size

    def _commission(self, volume: float) -> float:
        per_lot = self.config.commission_per_lot
        if per_lot is None:
            per_lot = self.instrument.commission_per_lot
        return per_lot * volume

    def ask(self, bid: float, bar: Bar | None = None) -> float:
        return bid + self._spread(bar)

    # ----------------------------------------------------------------- orders

    def submit(self, order: OrderRequest) -> None:
        """Queue an order. Market orders fill at the next price the engine offers;
        limit orders rest until the market reaches their level."""
        volume = self.instrument.round_volume(order.volume)
        if volume <= 0:
            self.rejected.append((order, "volume rounds to zero"))
            return
        order.volume = volume
        self.pending.append(order)

    def fill_pending(self, bid: float, time: datetime, bar: Bar | None = None) -> list[Position]:
        """Execute every queued order at `bid` (a price already known to be safe).

        A limit order fills here only when `bid` is already through its level,
        which is a better price than it asked for; otherwise it stays queued for
        `process_bar` to fill at the level itself.
        """
        if not self.pending:
            return []
        opened, resting = [], []
        orders, self.pending = self.pending, []
        for order in orders:
            if order.limit_price is not None:
                if self._voided(order, bid):
                    self.cancelled.append(order)
                    continue
                if not self._limit_reached(order, bid, bar):
                    resting.append(order)
                    continue
            position = self._open(order, bid, time, bar)
            if position is not None:
                opened.append(position)
        self.pending = resting
        return opened

    @staticmethod
    def _voided(order: OrderRequest, price: float) -> bool:
        """Has price passed the level that voids this order?"""
        if order.cancel_price is None:
            return False
        if order.side is Side.BUY:
            return price >= order.cancel_price
        return price <= order.cancel_price

    def _limit_reached(self, order: OrderRequest, bid: float, bar: Bar | None) -> bool:
        """Is `bid` good enough to fill this limit order?"""
        if order.side is Side.BUY:
            return self.ask(bid, bar) <= order.limit_price
        return bid >= order.limit_price

    def fill_limits_within(self, bar: Bar) -> list[Position]:
        """Fill resting limit orders the bar traded through, at their own level.

        A position opened here is not walked through the rest of the same bar:
        the path after the fill is unknown, so its stop and target start on the
        next one.
        """
        if not any(o.limit_price is not None for o in self.pending):
            return []
        spread = self._spread(bar)
        opened, resting = [], []
        for order in self.pending:
            if order.limit_price is None:
                resting.append(order)
                continue
            limit = order.limit_price
            # The invalidation is checked first, so a bar reaching both levels
            # resolves as the cancellation rather than the fill.
            reached_void = order.cancel_price is not None and (
                bar.high >= order.cancel_price if order.side is Side.BUY
                else bar.low <= order.cancel_price
            )
            if reached_void:
                self.cancelled.append(order)
                continue
            # A buy fills when the ask reaches down to the limit, a sell when the
            # bid reaches up to it. `bid` is chosen so the fill lands exactly there.
            if order.side is Side.BUY and bar.low + spread <= limit:
                position = self._open(order, limit - spread, bar.time, bar, limit=True)
            elif order.side is Side.SELL and bar.high >= limit:
                position = self._open(order, limit, bar.time, bar, limit=True)
            else:
                resting.append(order)
                continue
            if position is not None:
                opened.append(position)
        self.pending = resting
        return opened

    def modify(self, position: Position, sl: float | None = None, tp: float | None = None) -> None:
        """Move a position's stop or target."""
        if sl is not None:
            position.sl = self.instrument.round_price(sl)
        if tp is not None:
            position.tp = self.instrument.round_price(tp)

    def cancel_pending(self) -> int:
        """Drop every queued order. Returns how many were dropped."""
        count = len(self.pending)
        self.pending.clear()
        return count

    def _open(
        self, order: OrderRequest, bid: float, time: datetime, bar: Bar | None,
        limit: bool = False,
    ) -> Position | None:
        # Longs pay the spread on entry, shorts pay it on exit. A limit order
        # never fills worse than its level, so slippage does not apply to it.
        slip = 0.0 if limit else self._slippage
        if order.side is Side.BUY:
            price = self.ask(bid, bar) + slip
        else:
            price = bid - slip

        price = self.instrument.round_price(price)
        if not self._has_margin(order.volume, price):
            self.rejected.append((order, "insufficient free margin"))
            return None

        sl, tp = self._resolve_levels(order, price)
        position = Position(
            id=self._next_id,
            side=order.side,
            volume=order.volume,
            entry_time=time,
            entry_price=price,
            sl=sl,
            tp=tp,
            tag=order.tag,
            commission=self._commission(order.volume) * 2,  # round turn, booked up front
        )
        self._next_id += 1
        self.positions.append(position)
        return position

    def _resolve_levels(self, order: OrderRequest, entry: float) -> tuple[float | None, float | None]:
        """Turn percentage stops into prices now that the fill price is known."""
        sign = order.side.sign
        sl = order.sl_price
        tp = order.tp_price
        if sl is None and order.sl_pct is not None:
            sl = entry * (1 - sign * order.sl_pct / 100.0)
        if tp is None and order.tp_pct is not None:
            tp = entry * (1 + sign * order.tp_pct / 100.0)
        if sl is not None:
            sl = self.instrument.round_price(sl)
        if tp is not None:
            tp = self.instrument.round_price(tp)
        return sl, tp

    def _has_margin(self, volume: float, price: float) -> bool:
        if self.config.leverage <= 0:
            return True
        required = volume * self.instrument.contract_size * price / self.config.leverage
        return self.free_margin() >= required

    def margin_used(self) -> float:
        if self.config.leverage <= 0:
            return 0.0
        return sum(
            p.volume * self.instrument.contract_size * p.entry_price / self.config.leverage
            for p in self.positions
        )

    def free_margin(self) -> float:
        return self.equity - self.margin_used()

    # ------------------------------------------------------------ bar process

    def process_bar(self, bar: Bar) -> list[Trade]:
        """Advance every open position through one bar: swap, stops, excursions."""
        self._last_price = bar.close
        self._last_time = bar.time
        closed: list[Trade] = []

        if self.config.apply_swap:
            self._charge_swap(bar)

        for position in list(self.positions):
            position.bars_held += 1
            self._update_excursions(position, bar)
            exit_fill = self._exit_within_bar(position, bar)
            if exit_fill is not None:
                reason, price = exit_fill
                closed.append(self._close(position, price, bar.time, reason))

        # Resting limit orders fill last, so a position opened inside this bar
        # is not also walked through it.
        self.fill_limits_within(bar)

        self._mark_to_market(bar.close, bar)
        closed.extend(self._check_stop_out(bar))
        return closed

    def _charge_swap(self, bar: Bar) -> None:
        """Book a rollover once per calendar-date change, per open position."""
        current = bar.time.date()
        if self._last_swap_date is None:
            self._last_swap_date = current
            return
        nights = (current - self._last_swap_date).days
        if nights <= 0:
            return
        self._last_swap_date = current
        for position in self.positions:
            points = (
                self.instrument.swap_long
                if position.side is Side.BUY
                else self.instrument.swap_short
            )
            if points:
                per_night = self.instrument.value_of(
                    points * self.instrument.tick_size, position.volume
                )
                position.swap += per_night * nights

    def _update_excursions(self, position: Position, bar: Bar) -> None:
        if position.side is Side.BUY:
            best = bar.high - position.entry_price
            worst = bar.low - position.entry_price
        else:
            best = position.entry_price - bar.low
            worst = position.entry_price - bar.high
        position.mfe = max(position.mfe, best)
        position.mae = min(position.mae, worst)

    def _exit_within_bar(self, position: Position, bar: Bar) -> tuple[ExitReason, float] | None:
        """Decide whether the bar's range triggered the stop or the target.

        Prices are compared in the currency the position exits in: a long exits
        at bid (the bar's own prices), a short exits at ask (bar + spread).
        """
        if position.sl is None and position.tp is None:
            return None

        offset = self._spread(bar) if position.side is Side.SELL else 0.0
        low = bar.low + offset
        high = bar.high + offset
        open_ = bar.open + offset

        # Gaps are treated asymmetrically, and deliberately so. A stop that the
        # market jumped over fills at the open, which is worse than the level —
        # that is simply what happens. A target that the market jumped over
        # still fills *at* the level, even though a real limit order would often
        # have been filled better. Booking that windfall would make results
        # depend on assumed positive slippage, and a backtest should never be
        # flattered by something the broker is under no obligation to give you.
        if position.side is Side.BUY:
            hit_sl = position.sl is not None and low <= position.sl
            hit_tp = position.tp is not None and high >= position.tp
            sl_price = min(position.sl, open_) if hit_sl else None
        else:
            hit_sl = position.sl is not None and high >= position.sl
            hit_tp = position.tp is not None and low <= position.tp
            sl_price = max(position.sl, open_) if hit_sl else None
        tp_price = position.tp if hit_tp else None

        if not hit_sl and not hit_tp:
            return None
        if hit_sl and not hit_tp:
            return ExitReason.STOP_LOSS, sl_price - self._slippage * position.side.sign
        if hit_tp and not hit_sl:
            return ExitReason.TAKE_PROFIT, tp_price

        # Both levels sit inside the bar; the bar cannot say which came first.
        if self.config.intrabar == "optimistic":
            return ExitReason.TAKE_PROFIT, tp_price
        if self.config.intrabar == "ohlc":
            # Assume O->H->L->C on an up bar and O->L->H->C on a down bar.
            up_bar = bar.close >= bar.open
            high_first = up_bar
            if position.side is Side.BUY:
                return (
                    (ExitReason.TAKE_PROFIT, tp_price)
                    if high_first
                    else (ExitReason.STOP_LOSS, sl_price)
                )
            return (
                (ExitReason.STOP_LOSS, sl_price)
                if high_first
                else (ExitReason.TAKE_PROFIT, tp_price)
            )
        return ExitReason.STOP_LOSS, sl_price

    # ----------------------------------------------------------------- closing

    def close_position(
        self,
        position: Position,
        bid: float,
        time: datetime,
        reason: ExitReason = ExitReason.STRATEGY,
        bar: Bar | None = None,
    ) -> Trade:
        """Close at a market price. Longs get bid, shorts pay ask."""
        if position.side is Side.BUY:
            price = bid - self._slippage
        else:
            price = self.ask(bid, bar) + self._slippage
        return self._close(position, self.instrument.round_price(price), time, reason)

    def close_all(
        self,
        bid: float,
        time: datetime,
        reason: ExitReason = ExitReason.STRATEGY,
        bar: Bar | None = None,
    ) -> list[Trade]:
        return [self.close_position(p, bid, time, reason, bar) for p in list(self.positions)]

    def _close(self, position: Position, price: float, time: datetime, reason: ExitReason) -> Trade:
        delta = (price - position.entry_price) * position.side.sign
        gross = self.instrument.value_of(delta, position.volume)
        self.balance += gross - position.commission + position.swap
        self.positions.remove(position)

        trade = Trade(
            id=position.id,
            side=position.side,
            volume=position.volume,
            entry_time=position.entry_time,
            entry_price=position.entry_price,
            exit_time=time,
            exit_price=price,
            reason=reason,
            sl=position.sl,
            tp=position.tp,
            gross_pnl=gross,
            commission=position.commission,
            swap=position.swap,
            bars_held=position.bars_held,
            mae=position.mae,
            mfe=position.mfe,
            balance_after=self.balance,
            tag=position.tag,
        )
        self.trades.append(trade)
        return trade

    def _check_stop_out(self, bar: Bar) -> list[Trade]:
        """Liquidate when equity can no longer support the open margin."""
        used = self.margin_used()
        if used <= 0 or self.equity >= used * self.config.stop_out_level:
            return []
        trades = self.close_all(bar.close, bar.time, ExitReason.MARGIN_CALL, bar)
        self.stopped_out = True
        self._mark_to_market(bar.close, bar)
        return trades

    # ------------------------------------------------------------- accounting

    def _mark_to_market(self, bid: float, bar: Bar | None = None) -> None:
        open_pnl = 0.0
        for position in self.positions:
            exit_price = bid if position.side is Side.BUY else self.ask(bid, bar)
            delta = (exit_price - position.entry_price) * position.side.sign
            open_pnl += self.instrument.value_of(delta, position.volume) - position.commission
        self.equity = self.balance + open_pnl

    def record_equity(self, bar: Bar) -> EquityPoint:
        self._mark_to_market(bar.close, bar)
        point = EquityPoint(
            time=bar.time,
            balance=self.balance,
            equity=self.equity,
            open_positions=len(self.positions),
        )
        self.equity_curve.append(point)
        return point

    def finalize(self, bar: Bar) -> list[Trade]:
        """Close anything still open at the end of the data."""
        trades = self.close_all(bar.close, bar.time, ExitReason.END_OF_DATA, bar)
        self.pending.clear()
        self._mark_to_market(bar.close, bar)
        return trades
