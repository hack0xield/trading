"""Persisting derived output — backtest runs, and analysis exports.

A run directory is self-describing: the parameters, the metrics, every trade
and the equity curve, all in formats you can open without this codebase. That
is the difference between "I ran a sweep last week" and "here is exactly what I
ran last week and what it produced".
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from ..core.types import BacktestResult


def run_directory(root: str | Path, result: BacktestResult, label: str = "") -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    parts = [stamp, result.strategy, result.symbol, result.timeframe]
    if label:
        parts.append(label)
    path = Path(root) / "_".join(p for p in parts if p)
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_result(
    result: BacktestResult,
    metrics: dict,
    root: str | Path = "runs",
    label: str = "",
    execution: dict | None = None,
) -> Path:
    """Write summary.json, trades.csv and equity.csv. Returns the directory."""
    directory = run_directory(root, result, label)

    summary = {
        "strategy": result.strategy,
        "symbol": result.symbol,
        "timeframe": result.timeframe,
        "params": result.params,
        "execution": execution or {},
        "period": {
            "start": result.start.isoformat() if result.start else None,
            "end": result.end.isoformat() if result.end else None,
            "bars": result.bars_processed,
        },
        "metrics": metrics,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(directory / "summary.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)

    write_rows(directory / "trades.csv", [t.as_row() for t in result.trades])
    write_rows(directory / "equity.csv", [p.as_row() for p in result.equity])

    if result.logs:
        with open(directory / "run.log", "w", encoding="utf-8") as fh:
            fh.write("\n".join(result.logs) + "\n")

    return directory


def write_rows(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def load_summary(directory: str | Path) -> dict:
    with open(Path(directory) / "summary.json", "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_sweep(rows: list[dict], path: str | Path) -> Path:
    """Flat table of one row per parameter combination, ready for a spreadsheet."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return path
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return path
