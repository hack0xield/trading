"""Margin zones on the ZigZag candidate: versions, crossings, and the run.

Everything here turns on one property: a zone anchored on a *moving* candidate
must never let a later anchor touch an earlier observation. Most of what
follows is that property approached from different directions.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from backtester.core.engine import Backtester
from backtester.core.types import Bar
from backtester.indicators.zigzag import Candidate, ZigZagTracker
from backtester.strategies import get_strategy
from backtester.strategies.margin_zones.crossing import CrossingTracker
from backtester.strategies.margin_zones.margins import MarginZones
from backtester.strategies.margin_zones.mz50 import MZ50Strategy
from backtester.strategies.margin_zones.zones import INITIAL, STRICT_EXTENSION, ZoneTracker

UTC = timezone.utc
START = datetime(2024, 1, 1, tzinfo=UTC)

# 100 pips of FMZ and 110 of IMZ at a pip_size of 1.0, so from a high at 2000:
#   MZ0 1900, MZ100 1890, midpoint 1895, e50 = (2000 + 1895) / 2 = 1947.5
ZONES = MarginZones(code="TEST", as_of=date(2023, 1, 1), maintenance=10_000.0,
                    initial=11_000.0, pip_value=100.0, fmz=100.0, imz=110.0, mz=10.0)


def h4(i: int, price: float) -> Bar:
    return Bar(time=START + timedelta(hours=4 * i), open=price, high=price,
               low=price, close=price, volume=100, spread=0.0)


def zones_for(_candidate):
    return ZONES


def feed(prices, **kw):
    tracker = ZoneTracker(zones_for, pip_size=1.0, deviation_abs=80.0, **kw)
    out = [tracker.push(h4(i, p)) for i, p in enumerate(prices)]
    return tracker, out


class TestCandidate:
    """What the ZigZag is currently tracking, and what counts as a change."""

    def test_no_candidate_before_a_direction_exists(self):
        """With direction 0 the tracker watches both ends; nothing to anchor on."""
        zz = ZigZagTracker(deviation_abs=80.0)
        zz.push(h4(0, 1800))
        assert zz.candidate is None

    def test_the_candidate_is_the_running_extreme(self):
        zz = ZigZagTracker(deviation_abs=80.0)
        for i, p in enumerate([1800, 1900, 1860, 2000]):
            zz.push(h4(i, p))
        assert zz.candidate.kind == "high"
        assert zz.candidate.price == 2000

    def test_a_strict_extension_is_an_update(self):
        a = Candidate(leg=1, kind="high", price=100.0, index=0, time=START)
        assert Candidate(leg=1, kind="high", price=101.0, index=1, time=START).extends(a)

    def test_an_equal_extreme_is_not_an_update(self):
        """It may move the ZigZag index, but no level changed."""
        a = Candidate(leg=1, kind="high", price=100.0, index=0, time=START)
        assert not Candidate(leg=1, kind="high", price=100.0, index=9, time=START).extends(a)

    def test_a_different_leg_is_not_an_update(self):
        a = Candidate(leg=1, kind="high", price=100.0, index=0, time=START)
        assert not Candidate(leg=2, kind="low", price=90.0, index=5, time=START).extends(a)


class TestZoneVersions:
    """One immutable zone per anchor, levels frozen at creation."""

    def test_levels_follow_the_e50_formula(self):
        """e50 is anchor +- (dFMZ + dIMZ) / 4, not the zone's midpoint."""
        _, out = feed([1800, 1900, 1860, 2000, 1960])
        version = next(v for v in out if v is not None)
        assert version.kind == "high"
        anchor = version.anchor_price
        assert version.mz0 == pytest.approx(anchor - 100)
        assert version.mz100 == pytest.approx(anchor - 110)
        assert version.mz50 == pytest.approx(anchor - 105)
        assert version.e50 == pytest.approx(anchor - 52.5)

    def test_a_strict_extension_creates_a_new_version(self):
        tracker, _ = feed([1800, 1900, 1860, 2000, 2050, 2100])
        events = [v.event_type for v in tracker.versions]
        assert events[0] == INITIAL
        assert STRICT_EXTENSION in events
        assert [v.anchor_price for v in tracker.versions] == sorted(
            v.anchor_price for v in tracker.versions
        )

    def test_an_equal_high_creates_no_version(self):
        tracker, _ = feed([1800, 1900, 1860, 2000, 2000, 2000])
        anchors = [v.anchor_price for v in tracker.versions]
        assert anchors.count(2000) == 1

    def test_versions_are_immutable(self):
        tracker, _ = feed([1800, 1900, 1860, 2000, 2050])
        first = tracker.versions[0]
        with pytest.raises(Exception):
            first.anchor_price = 999.0

    def test_a_version_is_never_knowable_before_its_bar(self):
        tracker, _ = feed([1800, 1900, 1860, 2000, 2050])
        for v in tracker.versions:
            assert v.known_time >= v.anchor_time

    def test_superseding_records_when_the_previous_zone_ended(self):
        tracker, _ = feed([1800, 1900, 1860, 2000, 2050, 2100])
        for older, newer in zip(tracker.versions, tracker.versions[1:]):
            index, time = tracker.superseded[older.zone_id]
            assert index == newer.known_index and time == newer.known_time
        assert tracker.versions[-1].zone_id not in tracker.superseded

    def test_a_candidate_with_no_margin_reading_makes_no_zone(self):
        tracker = ZoneTracker(lambda c: None, pip_size=1.0, deviation_abs=80.0)
        for i, p in enumerate([1800, 1900, 1860, 2000]):
            tracker.push(h4(i, p))
        assert tracker.versions == [] and tracker.uncovered


class TestCrossing:
    """When two rollover observations count as having crossed a zone's level."""

    def series(self, tail: list[float], hold: float = 1960) -> list[float]:
        """A high candidate standing at 2000, then one price per rollover day.

        Six H4 bars make a day. e50 for a 2000 anchor is 1947.5 and the ZigZag
        threshold is 80, so the hold and the tail stay above 1920 — otherwise
        the candidate confirms, the leg flips, and the zone under test is gone
        before price reaches its level.
        """
        prices = [1800, 1900, 2000]
        prices += [hold] * 21
        for price in tail:
            prices += [price] * 6
        return prices + [tail[-1]] * 6 if tail else prices

    def run(self, tail, hold: float = 1960):
        tracker = CrossingTracker(zones_for, pip_size=1.0, deviation_abs=80.0)
        fired = []
        for i, p in enumerate(self.series(tail, hold)):
            fired += tracker.push(h4(i, p))
        return tracker, fired

    def test_a_downward_crossing_from_a_high_is_true(self):
        _, fired = self.run([1960, 1940])
        assert len(fired) == 1
        assert fired[0].classification == "True"
        assert fired[0].direction == "down"
        assert not fired[0].is_long

    def test_a_crossing_back_out_is_false(self):
        """Held below e50, so the only crossing available is the one outward."""
        _, fired = self.run([1960], hold=1940)
        assert len(fired) == 1
        assert fired[0].classification == "False"
        assert fired[0].direction == "up"

    def test_sitting_exactly_on_the_level_is_neutral(self):
        _, fired = self.run([1947.5, 1940])
        assert fired == []

    def test_observations_under_different_versions_do_not_pair(self):
        """The safeguard the whole versioning exists for."""
        tracker = CrossingTracker(zones_for, pip_size=1.0, deviation_abs=80.0)
        prices = [1800, 1900, 2000] + [1960] * 21
        prices += [1960] * 6          # one observation under the 2000 anchor
        prices += [2100] * 12         # a strict extension: new anchor, new level
        fired = []
        for i, p in enumerate(prices):
            fired += tracker.push(h4(i, p))
        assert fired == []

    def test_a_crossing_is_never_recorded_before_its_zone_existed(self):
        _, fired = self.run([1960, 1940])
        for crossing in fired:
            assert crossing.time > crossing.zone.known_time


# ------------------------------------------------------------------ strategy

@pytest.fixture
def desk(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "TEST.json").write_text(json.dumps({
        "code": "TEST", "name": "Test future", "exchange": "TEST",
        "contract_size": 100.0, "base_currency": "XAU", "quote_currency": "USD",
        "tick_size": 1.0, "tick_value": 100.0, "pip_size": 1.0,
    }))
    log = tmp_path / "margins.csv"
    log.write_text("code,as_of,maintenance,initial,source,note\nTEST,2023-01-01,5000.0,,t,\n")
    return {"contract": "TEST", "contracts_dir": str(contracts),
            "margin_log": str(log), "deviation_pips": 80.0}


def crossing_setup() -> list[Bar]:
    """A high candidate at 2000, then a downward crossing of its e50.

    A 5,000 maintenance over a 100 pip value gives dFMZ 50 and dIMZ 55, so from
    2000: MZ0 1950, MZ100 1945, midpoint 1947.5, e50 1973.75. The ZigZag
    threshold is 80 points, comfortably wider than the 26.25 from anchor to
    e50 — otherwise the candidate confirms before price can reach the level.
    """
    prices = [1800, 1900, 2000] + [1980] * 21
    for price in (1980, 1970):               # 1973.75 crossed downward
        prices += [price] * 6
    prices += [1970] * 6
    prices += [1944] * 12
    return [h4(i, p) for i, p in enumerate(prices)]


def run(strategy, bars, gold, config):
    return Backtester(strategy=strategy, symbol="XAUUSD", timeframe="H4",
                      instrument=gold, execution=config).run(bars)


class TestStrategy:
    def test_it_is_registered(self):
        assert get_strategy("mz50") is MZ50Strategy

    def test_it_places_no_orders(self, desk, gold, config):
        strategy = MZ50Strategy(**desk)
        result = run(strategy, crossing_setup(), gold, config)
        assert result.trades == []
        assert strategy.tracker.crossings

    def test_place_orders_is_off_by_default(self, desk):
        assert MZ50Strategy(**desk).p.place_orders is False

    def test_turning_place_orders_on_still_trades_nothing_yet(self, desk, gold, config):
        result = run(MZ50Strategy(**desk, place_orders=True), crossing_setup(), gold, config)
        assert result.trades == []

    def test_the_records_are_published(self, desk, gold, config):
        strategy = MZ50Strategy(**desk)
        result = run(strategy, crossing_setup(), gold, config)
        assert {"zones", "pivots", "rollover", "crossings"} <= set(result.artifacts)

    def test_every_crossing_names_a_zone_the_run_recorded(self, desk, gold, config):
        result = run(MZ50Strategy(**desk), crossing_setup(), gold, config)
        zone_ids = {z["zone_id"] for z in result.artifacts["zones"]}
        assert zone_ids
        for crossing in result.artifacts["crossings"]:
            assert crossing["zone_id"] in zone_ids

    def test_no_zone_is_recorded_before_the_bar_that_made_it_knowable(
        self, desk, gold, config
    ):
        strategy = MZ50Strategy(**desk)
        run(strategy, crossing_setup(), gold, config)
        for version in strategy.tracker.zones.versions:
            assert version.known_index >= version.anchor_index


class TestChart:
    """The run draws the margin-zone chart from its own forward pass."""

    def payload(self, desk, gold, config, tmp_path) -> dict:
        import re

        from backtester.data.results import save_result
        from backtester.metrics import compute

        strategy = MZ50Strategy(**desk)
        result = run(strategy, crossing_setup(), gold, config)
        directory = save_result(result, compute(result).to_dict(), root=tmp_path)
        assert strategy.chart(directory, "parquet://data/bars", "H4") is not None
        page = (directory / "chart.html").read_text(encoding="utf-8")
        return json.loads(re.search(r'(\{"symbol".*?\})\s*;', page, re.S).group(1))

    def test_the_layers_are_present(self, desk, gold, config, tmp_path):
        payload = self.payload(desk, gold, config, tmp_path)
        assert payload["pivots"], "the ZigZag went missing"
        assert payload["zones"], "the margin zones went missing"
        assert payload["rollover"], "the rollover observations went missing"
        assert payload["crossings"], "the crossings went missing"

    def test_the_title_names_the_strategy(self, desk, gold, config, tmp_path):
        assert self.payload(desk, gold, config, tmp_path)["strategy"] == "mz50"

    def test_a_zone_spans_from_when_it_became_knowable(self, desk, gold, config, tmp_path):
        """i0 is the bar that created the version, never the bar holding the anchor."""
        payload = self.payload(desk, gold, config, tmp_path)
        for zone in payload["zones"]:
            assert zone["i0"] >= zone["ai"]
            assert zone["i1"] >= zone["i0"]

    def test_the_candidate_still_in_progress_is_drawn(self, desk, gold, config, tmp_path):
        payload = self.payload(desk, gold, config, tmp_path)
        assert payload["cand"] is not None
        assert payload["cand"]["confirmAt"] is not None


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="needs node")
class TestChartRenders:
    """The page's script runs to completion under a stub DOM.

    A throw anywhere in it leaves the page blank — no chart, no tables, and no
    error visible without opening a console — so it is checked headlessly.
    """

    def render(self, desk, gold, config, tmp_path):
        import subprocess

        from backtester.data.results import save_result
        from backtester.metrics import compute

        strategy = MZ50Strategy(**desk)
        result = run(strategy, crossing_setup(), gold, config)
        directory = save_result(result, compute(result).to_dict(), root=tmp_path)
        page = strategy.chart(directory, "parquet://data/bars", "H4")
        return subprocess.run(
            ["node", "tests/render_check.js", str(page)],
            capture_output=True, text=True,
        )

    def test_the_page_runs_and_draws(self, desk, gold, config, tmp_path):
        out = self.render(desk, gold, config, tmp_path)
        assert out.returncode == 0, out.stdout + out.stderr
        assert "OK: script ran to completion" in out.stdout
        assert "zone rows   : 0" not in out.stdout
        assert "pivot rows  : 0" not in out.stdout

    def test_the_title_says_what_the_run_was(self, desk, gold, config, tmp_path):
        out = self.render(desk, gold, config, tmp_path)
        title = next(
            line.split(":", 1)[1].strip()
            for line in out.stdout.splitlines() if line.strip().startswith("title")
        )
        assert title.startswith("XAUUSD H4 — mz50,")
        assert "zone versions" in title
