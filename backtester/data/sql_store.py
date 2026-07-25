"""SQL backend via SQLAlchemy — works against PostgreSQL, SQLite, anything.

Use it when the data has to be *shared*: several machines, a teammate, or a
dashboard querying the same history a backtest ran on. For a single laptop
grinding one series, Parquet reads faster and needs no server.

Upserts are done as delete-then-insert over the affected time range rather than
with dialect-specific `ON CONFLICT`, which keeps the same code correct on both
SQLite and PostgreSQL.
"""

from __future__ import annotations

from datetime import datetime

from ..core.types import Bar
from ..utils.timeutil import ensure_utc
from .base import BarStore, SeriesInfo, dedupe


def _sqlalchemy():
    try:
        import sqlalchemy as sa
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "The SQL store needs SQLAlchemy: pip install sqlalchemy "
            "(plus psycopg2-binary for PostgreSQL)"
        ) from exc
    return sa


class SqlBarStore(BarStore):
    """Bars in a single `bars` table keyed on (symbol, timeframe, time)."""

    name = "sql"

    def __init__(self, url: str, table: str = "bars", echo: bool = False):
        sa = _sqlalchemy()
        self.url = url
        self.location = url.split("@")[-1]  # keep credentials out of logs
        self.engine = sa.create_engine(url, echo=echo, future=True)
        self.metadata = sa.MetaData()
        self.table = sa.Table(
            table,
            self.metadata,
            sa.Column("symbol", sa.String(32), primary_key=True),
            sa.Column("timeframe", sa.String(8), primary_key=True),
            sa.Column("time", sa.DateTime(timezone=True), primary_key=True),
            sa.Column("open", sa.Float, nullable=False),
            sa.Column("high", sa.Float, nullable=False),
            sa.Column("low", sa.Float, nullable=False),
            sa.Column("close", sa.Float, nullable=False),
            sa.Column("volume", sa.BigInteger, default=0),
            sa.Column("spread", sa.Float, default=0.0),
        )
        # Range scans dominate: "give me XAUUSD M15 between two dates".
        sa.Index(
            f"ix_{table}_series_time", self.table.c.symbol, self.table.c.timeframe, self.table.c.time
        )
        self.metadata.create_all(self.engine)

    def write(self, symbol: str, timeframe: str, bars: list[Bar]) -> int:
        if not bars:
            return 0
        sa = _sqlalchemy()
        symbol, timeframe = symbol.upper(), timeframe.upper()
        rows = dedupe(bars)
        lo, hi = rows[0].time, rows[-1].time

        with self.engine.begin() as conn:
            conn.execute(
                sa.delete(self.table).where(
                    self.table.c.symbol == symbol,
                    self.table.c.timeframe == timeframe,
                    self.table.c.time >= lo,
                    self.table.c.time <= hi,
                )
            )
            payload = [
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "time": bar.time,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": int(bar.volume),
                    "spread": float(bar.spread),
                }
                for bar in rows
            ]
            for start in range(0, len(payload), 5000):
                conn.execute(sa.insert(self.table), payload[start : start + 5000])
        return len(rows)

    def read(
        self,
        symbol: str,
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        sa = _sqlalchemy()
        query = sa.select(
            self.table.c.time,
            self.table.c.open,
            self.table.c.high,
            self.table.c.low,
            self.table.c.close,
            self.table.c.volume,
            self.table.c.spread,
        ).where(
            self.table.c.symbol == symbol.upper(),
            self.table.c.timeframe == timeframe.upper(),
        )
        if start is not None:
            query = query.where(self.table.c.time >= start)
        if end is not None:
            query = query.where(self.table.c.time <= end)
        query = query.order_by(self.table.c.time)

        with self.engine.connect() as conn:
            return [
                Bar(
                    time=ensure_utc(row[0]),
                    open=row[1],
                    high=row[2],
                    low=row[3],
                    close=row[4],
                    volume=int(row[5] or 0),
                    spread=float(row[6] or 0.0),
                )
                for row in conn.execute(query)
            ]

    def list_series(self) -> list[SeriesInfo]:
        sa = _sqlalchemy()
        query = (
            sa.select(
                self.table.c.symbol,
                self.table.c.timeframe,
                sa.func.count().label("count"),
                sa.func.min(self.table.c.time).label("first"),
                sa.func.max(self.table.c.time).label("last"),
            )
            .group_by(self.table.c.symbol, self.table.c.timeframe)
            .order_by(self.table.c.symbol, self.table.c.timeframe)
        )
        with self.engine.connect() as conn:
            return [
                SeriesInfo(
                    symbol=row[0],
                    timeframe=row[1],
                    count=int(row[2]),
                    first=ensure_utc(row[3]) if row[3] else None,
                    last=ensure_utc(row[4]) if row[4] else None,
                )
                for row in conn.execute(query)
            ]

    def delete(self, symbol: str, timeframe: str) -> None:
        sa = _sqlalchemy()
        with self.engine.begin() as conn:
            conn.execute(
                sa.delete(self.table).where(
                    self.table.c.symbol == symbol.upper(),
                    self.table.c.timeframe == timeframe.upper(),
                )
            )
