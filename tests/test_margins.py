"""Margin zones: the maths, the contract specs, and the reading log.

The anchor for the whole file is the worked example in `Margin Zones.md`:

    MM 2900, PP 6.25, NP 2  ->  FMZ 232, IMZ 255

If `test_the_note_worked_example` ever fails, the implementation has drifted
from the strategy it is supposed to encode, whatever else still passes.

Network access to CME is not exercised by default — see `test_cme_access` at
the bottom for why, and run with `--network` to check whether it has changed.
"""

from __future__ import annotations

import json
from datetime import date, timedelta


import pytest

from backtester.data.margins import (
    CONTRACT_DIR,
    ContractSpec,
    MarginLog,
    MarginObservation,
    compute_zones,
    list_specs,
    load_spec,
    save_spec,
    validate_observation,
)

TODAY = date(2026, 7, 27)


def spec_6e() -> ContractSpec:
    return ContractSpec(
        code="6E", name="Euro FX futures", exchange="CME",
        contract_size=125_000.0, base_currency="EUR", quote_currency="USD",
        tick_size=0.00005, tick_value=6.25, pip_size=0.0001,
    )


def spec_gc() -> ContractSpec:
    return ContractSpec(
        code="GC", name="Gold futures", exchange="COMEX",
        contract_size=100.0, base_currency="XAU", quote_currency="USD",
        tick_size=0.10, tick_value=10.0, pip_size=0.10,
    )


def obs(maintenance=2900.0, as_of=TODAY, **kw) -> MarginObservation:
    kw.setdefault("source", "test")
    return MarginObservation(code=kw.pop("code", "6E"), as_of=as_of,
                             maintenance=maintenance, **kw)


class TestTheFormula:
    def test_the_note_worked_example(self):
        """MM 2900, PP 6.25, NP 2 -> FMZ 232, IMZ 255. The whole strategy in one line."""
        zones = compute_zones(spec_6e(), obs(2900.0))
        assert zones.fmz == pytest.approx(232.0)
        assert zones.imz == pytest.approx(255.2)
        assert zones.mr == pytest.approx(23.2)

    def test_pip_value_is_pp_times_np(self):
        spec = spec_6e()
        assert spec.np == pytest.approx(2.0)
        assert spec.pip_value == pytest.approx(12.50)

    def test_fmz_is_the_move_that_burns_the_margin(self):
        """The economic meaning: FMZ pips against you == the maintenance margin."""
        spec, o = spec_6e(), obs(2900.0)
        zones = compute_zones(spec, o)
        loss = zones.fmz * spec.pip_value  # pips * dollars-per-pip
        assert loss == pytest.approx(o.maintenance)

    def test_a_published_initial_margin_beats_the_ratio(self):
        zones = compute_zones(spec_6e(), obs(2900.0, initial=3300.0))
        assert zones.initial == 3300.0
        assert zones.imz == pytest.approx(3300.0 / 12.50)

    def test_initial_ratio_is_configurable(self):
        zones = compute_zones(spec_6e(), obs(2900.0), initial_ratio=1.25)
        assert zones.imz == pytest.approx(232.0 * 1.25)

    def test_zero_margin_is_rejected(self):
        with pytest.raises(ValueError, match="must be > 0"):
            compute_zones(spec_6e(), obs(0.0))


class TestNPDoesNotGeneralise:
    """The note says NP is always 2. It is not, and on gold that halves the zones."""

    def test_np_is_ticks_per_pip_not_a_constant(self):
        assert spec_6e().np == pytest.approx(2.0)   # 0.0001 / 0.00005
        assert spec_gc().np == pytest.approx(1.0)   # 0.10 / 0.10

    def test_assuming_np_2_on_gold_halves_every_zone(self):
        spec = spec_gc()
        correct = compute_zones(spec, obs(12_000.0, code="GC"))
        wrong_pip_value = spec.tick_value * 2  # what "NP always 2" would give
        assert correct.fmz == pytest.approx(12_000.0 / spec.pip_value)
        assert 12_000.0 / wrong_pip_value == pytest.approx(correct.fmz / 2)

    def test_gold_zone_is_a_plausible_move(self):
        spec = spec_gc()
        zones = compute_zones(spec, obs(12_000.0, code="GC"))
        # 1,200 pips of 0.10 = $120/oz, ~3% at $4,000 gold. Sane for one day.
        assert zones.fmz == pytest.approx(1200.0)
        assert zones.fmz * spec.pip_size == pytest.approx(120.0)


class TestSpecConsistency:
    def test_a_good_spec_has_no_problems(self):
        assert spec_6e().problems() == []
        assert spec_gc().problems() == []

    def test_a_mistyped_tick_value_is_caught(self):
        """contract_size*pip_size and tick_value*NP must agree; this is the guard."""
        spec = spec_6e()
        spec.tick_value = 0.625  # off by 10x
        problems = spec.problems()
        assert any("pip value disagrees" in p for p in problems)

    def test_a_mistyped_contract_size_is_caught(self):
        spec = spec_6e()
        spec.contract_size = 12_500.0
        assert any("ERROR" in p for p in spec.problems())

    def test_missing_numbers_are_errors(self):
        spec = ContractSpec(code="XX", name="broken")
        problems = spec.problems()
        assert len(problems) >= 4
        assert all(p.startswith("ERROR") for p in problems)

    def test_a_fractional_tick_ratio_warns(self):
        spec = spec_6e()
        spec.pip_size = 0.00007
        assert any("not a whole number of ticks" in p for p in spec.problems())


class TestShippedSpecs:
    """The JSON in configs/contracts must survive the same checks."""

    def test_specs_are_present(self):
        codes = list_specs()
        assert {"6E", "6B", "GC"} <= set(codes)

    @pytest.mark.parametrize("code", ["6E", "6B", "GC"])
    def test_each_shipped_spec_is_self_consistent(self, code):
        assert load_spec(code).problems() == []

    def test_the_shipped_6e_reproduces_the_note(self):
        zones = compute_zones(load_spec("6E"), obs(2900.0))
        assert zones.fmz == pytest.approx(232.0)

    def test_every_spec_carries_a_verification_pointer(self):
        """These were not fetched from CME, so each must say where to check it."""
        for code in list_specs():
            data = json.loads((CONTRACT_DIR / f"{code}.json").read_text(encoding="utf-8"))
            assert data.get("_verify"), f"{code}.json has no _verify pointer"

    def test_unknown_code_lists_what_exists(self):
        with pytest.raises(FileNotFoundError, match="Available"):
            load_spec("NOPE")

    def test_spec_round_trips_through_json(self, tmp_path):
        save_spec(spec_6e(), tmp_path)
        assert load_spec("6E", tmp_path) == spec_6e()

    def test_unknown_json_keys_are_ignored(self, tmp_path):
        """`_note` / `_verify` are comments, and must not break loading."""
        (tmp_path / "ZZ.json").write_text(json.dumps({
            "code": "ZZ", "name": "x", "contract_size": 1.0, "tick_size": 0.1,
            "tick_value": 0.1, "pip_size": 0.1, "_note": "hi", "_verify": "there",
        }), encoding="utf-8")
        assert load_spec("ZZ", tmp_path).code == "ZZ"


class TestValidation:
    def test_a_clean_reading_passes(self):
        assert validate_observation(obs(2900.0), spec_6e(), today=TODAY) == []

    def test_future_dates_are_rejected(self):
        problems = validate_observation(obs(2900.0, as_of=TODAY + timedelta(days=1)),
                                        spec_6e(), today=TODAY)
        assert any("in the future" in p and "ERROR" in p for p in problems)

    def test_a_stale_reading_warns(self):
        """The note asks for a weekly check; this is what enforces it."""
        problems = validate_observation(obs(2900.0, as_of=TODAY - timedelta(days=30)),
                                        spec_6e(), today=TODAY)
        assert any("30 days old" in p and "WARN" in p for p in problems)

    def test_a_reading_inside_the_window_does_not_warn(self):
        problems = validate_observation(obs(2900.0, as_of=TODAY - timedelta(days=5)),
                                        spec_6e(), today=TODAY)
        assert not any("old" in p for p in problems)

    def test_initial_below_maintenance_is_an_error(self):
        problems = validate_observation(obs(2900.0, initial=2000.0), spec_6e(), today=TODAY)
        assert any("below maintenance" in p for p in problems)

    def test_a_missing_source_warns(self):
        o = MarginObservation(code="6E", as_of=TODAY, maintenance=2900.0, source="")
        assert any("no source" in p for p in validate_observation(o, spec_6e(), today=TODAY))

    def test_a_big_margin_jump_warns(self):
        previous = obs(2900.0, as_of=TODAY - timedelta(days=7))
        problems = validate_observation(obs(4500.0), spec_6e(), today=TODAY, previous=previous)
        assert any("moved" in p and "WARN" in p for p in problems)

    def test_a_small_margin_change_is_quiet(self):
        previous = obs(2900.0, as_of=TODAY - timedelta(days=7))
        problems = validate_observation(obs(3000.0), spec_6e(), today=TODAY, previous=previous)
        assert not any("moved" in p for p in problems)

    def test_an_implausible_zone_is_caught(self):
        """A 10x pip-value error shows up as a nonsense one-day move."""
        problems = validate_observation(obs(2_000_000.0), spec_6e(), today=TODAY)
        assert any("not a plausible" in p for p in problems)


class TestLevels:
    def test_levels_project_up_from_a_low(self):
        spec = spec_6e()
        zones = compute_zones(spec, obs(2900.0))
        levels = zones.levels(1.0850, spec, direction=1, fractions=(0.5, 1.0))
        assert levels["50%"] == pytest.approx(1.0850 + 116 * 0.0001)
        assert levels["100%"] == pytest.approx(1.0850 + 232 * 0.0001)

    def test_levels_project_down_from_a_high(self):
        spec = spec_6e()
        zones = compute_zones(spec, obs(2900.0))
        levels = zones.levels(1.0850, spec, direction=-1, fractions=(1.0,))
        assert levels["100%"] == pytest.approx(1.0850 - 232 * 0.0001)

    def test_the_basis_changes_the_scale(self):
        """FMZ 232 vs MR 23.2 — a 10x difference, which is why it is a parameter."""
        spec = spec_6e()
        zones = compute_zones(spec, obs(2900.0))
        on_fmz = zones.levels(1.0, spec, fractions=(1.0,), basis="fmz")["100%"]
        on_mr = zones.levels(1.0, spec, fractions=(1.0,), basis="mr")["100%"]
        assert on_fmz - 1.0 == pytest.approx(10 * (on_mr - 1.0))

    def test_an_unknown_basis_is_rejected(self):
        spec = spec_6e()
        with pytest.raises(ValueError, match="basis must be"):
            compute_zones(spec, obs(2900.0)).levels(1.0, spec, basis="nonsense")


class TestMarginLog:
    def test_round_trip(self, tmp_path):
        log = MarginLog(tmp_path / "m.csv")
        log.add(obs(2900.0))
        rows = log.read()
        assert len(rows) == 1
        assert rows[0].maintenance == 2900.0
        assert rows[0].as_of == TODAY
        assert rows[0].source == "test"

    def test_missing_file_reads_empty(self, tmp_path):
        assert MarginLog(tmp_path / "nope.csv").read() == []

    def test_same_code_and_date_is_replaced_not_duplicated(self, tmp_path):
        log = MarginLog(tmp_path / "m.csv")
        log.add(obs(2900.0))
        log.add(obs(3100.0))
        rows = log.read()
        assert len(rows) == 1
        assert rows[0].maintenance == 3100.0

    def test_latest_returns_the_most_recent(self, tmp_path):
        log = MarginLog(tmp_path / "m.csv")
        log.add(obs(2900.0, as_of=date(2026, 1, 1)))
        log.add(obs(3100.0, as_of=date(2026, 6, 1)))
        assert log.latest("6E").maintenance == 3100.0

    def test_latest_never_looks_ahead(self, tmp_path):
        """A backtest on a 2026-01 bar must not see the 2026-06 margin."""
        log = MarginLog(tmp_path / "m.csv")
        log.add(obs(2900.0, as_of=date(2026, 1, 1)))
        log.add(obs(3100.0, as_of=date(2026, 6, 1)))
        assert log.latest("6E", on=date(2026, 3, 1)).maintenance == 2900.0

    def test_latest_before_the_first_reading_is_none(self, tmp_path):
        log = MarginLog(tmp_path / "m.csv")
        log.add(obs(2900.0, as_of=date(2026, 6, 1)))
        assert log.latest("6E", on=date(2026, 1, 1)) is None

    def test_codes_are_kept_apart(self, tmp_path):
        log = MarginLog(tmp_path / "m.csv")
        log.add(obs(2900.0))
        log.add(obs(12_000.0, code="GC"))
        assert log.latest("6E").maintenance == 2900.0
        assert log.latest("GC").maintenance == 12_000.0
        assert len(log.for_code("6E")) == 1

    def test_optional_initial_survives_a_round_trip(self, tmp_path):
        log = MarginLog(tmp_path / "m.csv")
        log.add(obs(2900.0, initial=3300.0))
        log.add(obs(2900.0, as_of=TODAY - timedelta(days=1)))
        rows = log.read()
        assert rows[0].initial is None
        assert rows[1].initial == 3300.0

    def test_the_shipped_log_parses(self):
        """configs/margins.csv may be empty, but it must always be readable."""
        MarginLog().read()


class TestCliEndToEnd:
    def test_add_then_show(self, tmp_path, capsys):
        import scripts.margins as cli

        log = tmp_path / "m.csv"
        assert cli.main(["add", "--code", "6E", "--maintenance", "2900",
                         "--as-of", TODAY.isoformat(), "--source", "test",
                         "--log", str(log)]) == 0
        assert "232" in capsys.readouterr().out

        assert cli.main(["show", "--code", "6E", "--log", str(log),
                         "--pivot", "1.0850"]) == 0
        out = capsys.readouterr().out
        assert "FMZ" in out and "232" in out

    def test_add_rejects_a_bad_reading(self, tmp_path, capsys):
        import scripts.margins as cli

        code = cli.main(["add", "--code", "6E", "--maintenance", "-5",
                         "--log", str(tmp_path / "m.csv")])
        assert code == 1
        assert "Not recorded" in capsys.readouterr().out

    def test_show_without_a_reading_explains_how_to_add_one(self, tmp_path, capsys):
        import scripts.margins as cli

        assert cli.main(["show", "--code", "6E", "--log", str(tmp_path / "empty.csv")]) == 1
        assert "add --code 6E" in capsys.readouterr().out

    def test_contracts_command_passes_its_own_checks(self, capsys):
        import scripts.margins as cli

        assert cli.main(["contracts"]) == 0
        assert "ERROR" not in capsys.readouterr().out

    def test_check_only_calls_the_newest_reading_stale(self, tmp_path, capsys):
        """A six-year import must not emit one 'this is old' warning per row."""
        import scripts.margins as cli
        from backtester.data.margins import MarginLog, MarginObservation

        log = MarginLog(tmp_path / "m.csv")
        for year, mm in ((2021, 2000.0), (2022, 2400.0), (2023, 2650.0)):
            log.add(MarginObservation("6E", date(year, 1, 4), mm, source="CME export"))

        cli.main(["check", "--log", str(tmp_path / "m.csv")])
        out = capsys.readouterr().out
        assert out.count("days old") == 1
        assert "2023-01-04" in out and "no errors" in out


@pytest.mark.network
def test_cme_access(cme_url):
    """Documents that CME refuses scripted access — run with `--network`.

    Skipped by default: it needs the internet, and a test that fails because a
    third party is (correctly) blocking robots would make the suite look broken.
    It is here so that if CME ever opens an endpoint, or the block lifts, there
    is one place that finds out. It makes a single plain request and does not
    attempt to disguise itself.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        cme_url, headers={"User-Agent": "trading-backtester/0.1 (personal research)"}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code

    if status == 403:
        pytest.xfail("CME blocks scripted access (403) — margins must be entered by hand")
    assert status == 200, f"unexpected status {status}"
