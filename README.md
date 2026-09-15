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
  metrics/      performance statistics, the casebook, report rendering
  utils/        timeframes, time parsing, typed parameters
  cli.py        argument plumbing shared by the scripts
scripts/        fetch, backtest, optimize, manage data, make synthetic data
configs/        run configs (YAML) and per-symbol contract specs
tests/          350 tests, ~3s
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
