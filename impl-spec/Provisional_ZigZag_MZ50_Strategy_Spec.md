# Provisional ZigZag Candidate → MZ50 Strategy

## Implementation specification for causal backtesting

### 1. Objective

Implement a strategy that builds a Margin Zone (MZ) from the **current provisional ZigZag candidate**, enters when price makes a confirmed crossing of `MZ50` toward `MZ100`, and exits at `MZ100` or at an equal-distance stop.

Run the backtest in two modes:

1. `KEEP_OPEN`: a later extension of the candidate does not modify an open trade.
2. `CLOSE_ON_CANDIDATE_UPDATE`: a later adverse extension of the candidate closes the open trade at the first executable price after that update becomes known.

The implementation must be point-in-time correct: no final ZigZag pivot, final anchor, or later zone value may be applied to earlier observations.

---

## 2. Existing components and source of truth

Reuse without changing:

- the existing `ZigZagTracker` confirmation algorithm;
- the existing FMZ/IMZ Margin Zone distance calculation;
- the existing valid CFD rollover-point dataset and continuity rules;
- the existing instrument price, spread, commission and slippage model.

This specification changes only:

- which ZigZag state is used as the MZ anchor;
- how provisional zone versions are created and invalidated;
- how trades react to a later candidate update.

---

## 3. Definitions

### 3.1 Confirmed pivot

A completed ZigZag high or low emitted by `ZigZagTracker`. It has:

- `index/time`: where the extreme occurred;
- `confirm_index/confirm_time`: when the extreme became knowable.

Confirmed pivots are retained for charting and diagnostics, but confirmation is **not** required before the strategy may enter.

### 3.2 Provisional candidate

The currently known running extreme of the active ZigZag leg:

- when `direction = +1`, the tracker is searching for a high and the candidate is `(hi, hi_i)`;
- when `direction = -1`, the tracker is searching for a low and the candidate is `(lo, lo_i)`;
- when `direction = 0`, do not trade: wait until the first pivot establishes the search direction.

The candidate is known from current and past data, but it may later be extended or replaced.

### 3.3 Candidate update

A candidate update occurs only when its **price is extended strictly**:

```text
searching for high: new_high > previous_candidate_price
searching for low:  new_low  < previous_candidate_price
```

An equal high/low may move the ZigZag candidate index to the later bar, as required by the existing ZigZag algorithm, but it does **not**:

- increment `candidate_version`;
- move the MZ price levels;
- trigger `CLOSE_ON_CANDIDATE_UPDATE`.

A pivot confirmation and the resulting switch to the opposite search direction are not an adverse update of the originating candidate.

### 3.4 Zone version

Each initial candidate or strict candidate-price update creates a new immutable `zone_version`. A change in `dFMZ`, `dIMZ`, contract basis, or another input that changes any MZ price level also creates a new zone version even when the candidate itself is unchanged:

```text
zone_id
candidate_leg_id
candidate_version
candidate_kind          high | low
anchor_price
anchor_index/time
known_index/time        when this version became available to the strategy
mz0
mz50
mz100
valid_from_time
invalidated_time        nullable
```

Rules:

- A zone version never exists before `known_time`.
- A new candidate version or a change in zone inputs invalidates the previous untriggered zone version for new entries.
- Historical observations must never be recalculated using a later anchor.
- A trade keeps a permanent reference to the exact zone version that created it.
- Candidates that never become confirmed pivots must remain in the backtest history.

---

## 4. Margin Zone calculation

Let `dFMZ` and `dIMZ` be the existing near- and far-boundary distances for the relevant instrument and date.

### From a provisional high `H`

```text
MZ0   = H - dFMZ
MZ50  = H - (dFMZ + dIMZ) / 2
MZ100 = H - dIMZ
```

The permitted trade direction is `SHORT`, from `MZ50` toward `MZ100`.

### From a provisional low `L`

```text
MZ0   = L + dFMZ
MZ50  = L + (dFMZ + dIMZ) / 2
MZ100 = L + dIMZ
```

The permitted trade direction is `LONG`, from `MZ50` toward `MZ100`.

All three levels are frozen within a zone version. Recalculation creates a new version; it must not mutate the old one.

---

## 5. Point-in-time processing

### 5.1 ZigZag update timing

`ZigZagTracker` consumes one closed H4 bar at a time.

A candidate or candidate update discovered from H4 bar `i` becomes known only at that bar's close. The resulting zone version:

- receives `known_time = H4[i].close_time`;
- may be used only by crossing observations strictly after `known_time`;
- may not create a signal on H4 bar `i` using that bar's earlier high/low.

If a lower-timeframe or tick event stream is available, merge events chronologically. A zone may act only on price events after the event that created it.

### 5.2 Event ordering at the same timestamp

For equal timestamps, process in this order:

1. Complete any already-scheduled market exit.
2. Process TP/SL and crossing observations using the previously active zone state.
3. Close the H4 bar and update `ZigZagTracker`.
4. Create/invalidate provisional zone versions.
5. Schedule any `CLOSE_ON_CANDIDATE_UPDATE` exit for the first executable price strictly after the update time.

This prevents the completed H4 bar from generating a retroactive trade or exit inside itself.

---

## 6. True MZ50 crossing

Use the project's existing valid CFD rollover points as the default crossing observation stream:

```text
CrossObservation(time, price, valid, continuity_key)
```

For every active `zone_version`, keep a separate previous valid observation. A crossing is allowed only when:

- both observations are valid and consecutive under the existing rollover continuity rules;
- no missing rollover point lies between them;
- both observations occurred while the same immutable `zone_version` was active;
- both prices are strictly on opposite sides of that version's unchanged `MZ50`;
- equality with `MZ50` is neutral and is not a crossing.

When the candidate or zone version changes, reset the crossing baseline. Never compare a price classified under the old version with a price classified under the new version.

### SHORT signal from a provisional high

```text
previous_price > zone.mz50
current_price  < zone.mz50
```

### LONG signal from a provisional low

```text
previous_price < zone.mz50
current_price  > zone.mz50
```

Crossings from `MZ100` back toward `MZ0` are ignored.

### Entry price and time

The signal becomes knowable at the current rollover observation.

```text
entry_time  = current_observation.time
entry_price = first executable market price at or after entry_time
```

Do not backfill the entry at `MZ50` unless lower-timeframe/tick data explicitly proves a tradable crossing after the zone version became active.

Skip the entry if the market is already at or beyond the target:

```text
SHORT: entry_price <= mz100
LONG:  entry_price >= mz100
```

Record the skipped signal as `entry_beyond_target`.

---

## 7. Initial trade levels

At entry, copy all values from the originating zone version into the trade. They become immutable trade fields.

### SHORT

```text
take_profit = origin_zone.mz100
risk        = entry_price - take_profit
stop_loss   = entry_price + risk
```

Require `risk > 0`.

### LONG

```text
take_profit = origin_zone.mz100
risk        = take_profit - entry_price
stop_loss   = entry_price - risk
```

Require `risk > 0`.

Therefore the initial reward-to-risk ratio is exactly `1:1`, calculated from the actual fill price.

No later change in candidate, MZ distances, rollover date or confirmed-pivot state may move the stored TP or SL.

---

## 8. Position constraints

- Allow at most one open position per instrument.
- Do not pyramid or average.
- A zone version may create at most one trade.
- Ignore new entry signals while a position is open.
- After any trade exit, reset the crossing baseline. A new position requires a fresh valid 0%-side → 100%-side crossing occurring after the exit.
- Never create a trade retroactively from a crossing that occurred while another position was open.

---

## 9. Backtest variant A — `KEEP_OPEN`

Purpose: test the original trade as an immutable decision made from information available at entry.

After entry:

- later strict extensions of the originating candidate do not close the trade;
- candidate updates create new zone versions for diagnostics/future eligibility only;
- the open trade keeps its original entry, TP, SL and `origin_zone_id`;
- new signals are ignored until the trade exits;
- the trade exits only by TP, SL, or end-of-data handling.

Pseudocode:

```text
on_candidate_update(event):
    invalidate_previous_entry_zone()
    create_new_zone_version(event)

    if open_trade exists:
        do nothing to open_trade
```

Required exit reasons:

```text
take_profit
stop_loss
end_of_data
```

---

## 10. Backtest variant B — `CLOSE_ON_CANDIDATE_UPDATE`

Purpose: treat a new adverse extreme as invalidation of the setup that created the trade.

Close an open trade only when the **originating candidate is strictly extended in the adverse direction**. The update must belong to the same `candidate_leg_id` as the trade's originating candidate:

```text
SHORT originating from high H:
    update when event.candidate_leg_id == trade.origin_candidate_leg_id
                and new candidate high > H

LONG originating from low L:
    update when event.candidate_leg_id == trade.origin_candidate_leg_id
                and new candidate low < L
```

Do not close because of:

- an equal high/low with a later index;
- normal pivot confirmation without an extension of the originating extreme;
- a new opposite-leg candidate created after confirmation;
- a change in FMZ/IMZ data alone.

The update becomes known only when `ZigZagTracker` processes the closed H4 bar containing it. Close at the first executable market price strictly after that time:

```text
exit_reason = candidate_update
exit_time   = first_execution_time > candidate_update.known_time
exit_price  = market_price(exit_time)
```

Do not fill at the candidate high/low unless the execution data proves that the strategy could act at that price in real time.

Pseudocode:

```text
on_candidate_update(event):
    invalidate_previous_entry_zone()
    create_new_zone_version(event)

    if open_trade exists
       and event extends open_trade.origin_candidate adversely:
        schedule_market_exit(
            after=event.known_time,
            reason="candidate_update"
        )
```

Required exit reasons:

```text
take_profit
stop_loss
candidate_update
end_of_data
```

After the forced exit:

- keep the newly created zone version active;
- reset its crossing baseline at the exit time;
- require a completely new post-exit crossing before re-entry.

---

## 11. TP/SL execution and ambiguous bars

Use the smallest available timeframe or tick data to determine the first touched exit.

If only OHLC is available and one bar touches both TP and SL with unknown intrabar order:

- mark the trade `ambiguous_tp_sl = true`;
- use the conservative assumption `SL first` for the primary result;
- optionally report `TP first` as a sensitivity result;
- never silently choose the profitable outcome.

For variant B, if TP or SL is reached before the candidate-update exit becomes executable, TP/SL wins. If the scheduled candidate-update exit is executable first, close at that market price and ignore subsequent movement.

---

## 12. Required records

### Candidate/zone record

```text
instrument
candidate_leg_id
candidate_version
candidate_kind
candidate_price
candidate_index/time
known_index/time
zone_id
mz0, mz50, mz100
valid_from_time
invalidated_time
event_type                 initial | strict_extension | zone_input_change | confirmation_flip
eventually_confirmed       boolean, diagnostic only
confirmed_pivot_id         nullable, diagnostic only
```

`eventually_confirmed` must never be used as an entry filter.

### Signal record

```text
signal_id
instrument
zone_id
direction
previous_observation_time/price
current_observation_time/price
signal_time
status                     entered | ignored_open_trade | entry_beyond_target | invalid
```

### Trade record

```text
trade_id
backtest_variant           KEEP_OPEN | CLOSE_ON_CANDIDATE_UPDATE
instrument
direction
origin_zone_id
origin_candidate_leg_id
origin_candidate_version
origin_anchor_price/time
entry_time/price
initial_tp
initial_sl
initial_risk
exit_time/price
exit_reason
pnl_points
pnl_currency
r_multiple
mae
mfe
bars_held
candidate_updates_while_open
first_candidate_update_time/price
ambiguous_tp_sl
```

---

## 13. End-of-data handling

Do not invent a completed TP/SL result for an open trade at the end of the dataset.

For reporting:

- mark it `exit_reason = end_of_data`;
- value it at the final executable market price;
- report results both including and excluding end-of-data trades.

The unfinished final ZigZag candidate remains provisional and must not be converted into a confirmed pivot.

---

## 14. Mandatory invariants

The implementation must assert:

1. `zone.valid_from_time >= candidate.known_time`.
2. `signal_time > zone.known_time`.
3. Both crossing observations refer to the same `zone_id`.
4. No historical zone level changes after creation.
5. `trade.origin_zone_id` never changes.
6. TP and SL never change after entry.
7. `abs(TP - entry) == abs(entry - SL)` within price precision.
8. Candidate confirmation status is never used retroactively.
9. At most one open trade exists per instrument.
10. Variant A never exits because of a candidate update.
11. Variant B exits only on a strict adverse extension of the originating candidate.

---

## 15. Minimum tests

1. **No lookahead:** a zone created at H4 close cannot signal on an earlier part of that H4 bar.
2. **Strict update:** a higher high creates a new high-candidate version; an equal high only updates the ZigZag index.
3. **No cross-version crossing:** observations on opposite sides of two different MZ50 versions do not form a signal.
4. **Correct SHORT:** high candidate, downward MZ50 crossing, TP at MZ100, symmetric SL.
5. **Correct LONG:** low candidate, upward MZ50 crossing, TP at MZ100, symmetric SL.
6. **Variant A:** a later adverse candidate extension leaves entry, TP and SL unchanged.
7. **Variant B:** the same extension schedules a market exit at the next executable price.
8. **Confirmation is not update:** ordinary pivot confirmation does not force a variant-B exit.
9. **No retroactive deletion:** a trade remains in results even if its candidate never becomes a confirmed pivot.
10. **No immediate re-entry:** after any exit, a fresh post-exit crossing is required.
11. **Target already passed:** a signal occurring at or beyond MZ100 is skipped.
12. **Ambiguous bar:** when both TP and SL are touched, the primary OHLC-only result uses SL first and flags the trade.

---

## 16. Backtest output comparison

Run both variants on identical data, candidate events, zone versions and entry signals. The only intended difference is the open-trade reaction to a strict adverse candidate extension.

Report side by side:

```text
total signals
entered trades
ignored signals while open
candidate-update exits
TP count
SL count
win rate
net P&L
average R
profit factor
maximum drawdown
average holding time
percentage of trades whose candidate was updated while open
percentage of origin candidates eventually confirmed
```

Any difference in entry count before the first candidate-update exit indicates an implementation error rather than a strategy difference.
