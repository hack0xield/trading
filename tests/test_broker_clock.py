"""Measuring the broker's server clock — `scripts/fetch_mt5.py`.

MT5 stamps every bar with the server's wall clock and labels it UTC, and the
fetcher stores that verbatim. Nothing in the data records which clock it was,
so an hour read off a bar cannot be turned back into a real time without this
measurement — and a *wrong* measurement is worse than none, because it produces
session labels that look right and are not.

These tests run on Linux with no terminal: the arithmetic is a pure function of
two numbers, and the terminal is a stub.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts.fetch_mt5 import (
    plausible_offset,
    read_server_clock,
    recorded_offset,
    server_offset_hours,
    write_server_clock,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


class Tick:
    def __init__(self, time): self.time = time


class Terminal:
    company = "Broker Ltd"


class Account:
    server = "Broker-Live"


class FakeMT5:
    """Just enough terminal for the measurement."""

    def __init__(self, tick_time, account=True, terminal=True):
        self._tick = Tick(tick_time) if tick_time is not None else None
        self._account, self._terminal = account, terminal

    def symbol_info_tick(self, _symbol): return self._tick
    def account_info(self): return Account() if self._account else None
    def terminal_info(self): return Terminal() if self._terminal else None


def at(offset_hours: float, age_seconds: float = 0.0) -> float:
    """A server tick stamp for a clock `offset_hours` ahead, `age` seconds old."""
    return (NOW - timedelta(seconds=age_seconds)).timestamp() + offset_hours * 3600


class TestOffsetArithmetic:
    @pytest.mark.parametrize("hours", [0.0, 2.0, 3.0, -5.0, 5.5, 5.75])
    def test_it_recovers_the_offset(self, hours):
        assert server_offset_hours(at(hours), NOW) == hours

    def test_latency_and_clock_drift_are_snapped_away(self):
        """A few seconds either way is not a timezone."""
        assert server_offset_hours(at(2.0) + 7, NOW) == 2.0
        assert server_offset_hours(at(2.0) - 11, NOW) == 2.0

    def test_it_snaps_to_a_quarter_hour(self):
        assert server_offset_hours(at(2.0) + 400, NOW) == 2.0     # +6.7 min
        assert server_offset_hours(at(2.0) + 900, NOW) == 2.25    # +15 min

    @pytest.mark.parametrize("hours,ok", [(0, True), (14, True), (-12, True),
                                          (15, False), (-13, False), (-48, False)])
    def test_implausible_offsets_are_rejected(self, hours, ok):
        assert plausible_offset(hours) is ok


class TestReadServerClock:
    def test_a_live_tick_measures_the_clock(self):
        clock = read_server_clock(FakeMT5(at(2.0)), "EURUSD", NOW)
        assert clock["utc_offset_hours"] == 2.0
        assert clock["server"] == "Broker-Live"
        assert clock["measured_from"] == "EURUSD"

    def test_a_weekend_stale_tick_is_rejected(self):
        """Two days of staleness differences into ~-48h, which is no timezone."""
        assert read_server_clock(FakeMT5(at(2.0, age_seconds=48 * 3600)), "EURUSD", NOW) is None

    def test_a_few_hours_stale_cannot_be_detected(self):
        """The honest limit of a one-reading measurement.

        A tick three hours old on a UTC+2 clock is arithmetically identical to
        a live tick on a UTC-1 clock. Nothing here can tell them apart, so the
        fetcher compares against the recorded value and warns instead.
        """
        clock = read_server_clock(FakeMT5(at(2.0, age_seconds=3 * 3600)), "EURUSD", NOW)
        assert clock["utc_offset_hours"] == -1.0

    def test_no_tick_at_all(self):
        assert read_server_clock(FakeMT5(None), "EURUSD", NOW) is None
        assert read_server_clock(FakeMT5(0), "EURUSD", NOW) is None

    def test_a_terminal_that_answers_nothing_still_measures(self):
        clock = read_server_clock(FakeMT5(at(3.0), account=False, terminal=False), "X", NOW)
        assert clock["utc_offset_hours"] == 3.0
        assert clock["server"] == "" and clock["company"] == ""


class TestRoundTrip:
    def test_what_the_fetcher_writes_is_what_the_engine_reads(self, tmp_path):
        from backtester.utils.timeutil import broker_offset

        path = tmp_path / "broker.json"
        write_server_clock(read_server_clock(FakeMT5(at(2.0)), "EURUSD", NOW), path)
        assert broker_offset(path) == 2.0
        assert "utc_offset_hours" in json.loads(path.read_text())

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        from backtester.utils.timeutil import broker_offset

        assert broker_offset(tmp_path / "absent.json") is None
        assert broker_offset(tmp_path / "absent.json", default=2.0) == 2.0

    def test_a_corrupt_file_falls_back(self, tmp_path):
        from backtester.utils.timeutil import broker_offset

        bad = tmp_path / "broker.json"
        bad.write_text("{not json", encoding="utf-8")
        assert broker_offset(bad, default=2.0) == 2.0

    def test_the_fetcher_can_read_back_what_it_recorded(self, tmp_path):
        """`main` compares against this to notice a clock that has changed."""
        path = tmp_path / "broker.json"
        assert recorded_offset(path) is None
        write_server_clock(read_server_clock(FakeMT5(at(3.0)), "X", NOW), path)
        assert recorded_offset(path) == 3.0
