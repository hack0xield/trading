# Trading / backtesting workspace

Offline backtesting of MetaTrader 5 symbols, plus the plumbing that fetches bars.

| | |
|---|---|
| `backtester/` | the engine: bar loop, broker simulation, metrics, strategies |
| `backtester/indicators/` | strategy-agnostic indicators (ZigZag) |
| `backtester/strategies/margin_zones/` | CME margin turned into price levels, and what watches them |
| `scripts/` | run, fetch, optimize, manage data, chart |
| `configs/` | run configs (YAML), contract specs, the broker clock |
| `mt5-mcp-server/` | the live MT5 terminal as MCP tools |
| `impl-spec/` | the specifications to build next |
| `impl-spec-old/` | the specifications the code implements today |

## Two interpreters

`MetaTrader5` is Windows-only: it talks to `terminal64.exe` over a named pipe
and cannot be imported from Linux. Anything touching the live terminal runs on
Wine Python (`~/.mt5/drive_c/Python311`, via `run-server.sh` and
`fetch-mt5.sh`); everything else runs on Linux Python 3.10+.

`backtester/` core code therefore stays **standard-library only**. Storage
backends import pyarrow / SQLAlchemy lazily, inside the function that needs
them, so the package still imports in the Wine prefix where neither exists.

## Working here

```bash
.venv/bin/python -m pytest tests/ -q                    # 350 tests, ~3s
scripts/run_backtest.py --config configs/strategies/mz50.yaml --save
scripts/run_backtest.py --list-strategies
```

## The remote host

A Vultr instance runs this code behind the Telegram assistant in `../agents`,
and keeps its own backtest artifacts. Two `~/.ssh/config` aliases reach it:
`vultr-trading-app` (the `trading` user — everything) and `vultr-trading`
(root, provisioning only). `ssh -G vultr-trading-app` prints the address.

Its runs sit at the same paths, under `~/trading_assistant/trading/`:

| | |
|---|---|
| `runs/` | curated and reviewed — the evidence the assistant's `backtests.*` tools read |
| `runs-adhoc/` | assistant-initiated; `provenance.json` marks each `reviewed: false` |

That `runs/` is curated separately from this workstation's. The two holding
different runs is the design, not drift — do not offer to sync them.

Quickest read is the reports server on port **8083**, public and read-only, so
it needs no ssh: `/` indexes every run, `/r/<run-id>/` is one. For raw numbers,
`summary.json` in each run directory.

```bash
ssh vultr-trading-app 'ls -t trading_assistant/trading/runs-adhoc | head'
```

Its services are systemd **user** units — `systemctl --user`, never plain
`systemctl`, which answers `not-found` for all of them and reads as a dead
host. `../agents/deploy/README.md` covers operating them.

## Conventions

- All datetimes are timezone-aware UTC. Bars carry their **open** time.
- MT5 bar timestamps are **broker-server time** (UTC+2/+3), stored verbatim and
  labelled UTC. `scripts/fetch_mt5.py` records the measured offset in
  `configs/broker.json`; read it with `backtester.utils.timeutil.broker_offset`.
  The offset is seasonal — EET/EEST, shifting with New York — so trading
  sessions sit at stable *broker-local* hours. Read hours off the bars' own
  stamps rather than converting.
- Bar prices are **bid**. Longs enter at ask, shorts exit at ask.
- A strategy sees only closed bars. `on_bar_open` gets a time and a price,
  `on_bar` gets the finished bar, and orders fill at the next open. Nothing may
  use a value that was not knowable on the bar it is acting on — the analysis
  and the strategy must see the same thing.
- A new strategy is one file in `strategies/` with `@register`, plus a line in
  `strategies/__init__.py`.
- `strategy.txt` holds account credentials and is gitignored. Do not read
  secrets from it into code — the fetcher reuses `mt5-mcp-server/config.json`.

## Comments and docstrings

Describe what the code **is and does now**.

- **No history.** Never write what the code used to do, what changed, what was
  renamed or extracted, or why a past decision was taken. Git holds that.
- **State the purpose.** A module or function description says what it is for.
  Do not say what it is *not*, and do not describe how callers use it.
- **Summarise.** A description is not a walkthrough of every step and caveat.
  Keep a short reason inline only where a reader would otherwise break the code.
- **Short.** One line where one line does.
