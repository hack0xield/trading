"""Storage backends. Every backend must behave identically to the others."""

from __future__ import annotations

from datetime import timedelta

import pytest

from backtester.core.types import Bar
from backtester.data.csv_store import CsvBarStore, read_csv_file
from backtester.data.loader import resample, validate_bars
from backtester.data.registry import store_from_uri
from backtester.data.sql_store import SqlBarStore
from conftest import START, series

pyarrow = pytest.importorskip("pyarrow", reason="parquet backend needs pyarrow")

from backtester.data.parquet_store import ParquetBarStore  # noqa: E402


def make(index: int, open_: float, close: float) -> Bar:
    """A bar with a real range, unlike the flat ones `series()` produces."""
    return Bar(
        time=START + index * timedelta(minutes=15),
        open=open_,
        high=max(open_, close) + 5.0,
        low=min(open_, close) - 5.0,
        close=close,
        volume=100,
        spread=0.0,
    )


@pytest.fixture(params=["csv", "parquet", "sqlite"])
def store(request, tmp_path):
    """The same suite runs against each backend."""
    if request.param == "csv":
        return CsvBarStore(tmp_path / "bars")
    if request.param == "parquet":
        return ParquetBarStore(tmp_path / "bars")
    return SqlBarStore(f"sqlite:///{tmp_path / 'bars.db'}")


class TestRoundTrip:
    def test_write_then_read_returns_what_went_in(self, store):
        bars = series([1800.0, 1810.0, 1820.0])
        assert store.write("XAUUSD", "M15", bars) == 3

        read = store.read("XAUUSD", "M15")
        assert len(read) == 3
        assert [b.time for b in read] == [b.time for b in bars]
        assert read[0].open == pytest.approx(1800.0)
        assert read[-1].close == pytest.approx(1820.0)

    def test_all_fields_survive(self, store):
        original = Bar(
            time=START, open=1800.12, high=1805.55, low=1799.01, close=1803.33,
            volume=4321, spread=0.27,
        )
        store.write("XAUUSD", "M15", [original])
        read = store.read("XAUUSD", "M15")[0]
        assert read.time == original.time
        assert read.high == pytest.approx(original.high)
        assert read.low == pytest.approx(original.low)
        assert read.volume == original.volume
        assert read.spread == pytest.approx(original.spread)

    def test_symbol_and_timeframe_are_case_insensitive(self, store):
        store.write("xauusd", "m15", series([1800.0]))
        assert len(store.read("XAUUSD", "M15")) == 1

    def test_missing_series_reads_empty(self, store):
        assert store.read("NOPE", "M15") == []


class TestUpsert:
    def test_rewriting_the_same_bars_does_not_duplicate(self, store):
        bars = series([1800.0, 1810.0, 1820.0])
        store.write("XAUUSD", "M15", bars)
        store.write("XAUUSD", "M15", bars)
        assert len(store.read("XAUUSD", "M15")) == 3

    def test_an_overlapping_refetch_takes_the_newer_bar(self, store):
        """A candle that was still forming gets corrected, not duplicated."""
        store.write("XAUUSD", "M15", series([1800.0, 1810.0]))
        updated = Bar(
            time=series([1800.0, 1810.0])[1].time,
            open=1810.0, high=1899.0, low=1810.0, close=1888.0, volume=99, spread=0.3,
        )
        store.write("XAUUSD", "M15", [updated])

        read = store.read("XAUUSD", "M15")
        assert len(read) == 2
        assert read[1].close == pytest.approx(1888.0)

    def test_appending_a_later_range_extends_the_series(self, store):
        first = series([1800.0, 1810.0])
        later = [
            Bar(time=b.time + timedelta(days=30), open=b.open, high=b.high,
                low=b.low, close=b.close, volume=b.volume, spread=b.spread)
            for b in series([1900.0, 1910.0])
        ]
        store.write("XAUUSD", "M15", first)
        store.write("XAUUSD", "M15", later)
        assert len(store.read("XAUUSD", "M15")) == 4


class TestQuerying:
    def test_range_filter_is_inclusive(self, store):
        bars = series([1800.0, 1810.0, 1820.0, 1830.0])
        store.write("XAUUSD", "M15", bars)
        read = store.read("XAUUSD", "M15", start=bars[1].time, end=bars[2].time)
        assert len(read) == 2
        assert read[0].time == bars[1].time

    def test_list_series_reports_the_span(self, store):
        bars = series([1800.0, 1810.0, 1820.0])
        store.write("XAUUSD", "M15", bars)
        store.write("EURUSD", "H1", series([1.1, 1.2]))

        found = {(s.symbol, s.timeframe): s for s in store.list_series()}
        assert set(found) == {("XAUUSD", "M15"), ("EURUSD", "H1")}
        assert found[("XAUUSD", "M15")].count == 3
        assert found[("XAUUSD", "M15")].first == bars[0].time
        assert found[("XAUUSD", "M15")].last == bars[-1].time

    def test_delete_removes_only_that_series(self, store):
        store.write("XAUUSD", "M15", series([1800.0]))
        store.write("XAUUSD", "H1", series([1800.0]))
        store.delete("XAUUSD", "M15")
        assert store.read("XAUUSD", "M15") == []
        assert len(store.read("XAUUSD", "H1")) == 1


class TestBackendsAgree:
    def test_every_backend_returns_identical_bars(self, tmp_path):
        """The point of the abstraction: swapping the store cannot change a run."""
        bars = series([1800.0 + i for i in range(50)])
        stores = [
            CsvBarStore(tmp_path / "csv"),
            ParquetBarStore(tmp_path / "parquet"),
            SqlBarStore(f"sqlite:///{tmp_path / 'x.db'}"),
        ]
        for store in stores:
            store.write("XAUUSD", "M15", bars)

        results = [store.read("XAUUSD", "M15") for store in stores]
        for other in results[1:]:
            assert [b.as_row() for b in other] == [b.as_row() for b in results[0]]


class TestUriResolution:
    def test_scheme_selects_the_backend(self, tmp_path):
        assert isinstance(store_from_uri(f"csv://{tmp_path}"), CsvBarStore)
        assert isinstance(store_from_uri(f"parquet://{tmp_path}"), ParquetBarStore)
        assert isinstance(store_from_uri(f"sqlite:///{tmp_path}/a.db"), SqlBarStore)

    def test_a_bare_path_prefers_parquet(self, tmp_path):
        assert isinstance(store_from_uri(str(tmp_path)), ParquetBarStore)

    def test_a_db_suffix_means_sqlite(self, tmp_path):
        assert isinstance(store_from_uri(f"{tmp_path}/bars.db"), SqlBarStore)

    def test_a_store_passes_through(self, tmp_path):
        store = CsvBarStore(tmp_path)
        assert store_from_uri(store) is store


class TestCsvImport:
    def test_column_aliases_are_accepted(self, tmp_path):
        """MT5 exports, broker dumps and Kaggle files all name columns differently."""
        path = tmp_path / "export.csv"
        path.write_text(
            "<DATE>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<TICKVOL>,<SPREAD>\n"
            "2024-01-01 00:00:00,1800.0,1810.0,1795.0,1805.0,1234,25\n"
            "2024-01-01 00:15:00,1805.0,1815.0,1800.0,1810.0,2345,25\n",
            encoding="utf-8",
        )
        bars = read_csv_file(path)
        assert len(bars) == 2
        assert bars[0].open == pytest.approx(1800.0)
        assert bars[1].volume == 2345

    def test_missing_required_column_is_an_error(self, tmp_path):
        path = tmp_path / "bad.csv"
        path.write_text("time,open,high\n2024-01-01,1,2\n", encoding="utf-8")
        with pytest.raises(ValueError, match="missing column"):
            read_csv_file(path)


class TestValidation:
    def test_clean_bars_pass(self):
        bars = [make(0, 1800.0, 1805.0), make(1, 1805.0, 1802.0)]
        assert validate_bars(bars, "M15") == []

    def test_zero_range_bars_only_warn(self):
        """`series()` builds flat bars; that is a warning, not a hard error."""
        problems = validate_bars(series([1800.0, 1810.0]), "M15")
        assert problems and all(p.startswith("WARN") for p in problems)

    def test_out_of_order_bars_are_an_error(self):
        bars = series([1800.0, 1810.0])
        assert any("ERROR" in p for p in validate_bars([bars[1], bars[0]], "M15"))

    def test_ohlc_outside_high_low_is_an_error(self):
        bad = Bar(time=START, open=1800.0, high=1790.0, low=1780.0, close=1785.0)
        assert any("ERROR" in p for p in validate_bars([bad], "M15"))


class TestResample:
    def test_m15_aggregates_into_h1(self):
        # Four M15 bars inside one hour: open of the first, close of the last,
        # extremes of all four.
        bars = [
            make(0, 1800.0, 1805.0),  # high 1810, low 1795
            make(1, 1805.0, 1810.0),  # high 1815, low 1800
            make(2, 1810.0, 1790.0),  # high 1815, low 1785
            make(3, 1790.0, 1805.0),  # high 1810, low 1785
        ]
        hourly = resample(bars, "H1")

        assert len(hourly) == 1
        assert hourly[0].time == bars[0].time
        assert hourly[0].open == pytest.approx(1800.0)
        assert hourly[0].close == pytest.approx(1805.0)
        assert hourly[0].high == pytest.approx(1815.0)
        assert hourly[0].low == pytest.approx(1785.0)
        assert hourly[0].volume == sum(b.volume for b in bars)
