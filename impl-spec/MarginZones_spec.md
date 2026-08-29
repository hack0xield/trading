# Margin Zones — assumptions and known problems

What the implementation takes on faith, and where it is weaker than the
specification asks. The requirements themselves are in `MarginZones_revised.md`
(base), `MarginZones_revised_with_CFD_rollover_crossings.md` (§3.5, §5, §5.5)
and `Provisional_ZigZag_MZ50_Strategy_Spec.md` (the candidate anchor).

## `rollover_hour` / `rollover_tz` are set, not read from the broker

§5.1 says `T_roll` "must be determined from the trading-session schedule of the
specific CFD symbol and broker" and "must not be hardcoded." It is a config
value instead. Checked against the installed `MetaTrader5` package (v5.0.6090)
in the Wine prefix:

- `symbol_info_session_quote()` / `symbol_info_session_trade()` — the MQL5 calls
  carrying the weekly break schedule — **do not exist** in the Python wrapper
  (`AttributeError` on call).
- `symbol_info()`'s ~90 fields include `session_open` / `session_close`, but
  those are today's session *prices*, not break times.

So there is no way to ask the terminal when a symbol's break starts.
`rollover_hour: 0, rollover_tz: UTC` — broker midnight, since bar stamps are
broker-server time labelled UTC — is a default, not a verified fact. If the real
break is at another hour, every rollover point reads the wrong bar, and every
crossing inherits that error.

## The rollover price is a bar close, never a tick

§5.2's fallback ("if tick data is unavailable... use the Close price of the
final available bar"). This codebase stores no tick data at all. On H4 with
`rollover_hour: 0` the 20:00 bar closes exactly at the break, so the close is
the pre-break price; at any other hour the point is as stale as the timeframe.

## Margin readings only exist for 6E

`configs/contracts/` holds specs for `6E`, `6B` and `GC`, but
`data/margins/margins.csv` has readings for **`6E` only**. A candidate with no
covering reading produces no zone, so zones cannot be drawn for a `GC`-backed
instrument (XAUUSD) or a `6B`-backed one (GBPUSD). Missing input data rather
than a code gap — CME margin history is entered by hand, since CME blocks
scripted access — but it makes the strategy single-instrument in practice.

## "Trading day" is data-driven, not a broker calendar

A day gets no rollover point either because no bar precedes its `T_roll`, or
because the candidate bar is the one the previous point already used (the
weekend case). No holiday or session calendar is consulted, so an unusual
closure with a stray bar around it could produce a point where a real calendar
would not, or the reverse. Correct for the ordinary weekly gap; unverified
against holidays.

## The crossing gap rule is a threshold, not a calendar

§5.5.4 forbids inferring a crossing across a missing scheduled point but gives
no number. `max_gap_days` (default 3) stands in: a normal weekend collapses to a
2-3 day span between consecutive output points, so that width is allowed and
anything wider resets the baseline. Same root cause as the item above.

## Rollover points are not in the custom hover tooltip

The dots are drawn, but hovering near one shows whichever bar the crosshair
snaps to, not the point's own day and price. Crossings are better served: they
carry a native SVG `<title>`, and the custom tooltip matches them by calendar
day. The two paths are not unified.

## The Telegram crossing alert has no cross-run memory

"Just happened" means only that the newest rollover point is the second half of
a crossing pair, recomputed on every `evaluate()` with nothing persisted. The
alert therefore repeats on every run of the job while that stays true, rather
than firing once. Edge-triggered behaviour would need a state file recording the
last-alerted day.
