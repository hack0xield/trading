# Backtesting infrastructure

A bar-by-bar backtester for MetaTrader 5 symbols, plus the data plumbing around
it. The core engine imports **nothing outside the standard library**, so it
runs on any Python 3.10+ — including the Wine interpreter that hosts the
`MetaTrader5` package. Storage backends pull in pyarrow or SQLAlchemy only when
you actually use them.

```bash
pip install -r requirements.txt          # or: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# No MT5 data yet? Generate a synthetic series and run the whole pipeline now.
scripts/make_synthetic.py --symbol XAUUSD --timeframe M15 --start 2022-01-01 --end 2024-12-31
scripts/run_backtest.py --config configs/strategies/day_open_xauusd.yaml
```

## Layout

```
backtester/
  core/         types, instrument specs, broker simulation, the run loop
  data/         bar storage: csv / parquet / sql behind one interface  (see data/README.md)
  indicators/   strategy-agnostic indicators (ZigZag)
  strategies/   strategy implementations + the name registry
  live/         the same strategy trading an MT5 account, bar by bar
  metrics/      performance statistics, the casebook, report rendering
  utils/        timeframes, time parsing, typed parameters
  cli.py        argument plumbing shared by the scripts
scripts/        fetch, backtest, optimize, manage data, make synthetic data
configs/        run configs (YAML) and per-symbol contract specs
tests/          422 tests, ~7s
data/           bar store (gitignored)
runs/           saved backtest results (gitignored)
```

## The mock strategy

From `strategy.txt`: buy at the start of each day, 2% stop, 2% target. It lives
in [backtester/strategies/day_open.py](backtester/strategies/day_open.py) and
is configured by [configs/strategies/day_open_xauusd.yaml](configs/strategies/day_open_xauusd.yaml).

```bash
scripts/run_backtest.py -S day_open -s XAUUSD -t M15 \
    -p volume=0.1 -p stop_pct=2 -p take_pct=2 --save
```

It is deliberately trivial, which makes it a useful reference: with a symmetric
stop and target, P&L before costs should land near a coin flip, so the gap
between that and the actual result *is* the cost of trading. On the synthetic
series above it turns in 114 trades at a 51.8% win rate, a 1.00 profit factor
and -$86 — which is -$1,056 of swap partly offset by a small directional edge.
That is the null hypothesis behaving exactly as it should.

## The margin-zone strategy

`impl-spec/[1 Sep5]E50_NearTP_OriginalSL_TwoCloseExit_Spec.md`, as one forward
pass.

```bash
scripts/run_backtest.py --config configs/strategies/mz50.yaml --save
```

The Margin Zone hangs off the ZigZag **candidate** — the running extreme of the
leg in progress — not off a confirmed pivot. The candidate is knowable from
closed bars alone, so nothing reads the future; what it is not is *final*. Every
strict extension of it freezes a new immutable zone version with its own levels,
its own `known_time` and its own crossing baseline, and a crossing counts only
when both daily observations were measured against the same version. That is
what stops a level sliding under a static price and registering as a crossing
price never made.

**Entry** is a True crossing of `E50` — the 50% Extremum-to-50% MZ level,
`anchor ± (dFMZ + dIMZ) / 4` — toward the zone, by two consecutive daily
clearing closes.

**Target** is `MZ0`, the near boundary. **Stop** is `2 × entry − MZ100`, which
keeps the distance to the *far* boundary, so the planned reward-to-risk sits
below 1 by construction and is deliberately not restored by moving the stop.

**Early exit.** Two consecutive daily closes back past the trade's *own*
originating `E50`, on the anchor's side, close the position at the next
executable price (`e50_two_close_return`). The first close warns, the second
confirms. A close on the zone's side, a close exactly on `E50`, or a break in
the daily sequence clears the warning. The bracket keeps working throughout: a
target or stop reached first wins and cancels the pending exit.

`place_orders: false` runs the same pass with the trading rule off: the ZigZag,
the zone versions, the daily points and their True/False crossings are recorded
and drawn, and nothing is ordered.

`--save` writes the chart alongside `zones.csv`, `pivots.csv`, `rollover.csv`,
`crossings.csv`, `signals.csv`, `cases.csv` and `warnings.csv` — the last being
every warning, reset and confirmation, not just the pair that fired. Every row
was knowable at the timestamp it carries. A zone is drawn from the bar that made
it knowable, not back to the extreme it names, and each candidate is joined to
the pivot that opened its leg by a dashed line, so the repainting is visible
rather than hidden.

Strategies that do not override `Strategy.chart` get the generic
price-and-trades chart from `scripts/plot_run.py` instead, and `--no-chart`
skips it.

### The comparison runs

§12's three runs, from the one config. EUR/USD H4, 2% deviation, 2022-2026,
contract `6E`: 831 zone versions on 64 confirmed pivots, 1,205 daily points.

```bash
scripts/run_backtest.py -c configs/strategies/mz50.yaml --label A --save \
    -p take_profit=mz100 -p two_close_exit=false
scripts/run_backtest.py -c configs/strategies/mz50.yaml --label B --save \
    -p take_profit=mz0 -p two_close_exit=false
scripts/run_backtest.py -c configs/strategies/mz50.yaml --label C --save
```

| | A — far TP | B — near TP | C — near TP + exit |
|---|---:|---:|---:|
| entered trades | 78 | 79 | 79 |
| planned RR | 1.00 | 0.77 | 0.77 |
| take-profit exits | 36 | 42 | 35 |
| stop-loss exits | 42 | 37 | **18** |
| two-close exits | — | — | **26** |
| win rate | 46.2% | 53.2% | 45.6% |
| profit factor | 0.82 | 0.79 | **0.83** |
| net P&L | -693.77 | -758.83 | **-482.35** |

The near target does what it should in isolation — the hit rate climbs from
46.2% to 53.2% — but B is *worse* overall, because 0.77 RR costs more than the
extra hits pay. C is where the pieces work together: the early exit converts 19
of the 37 stop-losses into smaller ones, cutting the loss by 30% at a barely
changed win rate.

All three are still negative. At 0.1 lot a dollar is a pip, so C is about -482
pips over 79 trades. The specification's own note applies: the -127.4 pip HTML
estimate is not an acceptance benchmark, since it used fixed entries and proxy
fills.

**One departure to know about.** §5 requires daily-close continuity from the
instrument's session calendar and explicitly forbids a fixed window. There is no
trading calendar in this project, so `max_gap_days` (default 3) stands in, over
*emitted* daily points rather than clock hours — a weekend already collapses to
one skipped day by construction. Every crossing and every warning reset inherits
that approximation.

### Following the chain after a take-profit

`impl-spec/[2 Sep5]E50_NearTP_OriginalSL_TwoCloseExit_Spec+TrendFollowLogic.md`
§14, behind `trend_follow: true`.

Every take-profit — and only a take-profit — opens a follow-on zone anchored on
**that trade's own E50**, in the same direction, inheriting its `dFMZ`/`dIMZ`
frozen rather than re-reading the margin log. A stop, an early exit or the end
of the data ends the chain instead.

Where price sits when the step is created picks its entry, once:

| price at creation | what happens |
|---|---|
| between the new E50 and the new MZ0 | a limit rests at the new E50 |
| at the new E50, or on the anchor's side | wait for two daily closes to cross it |
| at the new MZ0 or past it | the branch ends, `chain_target_already_reached` |

A resting limit is voided if price reaches the new MZ0 first, and a bar touching
both resolves as the cancellation, since its intrabar order is unknown. One
position and one pending step at a time: a fresh admissible ZigZag signal
supersedes a step still waiting, and an open child blocks ordinary signals until
it exits.

```bash
scripts/run_backtest.py -c configs/strategies/mz50.yaml --label D --save \
    -p trend_follow=true
```

| | C | D — with the chain |
|---|---:|---:|
| entered trades | 79 | 100 |
| take-profit exits | 35 | 46 |
| two-close exits | 26 | 31 |
| stop-loss exits | 18 | 23 |
| win rate | 45.6% | 47.0% |
| profit factor | 0.83 | **0.87** |
| net P&L | -482.35 | **-456.12** |

The chain created 46 steps and traded 28 of them — 11 through a resting limit,
17 through a daily crossing — reaching depth 3. Of the rest, 15 were superseded
by a fresh ZigZag signal and 3 saw price reach the new near boundary before the
limit could fill.

```
zigzag  72 trades   net -413.75
chain   28 trades   net  -42.37     depth 1: 21   depth 2: 6   depth 3: 1
```

So D is not C plus a separate book of winners: the chain's own 28 trades lose
too, just far less per trade, and they displace some ordinary entries. The
specification's warning applies — the "+192.2 pips over 32 trades" estimate came
from proxy fills against a fixed trade list, and a full re-run changes both the
count and the result.

## The casebook

`--save` also writes the Backtest Database and its views, per
`impl-spec-old/Claude Specification_ Backtest Data Collection and Reporting.md`:

    casebook.csv                 one row per trade, the specification's columns
    casebook_total_statistics.csv
    casebook_direction.csv       Long / Short
    casebook_models.csv          the strategy name
    casebook_sessions.csv        Asia / London / New York / ...
    casebook_weekdays.csv
    casebook_months.csv
    report.md                    the summary, built from the table above

The six tables are *views* over `casebook.csv`, never computed separately, so
they cannot disagree with it or with each other — a test asserts every one of
them totals the database.

Three things the specification leaves open, decided here:

* **Break-even** is any case returning within `--be-threshold` R of flat
  (default 0.1). The document uses BE for 17% of its own cases and never
  defines it. It matters: winrate is `W / (W + L)` with BE **excluded** from
  the denominator, and on the reference data the same 184 cases score 78.95%
  that way against 64.17% counted over all trades.
* **Models** carries the strategy name. In the reference book it is a
  discretionary label (BOS, Inversion, Engulfing) chosen by eye.
* **Sessions** keep the specification's five windows and fill the twelve hours
  they miss with buckets named after themselves (`18:00-03:00`), so every
  trade is classified. Hours are read off the bar stamps, which are
  broker-server time — this broker's clock follows New York's daylight saving,
  so sessions sit at stable broker-local hours and wobble in UTC.

The five review columns (`BE Reason`, `News Event`, `Mistake`, `To Improve`,
`Needs validation`) are written empty. No backtest can fill them.

## Trading it live

`scripts/run-live.sh` trades a run config on the MT5 account until stopped. It
takes the backtest's own config, and the strategy object is the backtest's own,
unchanged.

```bash
scripts/run-live.sh --config configs/strategies/mz50.yaml            # a demo account
scripts/run-live.sh --config configs/strategies/mz50.yaml --paper    # sends nothing
```

It runs under the Wine Python, needs the terminal logged in with Algo Trading
on, and refuses a real-money account unless given `--allow-real`.

**Startup replays history.** Every bar from the config's `start` goes through
the backtest engine, so the strategy reaches today holding exactly the zones,
crossings and chain the backtest holds. Then, on every new H4 bar, the
engine's six steps run in the engine's order, split across the bar boundary:

| when | what |
|---|---|
| the bar closes | read back stops, targets and limit fills; `on_trade`; `on_bar` |
| the next bar opens | send queued orders; `on_bar_open`; send what it queued |

Orders therefore fill at the next open, as in the backtest. The account's server
holds each position's stop and target. A limit order's void level is watched
from this side, on every poll and against each closed bar.

**Shadow, then live.** The runner trades live only when the account holds what
the replay holds. Until then it is in *shadow*: the backtest keeps stepping live
bars and nothing is sent. A replayed position the account lacks is never opened
late; the runner waits for the replay to close it and hands over at the next
open. Positions and orders carrying the runner's magic number are adopted when
they match the replay (the same side and tag), and closed or cancelled when
they do not. That makes a restart safe. Anything without the magic number is
never touched.

Stopping leaves positions under their server-side stops and removes resting
limit orders, whose void level needs the process.

**One account.** The runner records the login it started on. Before every
send (entry, limit, stop move, close, removal), and every time it reads the
account back, it checks the terminal is still on that login. If not, nothing is
sent, an `error` carries `expected_login` and `actual_login`, and the runner
treats the terminal as unavailable: queued orders stay queued and the run loop
logs back in.

**What the broker answers.** Every request comes back one of four ways,
read from its `TRADE_RETCODE_*`:

| answer | retcodes | what happens |
|---|---|---|
| done | `DONE`, `PLACED` | the order is tracked |
| not now: certainly not carried out, may succeed shortly | `MARKET_CLOSED`, `PRICE_OFF`, `REQUOTE`, `PRICE_CHANGED`, `TOO_MANY_REQUESTS`, `TRADE_DISABLED`, `SERVER_DISABLES_AT`, `CLIENT_DISABLES_AT`, `FROZEN` | kept and sent again, no sooner than `--retry-seconds` (default 15) |
| possibly carried out | `TIMEOUT`, `CONNECTION`, `LOCKED`, `DONE_PARTIAL`, and no answer at all | looked for at the broker, by magic number and tag, among positions, resting orders and recent deals, and adopted if there; otherwise sent again on the next poll |
| refused | everything else: invalid stops, no money, invalid volume, … | an immediate `order_rejected` |

mz50 decides at the daily rollover, so its orders go out at 00:00 on the
broker's clock, often before the trade session has opened; "market closed"
then is routine. A kept entry carries the broker's answer as its `problem`
and is sent again within its bar, with the usual checks each time: a limit
whose void level was reached is cancelled, a limit the market already reached
fills at market, and a stop or target already passed is refused. Its
`order_intent` goes out once. One still unsent when its bar ends is rejected,
the reason naming the last answer. Closes, stop moves and removals are kept
the same way until they go through. Answers that need someone to act (Algo
Trading off, automated trading disabled by the broker, trading disabled for
the symbol) raise one retrying `error` naming the cause; any other "not now"
raises nothing.

A position or order under the magic number that the runner does not track is
reported as an `error`, once, and left alone.

The login is tried first from a separate process, `--connect-timeout` seconds
at most (default 120): a terminal that hangs holds the Python interpreter lock
for as long as it hangs, so nothing inside the runner could time it out. A
terminal that freezes mid-run freezes the runner with it; `state.json` then
stops updating, which is how to tell.

**Startup failures are written down.** Once the config names the strategy and
symbol, the session directory exists, and any failure after that (terminal not
answering or not logged in, Algo Trading off, a real account refused, the
instrument's digits disagreeing with the broker's, no bars, bad bars, anything
raised during the replay) leaves an `error` event with `retrying: false` and a
`state.json` with `running: false` and the reason in `failure`, then exits 1.
A config that cannot be read, or an unknown strategy, exits 1 with the reason on
stderr and no session.

**Margin data.** A margin reading older than `--margin-stale-days` (default 30)
is reported as stale in `started` and `state.json`, and does not stop the
runner. The log is read again whenever it changes, so adding a reading clears
the warning without a restart.

**Events.** Every order is announced before it is sent (`order_intent`,
`exit_intent`) and again when the broker answers. Events go to the console and
to `events.jsonl`. In code, a listener is any callable taking an `Event`:

```python
runner.notify.add(lambda event: print(event.kind, event.data))
```

An `error` the runner carries on through is delivered once, not again while the
same message repeats within five minutes.

### The session, as reporting reads it

Written for the Telegram assistant in `../agents`, which starts and stops
runners and reports on them. This is what both repositories rely on.

**Where.** `runs-live/<strategy>_<symbol>_<magic>/`, holding `events.jsonl` and
`state.json`. Sessions are found by scanning `runs-live/*/state.json` and
matching `config`. The magic number defaults to a hash of strategy and symbol,
so a restart finds its own session.

**Stopping.** SIGTERM to `run-live.sh` (Ctrl-C does the same). It creates the
stop file, and the runner withdraws resting orders, emits `stopped` and writes
its final `state.json` within a poll. If the runner has not exited after 60
seconds (`RUN_LIVE_GRACE`), the wrapper kills that runner and no other. A
supervisor's stop timeout belongs above 60 seconds.

**Exit status.** 0 after a requested stop; 1 on failure, including a runner the
wrapper had to kill.

**`state.json`**, replaced whole (temporary file and rename) on every poll and
once more on exit:

| field | |
|---|---|
| `updated_at` | the heartbeat; stale while `running` means the runner is stuck |
| `running`, `started_at`, `stopped_at` | `running` is false only in the final write |
| `pid` | `run-live.sh`'s process id, the one to signal |
| `strategy`, `symbol`, `timeframe`, `config`, `magic`, `paper` | the session; `config` is the path as given |
| `mode` | `shadow` or `live`; null until the replay is done |
| `account` | `login`, `server`, `trade_mode` (`demo`, `contest`, `REAL`) |
| `last_closed_bar`, `forming_bar` | bar open times, on the broker's clock |
| `balance`, `equity` | the account's |
| `source` | `account`, or `replay` while in shadow: then the positions and orders below are the backtest's and none is on the account |
| `positions` | `ticket`, `side`, `volume`, `price` (entry), `sl`, `tp`, `tag`, `entry_time`, `profit` (floating, null until read) |
| `resting_orders` | `ticket`, `side`, `volume`, `limit`, `sl`, `tp`, `tag`, `void`, `withdrawing` |
| `queued_orders` | `side`, `volume`, `order` (`market`/`limit`), `limit`, `sl`, `tp`, `void`, `tag`, `signal_time`, and live `unanswered` (it may be at the broker already) and `problem`: why it is still waiting, such as the broker's answer `Market closed (retcode 10018)`; routine at the daily break |
| `margin` | `contract`, `as_of`, `maintenance`, `age_days`, `stale`, `stale_after_days`; null for a strategy without a margin log |
| `last_error` | `time` and `error` of the latest `error` event, or null |
| `failure` | why the runner stopped on its own, or null |

**Events**, one JSON object per line. Every one carries `time` (UTC, when
emitted), `kind`, `mode` (`shadow` or `live`), `strategy` and `symbol`:

| kind | fields |
|---|---|
| `started` | `magic`, `paper`, `account`, `replay_from`, `last_closed_bar`, `replay_trades`, `replay_open` (positions), `replay_pending` (orders), `margin` |
| `mode` | the switch to live: `adopted_positions`, `adopted_orders`, `closed_leftovers`, `removed_leftovers` (tickets), `queued` (orders) |
| `bar_closed` | `bar_time`, `close`, `balance`, `equity`, `open_positions`; the replay's figures in shadow |
| `order_intent` | about to be sent: `side`, `volume`, `order`, `tag`, `signal_time`, `sl`, `tp`, and `limit` for a limit order or, live, `price` for a market one |
| `order_placed` | a limit resting at the broker: the intent's fields and `ticket` |
| `order_filled` | a position opened: `ticket`, `side`, `volume`, `price`, `sl`, `tp`, `tag`, `entry_time` |
| `order_rejected` | a refusal, or an entry still unsent when its bar ended (`reason` starts `not sent before its bar ended:` and names the last answer); an entry's fields and `reason`, or for an open position's request `ticket`, `action` (`close`, `modify`, `remove`) and `reason`. Never for a "not now" answer while it can still be retried |
| `order_cancelled` | the order's fields, `ticket` if it rested, and `reason` |
| `position_modified` | `ticket`, `sl_from`, `sl`, `tp_from`, `tp` |
| `exit_intent` | a close about to be sent: `ticket`, `side`, `volume`, `price`, `reason`, `tag` |
| `position_closed` | the trade: `ticket`, `side`, `volume`, `entry_time`, `entry_price`, `exit_time`, `exit_price`, `reason` (`STOP_LOSS`, `TAKE_PROFIT`, `STRATEGY`, `MARGIN_CALL`), `gross_pnl`, `commission`, `swap`, `net_pnl`, `sl`, `tp`, `tag`, and more; a position closed for not being the strategy's carries only `ticket`, `side`, `volume`, `tag` and `leftover: true` |
| `error` | `error`, `retrying` (true while the runner carries on); `expected_login`, `actual_login` for another account; `retcode` (`CLIENT_DISABLES_AT`, `SERVER_DISABLES_AT`, `TRADE_DISABLED`) once when trading is blocked until someone acts; `ticket` and the row's details for an untracked position or order. A closed market raises none |
| `stopped` | `failure`, null after a requested stop |

`tests/test_live.py` holds the runner to the backtest. Against a fake terminal
that resolves stops, targets and limits by the simulated broker's rules, every
scenario produces the same trades, the same stops and the same records. Run
the same way over EUR/USD 2022-2026 with `mz50.yaml`, all 91 trades after a
2023 handover match the backtest's.

## Writing another strategy

One file, one decorated class. The `@register` decorator is what makes
`--strategy my_thing` work.

```python
from dataclasses import dataclass
from backtester.core.strategy import Strategy
from backtester.utils.params import StrategyParams
from backtester.strategies.registry import register

@dataclass
class MyParams(StrategyParams):
    volume: float = 0.1
    lookback: int = 20

@register
class MyStrategy(Strategy):
    name = "my_thing"
    params_class = MyParams

    def on_bar_open(self, ctx, event):
        """At a bar's open: you know the time and price, nothing else yet."""
        if ctx.is_flat and event.time.hour == 8:
            ctx.buy(self.p.volume, sl_pct=1.0, tp_pct=2.0)

    def on_bar(self, ctx, bar):
        """After the bar closes: full OHLC, orders fill at the next open."""
        if len(ctx.history) >= self.p.lookback:
            average = sum(ctx.history.closes(self.p.lookback)) / self.p.lookback
            ...
```

Add it to the imports in `backtester/strategies/__init__.py` and it is
available everywhere. `scripts/run_backtest.py --list-strategies` prints every
strategy with its parameters and defaults.

## How the engine avoids lying to you

Backtests fail in a small number of well-known ways. Each is handled explicitly
rather than by accident:

**Lookahead is not expressible.** The two hooks are separated by what is
knowable. `on_bar_open` receives a `BarOpen` — a time and a price, *not* a
`Bar` — so a strategy trading at the open physically cannot read the high, low
or close of the bar it is trading into. `on_bar` gets the finished bar, and
anything it orders fills at the **next** bar's open. Both are asserted in
`tests/test_engine.py::TestFillTiming`.

**Intrabar ambiguity is a knob, not a guess.** When one bar's range contains
both the stop and the target, the bar cannot say which came first. `--intrabar`
picks: `conservative` (default, stop wins), `optimistic` (target wins), or
`ohlc` (infer from bar direction). If a result moves materially between
conservative and optimistic, the run needs finer data — not a prettier
assumption. Check it in one command:

```bash
scripts/run_backtest.py -c configs/strategies/day_open_xauusd.yaml --intrabar optimistic
```

**Costs are on by default.** Spread (longs enter at ask and exit at bid; shorts
the reverse), commission per lot per side, and overnight swap. MT5 bars are bid
prices, and ignoring that makes a symmetric 2%/2% strategy look symmetric when
it is not.

**Gaps are asymmetric, deliberately.** A stop the market jumped over fills at
the open — worse than your level, which is what really happens. A target the
market jumped over still fills *at* the level, never better: booking that
windfall would make results depend on positive slippage no broker owes you.

**Drawdown is measured on equity, not on closed balance**, so heat taken by an
open position counts. Sharpe and Sortino are annualised from daily equity
returns whatever the bar timeframe, so an M5 run and a D1 run are comparable.

## Data

`--data` takes a URI and everything else is unchanged:

```
parquet://data/bars              default; recommended
csv://data/bars                  interchange, and the Wine prefix
sqlite:///data/bars.db           single file, SQL-queryable
postgresql://user@host/trading   shared / concurrent access
```

**Which one?** Parquet for bar data, PostgreSQL only when the data must be
shared, CSV for import and export. The full reasoning, with measured sizes and
timings, is in **[backtester/data/README.md](backtester/data/README.md)** — the
short version is that all backends read at the same speed (the time goes into
building Python objects, not I/O), so the choice is about size and access
pattern, and Parquet is 3x smaller than CSV and 9x smaller than SQLite.

```bash
scripts/manage_data.py list
scripts/manage_data.py check -s XAUUSD -t M15         # gaps, bad OHLC, spreads
scripts/manage_data.py convert --from csv://data/bars --to parquet://data/bars
scripts/manage_data.py import --file broker_export.csv -s XAUUSD -t M1
scripts/manage_data.py resample -s XAUUSD --from-tf M1 --to-tf M15
```

### Getting real data out of MT5

The `MetaTrader5` package is a Windows IPC client and cannot be imported from
Linux, so the fetcher runs under the same Wine Python as the MCP server. It
reuses `mt5-mcp-server/config.json`, so there is only ever one copy of the
password.

```bash
scripts/fetch-mt5.sh --symbol XAUUSD --timeframe M15,H1,D1 --start 2020-01-01
scripts/fetch-mt5.sh --symbol XAUUSD --timeframe D1 --dump-spec   # broker's real contract spec
scripts/manage_data.py convert --from csv://data/bars --to parquet://data/bars
```

`--dump-spec` is worth running once per symbol. The built-in XAUUSD spec
(100 oz/lot, 25-point spread, nominal swaps) is *typical retail*, not your
broker — and a wrong `contract_size` silently scales every P&L figure.

## Sweeping parameters

```bash
scripts/optimize.py -c configs/strategies/day_open_xauusd.yaml \
    --sweep stop_pct=1,1.5,2,2.5,3 --sweep take_pct=1,2,3 --sort sharpe --out sweep.csv
```

Runs in parallel, one bar-load per worker. A grid this cheap to run is also
cheap to overfit: treat the *shape* of the surface as the finding — a broad
plateau of decent results beats one bright cell — and confirm anything you like
on data the sweep never saw.

## Saved runs

`--save` writes a self-describing directory: `summary.json` (params, execution
assumptions, every metric), `trades.csv`, `equity.csv`, `run.log`. All readable
without this codebase, which is the difference between "I ran a sweep last
week" and knowing exactly what you ran.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

The storage suite runs the same cases against all three backends and asserts
they return byte-identical bars, so swapping stores cannot change a result.
