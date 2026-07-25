# MetaTrader 5 MCP server (Linux / Wine)

An MCP server exposing the local MetaTrader 5 terminal as 17 tools, following
the design in [MQL5 article 21905](https://www.mql5.com/en/articles/21905),
adapted to run against MT5 under Wine instead of native Windows.

## Why it runs under Wine

The `MetaTrader5` Python package is a Windows-only IPC client — it talks to
`terminal64.exe` over a Windows named pipe and cannot be imported from a Linux
interpreter. So the server runs on a **Windows Python installed inside the same
Wine prefix as the terminal**:

```
Claude Code (Linux)
  └─ stdio ─> run-server.sh
               └─ wine C:\Python311\python.exe server.py   (fastmcp)
                    └─ MetaTrader5 ─> terminal64.exe  (WINEPREFIX=~/.mt5)
```

Both must share the prefix (`~/.mt5`); a Python in a different prefix will not
see the terminal.

## What is installed

| Piece | Location |
|---|---|
| Windows Python 3.11.9 | `~/.mt5/drive_c/Python311` |
| `MetaTrader5` 5.0.5735, `fastmcp` 3.4.4 | that Python's site-packages |
| MCP registration | `../.mcp.json` (project scope) |

Reinstall the packages with:

```bash
WINEPREFIX=~/.mt5 wine 'C:\Python311\python.exe' -m pip install -r requirements.txt
```

## Configuration

`config.json` (chmod 600 — it holds the account password):

| Key | Meaning |
|---|---|
| `mt5_path` | Windows path to `terminal64.exe`; `null` lets the library find it |
| `login`, `password`, `server` | account credentials; `null` reuses the already logged-in terminal |
| `timeout` | connection timeout in seconds |
| `read_only` | `true` makes every trading tool refuse to execute |

## Tools

Read-only: `get_account`, `list_symbols`, `get_symbol_info`, `get_price`,
`get_rates`, `get_ticks`, `get_positions`, `get_pending_orders`,
`get_history_deals`, `get_history_orders`.

Trading: `place_market_order`, `place_pending_order`, `modify_pending_order`,
`cancel_order`, `close_position`, `close_all_positions`, `modify_position`.

Both order-placing tools accept `dry_run=true`, which runs the request through
the broker's validator (`order_check`) and reports margin impact without
trading.

## Requirements at runtime

- The terminal must be running and logged in (the server will launch it from
  `mt5_path` if it is not, which under Wine needs a display).
- **AutoTrading must be enabled in the terminal** (the "Algo Trading" toolbar
  button, or Ctrl+E) or every order is rejected with retcode 10027,
  "AutoTrading disabled by client". Read-only tools work regardless.
- Quotes are only live while the broker's market is open; outside session hours
  `get_price` returns the last close and `get_ticks` returns nothing.

## Testing without an MCP client

```bash
# handler-level
WINEPREFIX=~/.mt5 wine 'C:\Python311\python.exe' -c "import mt5client; print(mt5client.ensure_initialized())"

# protocol-level
npx @modelcontextprotocol/inspector ./run-server.sh
```

## Note on threading

MT5's `order_send` / `order_check` reject any call that carries a keyword
dict, even an empty one. Since `ThreadPoolExecutor.submit(fn, *args)` and
`functools.partial` both forward one, `mt5client._run_with_timeout` calls
positionally-only when there are no kwargs. Removing that branch brings back
`(-2, 'Unnamed arguments not allowed')` on every trade call.
