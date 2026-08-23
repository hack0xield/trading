# Trading / backtesting workspace

Two pieces, kept separate because they run on different interpreters:

| | Runs on | Purpose |
|---|---|---|
| `mt5-mcp-server/` | Wine Python (`~/.mt5/drive_c/Python311`) | live MT5 terminal as MCP tools |
| `backtester/` + `scripts/` | Linux Python 3.10+ | offline backtesting |

**The `MetaTrader5` package is Windows-only** — it talks to `terminal64.exe`
over a named pipe and cannot be imported from the Linux interpreter. Anything
touching the live terminal goes through Wine (`run-server.sh`, `fetch-mt5.sh`).
Everything else stays on Linux.

Because of that split, `backtester/` core code must stay **standard-library
only**. Storage backends import pyarrow / SQLAlchemy lazily, inside the
function that needs them, so the package still imports in the Wine prefix where
neither is installed.

## Working here

```bash
.venv/bin/python -m pytest tests/ -q                    # 299 tests, ~3s
scripts/run_backtest.py --config configs/day_open_xauusd.yaml
scripts/run_backtest.py --list-strategies
```

See [README.md](README.md) for the engine's guarantees and
[backtester/data/README.md](backtester/data/README.md) for the storage
trade-offs.

## Conventions

- All datetimes are timezone-aware UTC. Bars carry their **open** time.
- MT5 bar timestamps are **broker-server time** (usually UTC+2/+3), stored
  verbatim and labelled UTC. Strategies correct for it via `session_tz`.
- Bar prices are **bid**. Longs enter at ask, shorts exit at ask.
- A new strategy is one file in `strategies/` with `@register`, plus a line in
  `strategies/__init__.py`.
- `strategy.txt` holds account credentials and is gitignored. Do not read
  secrets from it into code — the fetcher reuses `mt5-mcp-server/config.json`.
