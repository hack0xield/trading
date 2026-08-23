"""Parquet backend — the default, and the one to use for real work.

Columnar and compressed: a backtest reads only the columns it needs, and
XAUUSD M1 lands at roughly 4 MB per year against 25 MB of CSV, loading in tens
of milliseconds rather than seconds. Files are partitioned by year so a
re-fetch of recent data rewrites one small file instead of the whole history.

Requires pyarrow; the import is deferred so the rest of the package stays
usable without it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..core.types import Bar
from .base import BarStore, SeriesInfo, dedupe, in_range


def _pyarrow():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover
        # Name the interpreter. A missing dependency and a virtualenv that was
        # not the one running the script produce the same ImportError, and
        # without this line they are indistinguishable — the usual cause is a
        # `#!/usr/bin/env python3` shebang resolving somewhere unexpected.
        import sys

        raise ImportError(
            f"The parquet store needs pyarrow, and {sys.executable} does not have it.\n"
            f"  install it:      {sys.executable} -m pip install pyarrow\n"
            f"  or, if that is not the interpreter you meant, name it explicitly:\n"
            f"                   .venv/bin/python scripts/run_backtest.py ...\n"
            f"  or avoid it:     use a csv://... data URI instead"
        ) from exc
    return pa, pq


class ParquetBarStore(BarStore):
    """`<root>/<SYMBOL>/<TIMEFRAME>/<YEAR>.parquet`."""

    name = "parquet"

    def __init__(self, root: str | Path = "data/bars", compression: str = "zstd"):
        self.root = Path(root)
        self.location = str(self.root)
        self.compression = compression

    def dir_for(self, symbol: str, timeframe: str) -> Path:
        return self.root / symbol.upper() / timeframe.upper()

    def _schema(self):
        pa, _ = _pyarrow()
        return pa.schema(
            [
                ("time", pa.timestamp("s", tz="UTC")),
                ("open", pa.float64()),
                ("high", pa.float64()),
                ("low", pa.float64()),
                ("close", pa.float64()),
                ("volume", pa.int64()),
                ("spread", pa.float64()),
            ]
        )

    def write(self, symbol: str, timeframe: str, bars: list[Bar]) -> int:
        if not bars:
            return 0
        pa, pq = _pyarrow()
        directory = self.dir_for(symbol, timeframe)
        directory.mkdir(parents=True, exist_ok=True)

        by_year: dict[int, list[Bar]] = {}
        for bar in bars:
            by_year.setdefault(bar.time.year, []).append(bar)

        written = 0
        for year, chunk in by_year.items():
            path = directory / f"{year}.parquet"
            if path.exists():
                chunk = dedupe(self._read_file(path) + chunk)
            else:
                chunk = dedupe(chunk)

            table = pa.Table.from_pydict(
                {
                    "time": [b.time for b in chunk],
                    "open": [b.open for b in chunk],
                    "high": [b.high for b in chunk],
                    "low": [b.low for b in chunk],
                    "close": [b.close for b in chunk],
                    "volume": [int(b.volume) for b in chunk],
                    "spread": [float(b.spread) for b in chunk],
                },
                schema=self._schema(),
            )
            tmp = path.with_suffix(".parquet.tmp")
            pq.write_table(table, tmp, compression=self.compression)
            tmp.replace(path)
            written += len(chunk)
        return written

    def _read_file(self, path: Path) -> list[Bar]:
        _, pq = _pyarrow()
        table = pq.read_table(path)
        data = table.to_pydict()
        return [
            Bar(
                time=_as_utc(t),
                open=o,
                high=h,
                low=lo,
                close=c,
                volume=int(v or 0),
                spread=float(s or 0.0),
            )
            for t, o, h, lo, c, v, s in zip(
                data["time"],
                data["open"],
                data["high"],
                data["low"],
                data["close"],
                data["volume"],
                data["spread"],
            )
        ]

    def read(
        self,
        symbol: str,
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        directory = self.dir_for(symbol, timeframe)
        if not directory.exists():
            return []
        bars: list[Bar] = []
        for path in sorted(directory.glob("*.parquet")):
            # Skip whole year files outside the window before touching them.
            try:
                year = int(path.stem)
            except ValueError:
                year = None
            if year is not None:
                if start is not None and year < start.year:
                    continue
                if end is not None and year > end.year:
                    continue
            bars.extend(self._read_file(path))
        bars.sort(key=lambda b: b.time)
        return in_range(bars, start, end)

    def list_series(self) -> list[SeriesInfo]:
        if not self.root.exists():
            return []
        out = []
        for symbol_dir in sorted(p for p in self.root.iterdir() if p.is_dir()):
            for tf_dir in sorted(p for p in symbol_dir.iterdir() if p.is_dir()):
                bars = self.read(symbol_dir.name, tf_dir.name)
                if not bars:
                    continue
                out.append(
                    SeriesInfo(
                        symbol=symbol_dir.name.upper(),
                        timeframe=tf_dir.name.upper(),
                        count=len(bars),
                        first=bars[0].time,
                        last=bars[-1].time,
                    )
                )
        return out

    def delete(self, symbol: str, timeframe: str) -> None:
        directory = self.dir_for(symbol, timeframe)
        if not directory.exists():
            return
        for path in directory.glob("*.parquet"):
            path.unlink()
        directory.rmdir()


def _as_utc(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(int(value), tz=timezone.utc)
