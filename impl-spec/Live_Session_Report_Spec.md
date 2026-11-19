# Live session report: the paper account and its page

**Status:** implemented on `margin-zones-streaming`, except §7 (H1 stepping), which is open.
**Answers:** `Strategy_Paper_Trading_H1_Spec.md` — §6 maps each of its points to what is built.

## 1. Purpose

A live runner (`scripts/run-live.sh`, paper or demo) must be as readable as a
backtest. You should see the chart, the zones, the orders and the account
state, plus what the strategy did since it started, at a permanent link that
stays current with no one at the keyboard.

## 2. Principle: the report is the backtest's own

The live runner steps the backtest engine on each closed bar. The strategy
object therefore holds exactly what a backtest over the same bars would hold.
The session report is built from that same object, by the same code that
`run_backtest.py --save` uses:

- `Backtester.snapshot()` returns the result so far. It does not call
  `on_finish` and does not force-close open positions.
- `write_run()` writes `summary.json`, `trades.csv`, `equity.csv` and the
  strategy's tables. `casebook.write()` adds the casebook.
- `strategy.chart()` draws the strategy's own chart (the zone chart for mz50).
  A strategy without one gets the default trade chart.

The live page and the backtest cannot disagree, because nothing is computed
twice.

## 3. The session's account

- **Opening.** The account opens at the first bar the runner sees open after
  it first starts. `runs-live/<session>/session.json` records this as
  `live_from`, together with `initial_balance` (taken from the config's
  `execution`) and `created_at`.
- **What counts.** Only the strategy's entries with `entry_time >= live_from`
  count. Positions and orders the replay already held when the session
  started stay on the chart, faded and marked `in_session: false`.
- **Figures.**
  - balance = initial + realised net P&L of the counted closed trades
  - equity = balance + floating P&L of the counted open positions
  - Also reported: trades, wins/losses, win rate, profit factor.
- **Restart.** Every start replays from the config's `start`, so missed bars
  are processed in order and the same `live_from` is reused. The account comes
  back identical, and nothing is counted twice.
- **Reset.** `--reset-account` deletes `session.json`, and a new account opens
  at the next bar.
- **Paper vs live.**
  - In paper mode this is the account: `state.json`'s `balance` and `equity`
    are its own.
  - In live (demo) mode they stay the MT5 account's. The session block is then
    the strategy's record since `live_from`.

## 4. The report directory

`runs-live/<session>/report/` is written again after every runner event (each
closed bar, each order event) and at shutdown:

| file | |
|---|---|
| `summary.json`, `trades.csv`, `equity.csv`, `zones.csv`, `pivots.csv`, `crossings.csv`, `signals.csv`, `cases.csv`, `warnings.csv`, `chain.csv`, `journal.csv`, `casebook*.csv`, `report.md` | what a saved backtest over the same bars contains |
| `live.json` | the live layer: `account`, `broker_account`, `positions`, `resting_orders`, `queued_orders`, `status`, `journal` (newest 200), `events` (newest 200 runner events, `bar_closed` left out), `last_closed_bar`, `forming_bar`, `bar_close_utc`, `margin`, `generated` |
| `chart.html` | the strategy's chart with `live.json` inlined |
| `pulse.json` | rewritten every poll: `updated_at`, `running`, `mode`, `last_closed_bar`, `report_generated`, `report_bar`, `report_error`, `failure` |

**How files are written.** Each file is staged in `report/.staging/` and moved
into place on its own, so a reader never sees half a file.

**Failures.** A failed report is logged once and shows as `report_error` in
the pulse. Trading carries on.

**Cost.** About 0.15 s per write for mz50 over EUR/USD H4, 2022–2026. The
chart is about 770 KB.

**Order of writes.** The report is written before the pulse, so the pulse
always names the newest report.

## 5. Strategy hooks

- **`Strategy.status() -> dict | None`**: where the strategy stands now. It
  returns at least `phase` and `label`. mz50 adds:
  - `phase`, one of `waiting_signal`, `entry_ordered`, `limit_resting`,
    `chain_waiting_cross`, `in_position`, `exit_warning`, `exit_due` or
    `no_orders`
  - `zone`: id, direction, anchor, E50, MZ0, MZ100
  - `generation` (chain depth) and `chain_id`
  - `warning`: the first adverse daily close
  - `exit_due` and the live ZigZag `candidate`
- **`journal` artifact (mz50).** One row per decision, with `time` (bar open,
  broker clock), `event`, `zone_id`, `chain_depth` and `detail`. The events are:
  - `zone_created`, with the reason: new candidate, extension or margin change,
    and which zone it replaces
  - `signal`, with the outcome: entered, or why it was not
  - `entered`
  - `entry_not_filled`
  - `two_close`: warning, confirmation or reset
  - `exit_at_open`
  - `chain`: step created, limit filled, voided, superseded or cancelled
  - `closed`

  The journal is also saved with backtests, as `journal.csv`.
- **`Strategy.chart(..., live=None)`**: takes the live layer and passes it into
  the page.

## 6. The page

The page is the backtest's chart with a shared layer on top
(`scripts/live_layer.js`, used by both chart templates). Mapped to §5 of the
H1 paper-trading spec:

| requirement | shown as |
|---|---|
| candles, ZigZag, anchors, zones MZ0–MZ100, E50 | the zone chart; a Price control switches Line/Candles, and candles are the default on a live page |
| orders, entries/exits, TP/SL | closed trades (faded before `live_from`); the open position's entry, SL and TP lines to the right edge with labels; resting limit and its void level; a queued market order past the last bar; a marker where the account opened |
| state: waiting / limit / position / first exit warning; chain generation | the Strategy card: phase badge, one-line label, zone, generation, candidate |
| balance, equity, current and realised P&L, trade history | the account card; "Open positions and orders" table; the trades table |
| time of the last processed bar; data lag | header: last bar (broker clock), how long ago it closed (UTC), heartbeat age; "next bar overdue" on weekdays; "not responding" when the heartbeat is older than 120 s |
| journal of entry, exit, zone creation and cancellation reasons | "Strategy journal" table, then "Runner events" (orders as the broker answered them) |
| auto-refresh after each bar, independent of a browser | the runner writes the files; the page polls `pulse.json` every 30 s and reloads when a newer report exists, keeping zoom and scroll |

**Serving.** The reports server in `../agents` (public, read-only, :8083)
serves sessions at:

- `/live/`: an index showing runner state, session equity and last bar
- `/live/<session>/`: redirects to the chart
- `/live/<session>/<file>`: files from `report/` only, with `Cache-Control:
  no-store`

`state.json` (which holds the account login) and everything else outside
`report/` are never served. `live.status` returns `chart_url` and the
`session_account`.

To watch a session locally:
`python3 -m http.server 8090 -d runs-live`, then open
`/<session>/report/chart.html`.

## 7. Open issue: H1 stepping (left for later)

The H1 spec asks for **H1** as the update and execution step. The ZigZag
timeframe and the daily signals stay as the strategy's settings. Today mz50
runs on **H4** throughout. This has four consequences:

- **Update rate.** The runner processes one bar every 4 hours, so the page
  updates (and orders fill) on H4 boundaries, not hourly.
- **TP/SL resolution.** Stops and targets are resolved on H4 OHLC. A 4-hour
  bar holding both resolves conservatively as the stop (SL first). That is
  correct but coarser than H1 would be.
- **Limit fills and void levels.** Limit fills and void levels are judged
  against H4 ranges. Live, the void level is also watched on every poll.
- **Rollover close.** The rollover close is the 20:00 H4 bar's close. On H1 it
  would be the 23:00 bar's close, which is the same price, so daily signals
  would not move.

**What H1 needs:**

- mz50 takes H1 bars and builds the ZigZag and zones from **completed** H4
  bars, resampled inside the strategy. A forming H4 bar must never feed the
  tracker.
- Rollover observations are read from H1 closes.
- Orders fill at the next H1 open, and brackets resolve on H1 OHLC.
- `expect_timeframe` and the config's `timeframe` change to H1.
- H1 history from the config's `start` is fetched.

The trade list will differ from today's H4 backtests, mostly where a bar
holds both the stop and the target. A new baseline backtest is therefore
needed before comparing live results.

**Acceptance (from the H1 spec).** Replaying the same H1 bars through the
backtest and the live runner gives identical events and trades, and a restart
changes nothing.

Nothing in the report or the page depends on the timeframe. At H1 the chart
would be about 4× larger (roughly 3 MB); the page already opens at the newest
bars.

## 8. Known limits

- **Live mode after the handover.** The equity curve continues from the
  replay's by the MT5 account's own changes. Trades the account made before a
  restart come back as the replay's, which the runner holds to the same
  trades.
- **"Next bar overdue".** This is a weekday heuristic. It does not know broker
  holidays.
- **Journal times.** The journal shows bar times on the broker's clock. Runner
  events are UTC wall-clock times.
- **No session-only equity curve or drawdown.** The account card shows totals
  and trade statistics.

## 9. Tests

- `tests/test_live_report.py` covers:
  - the snapshot does not finish the run
  - the report's tables equal a saved backtest's over the same bars
  - the journal's content
  - the account opens at the first bar and counts nothing earlier
  - counted trades and balance
  - a restart keeps the account with no duplicates
  - reset
  - status phases across scenarios
  - the pulse names the newest report
  - a failing report never stops trading
  - both chart pages run with the live layer (`tests/render_check.js`)
- `../agents/tests/test-live.sh` covers the `/live/` routes. A session's
  report files are served; `state.json`, paths out of the live root, and types
  outside the allow-list are refused.
