"""Resolve a `--data` string into a store.

    parquet://data/bars           Parquet files (default, recommended)
    csv://data/bars               CSV files
    sqlite:///data/bars.db        SQLite, single file, SQL queryable
    postgresql://user@host/trading  PostgreSQL
    data/bars                     bare path -> Parquet if pyarrow is present,
                                  otherwise CSV

Anything with a scheme SQLAlchemy understands is handed straight to it, so
MySQL, TimescaleDB and friends work without changes here.
"""

from __future__ import annotations

from pathlib import Path

from .base import BarStore

FILE_SCHEMES = {"csv", "parquet"}
DEFAULT_URI = "parquet://data/bars"


def store_from_uri(uri: str | BarStore | None = None) -> BarStore:
    if isinstance(uri, BarStore):
        return uri
    text = (uri or DEFAULT_URI).strip()

    if "://" not in text:
        return _from_path(text)

    scheme, _, rest = text.partition("://")
    scheme = scheme.lower()

    if scheme == "csv":
        from .csv_store import CsvBarStore

        return CsvBarStore(rest)
    if scheme == "parquet":
        from .parquet_store import ParquetBarStore

        return ParquetBarStore(rest)

    from .sql_store import SqlBarStore

    return SqlBarStore(text)


def _from_path(path: str) -> BarStore:
    """A bare path: pick the best backend the environment can support."""
    if path.endswith((".db", ".sqlite", ".sqlite3")):
        from .sql_store import SqlBarStore

        return SqlBarStore(f"sqlite:///{Path(path).as_posix()}")

    try:
        import pyarrow  # noqa: F401  (probe: is the parquet backend usable?)

        from .parquet_store import ParquetBarStore

        return ParquetBarStore(path)
    except ImportError:
        from .csv_store import CsvBarStore

        return CsvBarStore(path)


def describe_backends() -> str:  # pragma: no cover - help text
    return __doc__ or ""
