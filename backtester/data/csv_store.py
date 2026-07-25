"""CSV backend — stdlib only, readable, diffable, bulky.

Worth having for exchanging data, eyeballing a series, and running on an
interpreter with nothing installed — which is exactly the Wine prefix the MT5
fetcher runs in.

Its real cost is size, not speed. Measured on 74,985 XAUUSD M15 bars in this
repo, CSV takes 68 bytes per bar against Parquet's 21 — so a year of M1 gold
(~370k bars) is roughly 25 MB here versus 8 MB there, and the gap compounds
across symbols and years. Read latency is near-identical between the backends,
because at this scale the time goes into building `Bar` objects rather than
into I/O. See `backtester/data/README.md` for the numbers.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path

from ..core.types import Bar
from ..utils.timeutil import ensure_utc, parse_dt
from .base import BAR_COLUMNS, BarStore, SeriesInfo, dedupe


class CsvBarStore(BarStore):
    """One file per series: `<root>/<SYMBOL>/<TIMEFRAME>.csv`."""

    name = "csv"

    def __init__(self, root: str | Path = "data/bars"):
        self.root = Path(root)
        self.location = str(self.root)

    def path_for(self, symbol: str, timeframe: str) -> Path:
        return self.root / symbol.upper() / f"{timeframe.upper()}.csv"

    def write(self, symbol: str, timeframe: str, bars: list[Bar]) -> int:
        if not bars:
            return 0
        path = self.path_for(symbol, timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)

        existing = self.read(symbol, timeframe) if path.exists() else []
        merged = dedupe(existing + list(bars))

        # Write beside the target and rename, so an interrupted run cannot
        # leave a half-written series where a good one used to be.
        tmp = path.with_suffix(".csv.tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(BAR_COLUMNS)
            for bar in merged:
                writer.writerow(
                    [
                        bar.time.isoformat(),
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.volume,
                        bar.spread,
                    ]
                )
        os.replace(tmp, path)
        return len(merged)

    def read(
        self,
        symbol: str,
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        path = self.path_for(symbol, timeframe)
        if not path.exists():
            return []
        bars = []
        with open(path, "r", newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                time = parse_dt(row["time"])
                if start is not None and time < start:
                    continue
                if end is not None and time > end:
                    break  # file is time-ordered
                bars.append(
                    Bar(
                        time=time,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=int(float(row.get("volume") or 0)),
                        spread=float(row.get("spread") or 0.0),
                    )
                )
        return bars

    def list_series(self) -> list[SeriesInfo]:
        if not self.root.exists():
            return []
        out = []
        for symbol_dir in sorted(p for p in self.root.iterdir() if p.is_dir()):
            for file in sorted(symbol_dir.glob("*.csv")):
                bars = self.read(symbol_dir.name, file.stem)
                out.append(
                    SeriesInfo(
                        symbol=symbol_dir.name.upper(),
                        timeframe=file.stem.upper(),
                        count=len(bars),
                        first=bars[0].time if bars else None,
                        last=bars[-1].time if bars else None,
                    )
                )
        return out

    def delete(self, symbol: str, timeframe: str) -> None:
        path = self.path_for(symbol, timeframe)
        if path.exists():
            path.unlink()


def read_csv_file(path: str | Path, time_column: str = "time") -> list[Bar]:
    """Load bars from an arbitrary CSV — a broker export, a Kaggle dump.

    Column names are matched case-insensitively and the usual aliases are
    accepted, because no two sources agree on what to call tick volume.
    """
    aliases = {
        "time": ("time", "date", "datetime", "timestamp", "<date>"),
        "open": ("open", "o", "<open>"),
        "high": ("high", "h", "<high>"),
        "low": ("low", "l", "<low>"),
        "close": ("close", "c", "<close>"),
        "volume": ("volume", "vol", "tick_volume", "tickvol", "<tickvol>"),
        "spread": ("spread", "<spread>"),
    }
    bars = []
    with open(path, "r", newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(fh, dialect=dialect)
        lookup = {name.strip().lower(): name for name in (reader.fieldnames or [])}

        def column(field: str) -> str | None:
            for candidate in aliases[field]:
                if candidate in lookup:
                    return lookup[candidate]
            return None

        columns = {field: column(field) for field in aliases}
        if columns["time"] is None:
            columns["time"] = lookup.get(time_column.lower())
        missing = [f for f in ("time", "open", "high", "low", "close") if not columns[f]]
        if missing:
            raise ValueError(f"{path}: missing column(s) {', '.join(missing)}")

        for row in reader:
            bars.append(
                Bar(
                    time=ensure_utc(parse_dt(row[columns["time"]])),
                    open=float(row[columns["open"]]),
                    high=float(row[columns["high"]]),
                    low=float(row[columns["low"]]),
                    close=float(row[columns["close"]]),
                    volume=int(float(row[columns["volume"]] or 0)) if columns["volume"] else 0,
                    spread=float(row[columns["spread"]] or 0) if columns["spread"] else 0.0,
                )
            )
    return dedupe(bars)
