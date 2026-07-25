# Which store should I use?

**Short answer: Parquet for bar data, PostgreSQL only when the data has to be
shared, CSV as the interchange format.** All three are implemented and
interchangeable — `--data` takes a URI and nothing else in the codebase changes.

## Measured, on this machine

74,985 XAUUSD M15 bars (3 years), same data written to each backend:

| Backend | On disk | Bytes/bar | Warm full read | Server needed |
|---|---:|---:|---:|---|
| Parquet (zstd) | 1.6 MB | 21 | 358 ms | no |
| CSV | 5.1 MB | 68 | 253 ms | no |
| SQLite | 14.9 MB | 199 | 257 ms | no |
| PostgreSQL | ~same as SQLite | ~200 | network-bound | yes |

Two things in that table are worth absorbing before picking.

**The read times are all the same, and none of them are I/O.** Pulling the
Parquet *file* into an Arrow table takes **2 ms**; the other ~356 ms is spent
constructing 75,000 Python `Bar` objects. Every backend pays that same cost, so
at this scale the storage choice does not buy you speed. It buys you size,
tooling, and room to grow.

**Size is where they genuinely differ.** Parquet is 3x smaller than CSV and 9x
smaller than SQLite, because it stores columns of doubles rather than rows of
text with per-row indexes. Scale that to tick-level or multi-symbol M1 history
and it becomes the difference between a directory you can back up and one you
cannot.

## The recommendation, and why

### Parquet — the default

Columnar, compressed, no server, partitioned by year so a re-fetch of recent
data rewrites one small file instead of the whole history. It is what the
quant world stores bar data in, and the reason is the size column above.

It is also the format every other tool already reads. When you want to analyse
a run, DuckDB queries the files in place with no import step:

```sql
-- duckdb, straight against the store
SELECT date_trunc('month', time) AS m, count(*), avg(high - low) AS avg_range
FROM 'data/bars/XAUUSD/M15/*.parquet' GROUP BY 1 ORDER BY 1;
```

Cost: a pyarrow dependency, and the files are not human-readable.

### PostgreSQL — when it is about sharing, not speed

Reach for it when the *access pattern* justifies a server, not because a
database sounds more serious than files:

- several machines or people need one authoritative copy of the history
- a dashboard, a live bot and a backtest all read the same data concurrently
- you want to keep years of **run results** and query across them in SQL
  ("every sweep where profit factor > 1.2, by month")

For one laptop grinding one symbol, it is strictly more moving parts for no
gain: 9x the disk, a daemon to keep running, and no faster to read.

`SqlBarStore` is plain SQLAlchemy Core, so the identical code runs against
SQLite (single file, zero setup, genuinely useful for a portable snapshot) and
TimescaleDB (worth a look if you go the Postgres route — its hypertables and
compression are built for exactly this shape of data).

### CSV — interchange, not a working format

Keep it for importing a broker export, handing a series to someone else, and
for the MT5 fetcher, which runs under the Wine Python where pyarrow is not
installed. Do not point a multi-year M1 backtest at it.

## Moving between them

Nothing is locked in — the conversion is one command, and the stores are
verified against each other by `tests/test_stores.py::TestBackendsAgree`:

```bash
scripts/manage_data.py convert --from csv://data/bars --to parquet://data/bars
scripts/manage_data.py convert --from parquet://data/bars --to postgresql://localhost/trading
```

## What is stored

One row per bar, keyed on `(symbol, timeframe, time)`:

| Column | Meaning |
|---|---|
| `time` | bar **open** time, UTC-aware (MT5 reports broker-server time — see below) |
| `open`, `high`, `low`, `close` | bid prices, as MT5 reports them |
| `volume` | tick volume |
| `spread` | spread at the bar, in price units (MT5 gives points; converted on ingest) |

Writes are upserts, so re-fetching an overlapping range never duplicates bars
and a candle that was still forming gets corrected rather than doubled.

> **Timestamps are broker-server time**, typically UTC+2/+3, stored verbatim
> and labelled UTC. This is deliberate — silently shifting it would be worse —
> but it means "the start of the day" is your broker's midnight, not London's.
> `session_tz` and `session_start_hour` on a strategy are where you correct it.
