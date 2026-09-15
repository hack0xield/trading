#!/bin/bash
# Run run_live.py under the Wine Python that shares the MT5 prefix, until stopped.
#
#   scripts/run-live.sh --config configs/strategies/mz50.yaml
#
# Ctrl-C or SIGTERM to this script creates the stop file and waits up to
# RUN_LIVE_GRACE seconds (default 60) for the runner to exit on its own, since
# Wine does not reliably deliver signals to the Windows-side Python. Positions
# stay open under their server-side stops. Exit status is the runner's: 0 after
# a requested stop, non-zero on failure or when the runner had to be killed.
set -uo pipefail
export WINEPREFIX="${WINEPREFIX:-$HOME/.mt5}"
export WINEDEBUG=-all
export PYTHONUNBUFFERED=1
export PYTHONIOENCODING=utf-8
# state.json records this script's pid: the one to signal.
export RUN_LIVE_PID=$$
cd "$(dirname "$0")/.." || exit 1

mkdir -p runs-live
STOP_FILE="runs-live/stop-$$"
GRACE="${RUN_LIVE_GRACE:-60}"
PID=""

stop() {
  echo "[run-live] stopping..."
  touch "$STOP_FILE"
  [ -z "$PID" ] && { rm -f "$STOP_FILE"; exit 0; }
  for _ in $(seq "$GRACE"); do
    if ! kill -0 "$PID" 2>/dev/null; then
      wait "$PID"
      status=$?
      rm -f "$STOP_FILE"
      exit "$status"
    fi
    sleep 1
  done
  echo "[run-live] no clean exit after ${GRACE}s, killing"
  kill "$PID" 2>/dev/null
  # Only this script's runner: its stop file is in its command line.
  pkill -f "python\.exe scripts/run_live\.py --stop-file ${STOP_FILE}( |\$)" 2>/dev/null
  rm -f "$STOP_FILE"
  exit 1
}
trap stop INT TERM

# Started in the background, so the shell's own Ctrl-C reaches only the trap.
wine 'C:\Python311\python.exe' scripts/run_live.py --stop-file "$STOP_FILE" "$@" &
PID=$!
wait "$PID"
status=$?
rm -f "$STOP_FILE"
exit "$status"
