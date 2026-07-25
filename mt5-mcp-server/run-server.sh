#!/bin/bash
# Launch the MT5 MCP server under the Wine Python that shares the MT5 prefix.
# The MetaTrader5 package is Windows-only, so a Linux interpreter cannot be used.
export WINEPREFIX="${WINEPREFIX:-$HOME/.mt5}"
export WINEDEBUG=-all          # keep Wine chatter off the stdio channel
export PYTHONUNBUFFERED=1
export PYTHONIOENCODING=utf-8
cd "$(dirname "$0")" || exit 1
exec wine 'C:\Python311\python.exe' server.py
