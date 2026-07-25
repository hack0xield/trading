#!/bin/bash
# Run fetch_mt5.py under the Wine Python that shares the MT5 prefix.
# The MetaTrader5 package is Windows-only, so the Linux interpreter cannot
# import it — same constraint as mt5-mcp-server/run-server.sh.
#
#   scripts/fetch-mt5.sh --symbol XAUUSD --timeframe M15,H1 --start 2020-01-01
#   scripts/fetch-mt5.sh --symbol XAUUSD --dump-spec --timeframe D1
export WINEPREFIX="${WINEPREFIX:-$HOME/.mt5}"
export WINEDEBUG=-all
export PYTHONUNBUFFERED=1
export PYTHONIOENCODING=utf-8
cd "$(dirname "$0")/.." || exit 1
exec wine 'C:\Python311\python.exe' scripts/fetch_mt5.py "$@"
