"""Signal config, message composition and delivery.

Nothing here touches the network: `Notifier` in dry-run mode records what it
would have sent. The one thing worth being paranoid about is that a job cannot
silently deliver to the wrong people, so group resolution is tested harder
than the maths.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from signals.config import Job, SignalConfig, load_config
from signals.runner import run_job
from signals.strategies import get_strategy
from signals.telegram import MAX_LEN, Notifier, escape

UTC = timezone.utc


def write_config(tmp_path, body: dict) -> str:
    path = tmp_path / "signals.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return str(path)


BASE = {
    "groups": {
        "ops": {"chat_ids": ["-100111"], "description": "operators"},
        "eurusd": {"chat_ids": ["-100222"]},
    },
    "jobs": [{"name": "j1", "symbol": "EURUSD", "notify": ["eurusd"], "on_error": ["ops"]}],
}


class TestConfig:
    def test_loads_groups_and_jobs(self, tmp_path):
        config = load_config(write_config(tmp_path, BASE))
        assert set(config.groups) == {"ops", "eurusd"}
        assert config.jobs[0].name == "j1"
        assert config.jobs[0].timeframe == "H4"      # default

    def test_numeric_chat_ids_become_strings(self, tmp_path):
        body = {"groups": {"g": {"chat_ids": [-100333, 42]}}, "jobs": []}
        config = load_config(write_config(tmp_path, body))
        assert config.groups["g"].chat_ids == ["-100333", "42"]

    def test_unknown_group_fails_at_load_not_at_send(self, tmp_path):
        """A typo must surface before the work is done, not after."""
        body = {"groups": {"ops": {"chat_ids": ["1"]}},
                "jobs": [{"name": "j", "notify": ["typoed"]}]}
        with pytest.raises(KeyError, match="typoed"):
            load_config(write_config(tmp_path, body))

    def test_unknown_job_key_is_rejected(self, tmp_path):
        body = {"groups": {}, "jobs": [{"name": "j", "symbl": "EURUSD"}]}
        with pytest.raises(SystemExit, match="unknown key"):
            load_config(write_config(tmp_path, body))

    def test_missing_file_explains_how_to_fix_it(self, tmp_path):
        with pytest.raises(SystemExit, match="signals.example"):
            load_config(tmp_path / "nope.yaml")

    def test_chat_ids_are_deduplicated_across_groups(self, tmp_path):
        """Overlapping groups must not mean two copies of one message."""
        body = {"groups": {"a": {"chat_ids": ["1", "2"]}, "b": {"chat_ids": ["2", "3"]}},
                "jobs": []}
        config = load_config(write_config(tmp_path, body))
        assert config.chat_ids_for(["a", "b"]) == ["1", "2", "3"]

    def test_env_token_beats_the_file(self, tmp_path, monkeypatch):
        (tmp_path / "t.json").write_text(json.dumps({"bot_token": "from-file"}))
        config = SignalConfig(token_file=str(tmp_path / "t.json"))
        assert config.resolve_token() == "from-file"
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-env")
        assert config.resolve_token() == "from-env"

    def test_no_token_anywhere_is_none(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        assert SignalConfig(token_file=str(tmp_path / "absent.json")).resolve_token() is None


def stub_evaluate(monkeypatch, result: dict) -> None:
    """Replace the strategy's evaluate, so run_job can be tested without data."""
    from signals.strategies.margin_zones import MarginZonesSignal

    monkeypatch.setattr(MarginZonesSignal, "evaluate", lambda self, job, config: result)


def render(job: Job, f: dict) -> str:
    """Compose through the strategy, the way the runner does."""
    return get_strategy(job.strategy)(job.params).compose(job, f)


def facts(**over) -> dict:
    base = {
        "symbol": "EURUSD", "timeframe": "H4", "bars": 7110,
        "first_bar": datetime(2022, 1, 3, tzinfo=UTC),
        "last_bar": datetime(2026, 7, 28, 16, tzinfo=UTC),
        "last_close": 1.14008, "age_hours": 2.0, "stale": False,
        "pivots": 63, "zones": 63, "margin_readings": 9, "digits": 4,
    }
    base.update(over)
    return base


class TestCompose:
    def test_message_carries_the_facts_that_prove_the_data_path(self):
        text = render(Job(name="j1", symbol="EURUSD"), facts())
        assert "EURUSD H4" in text
        assert "1.1401" in text                 # last close, at instrument precision
        assert "7,110 bars" in text
        assert "63 confirmed pivots" in text

    def test_stale_data_is_called_out(self):
        text = render(Job(name="j1"), facts(stale=True, age_hours=240))
        assert "Stale" in text and "240h" in text

    def test_fresh_data_says_nothing_about_staleness(self):
        assert "Stale" not in render(Job(name="j1"), facts())

    def test_it_says_it_is_not_a_trade_signal(self):
        """Until a condition exists, the message must not read like one."""
        assert "not a trade signal" in render(Job(name="j1"), facts())

    def test_symbol_is_escaped(self):
        text = render(Job(name="a<b>c", symbol="X&Y"), facts(symbol="X&Y"))
        assert "&amp;" in text and "&lt;b&gt;" in text


class TestNotifier:
    def test_dry_run_records_but_does_not_send(self):
        n = Notifier(token="t", dry_run=True)
        out = n.send(["1", "2"], "hello")
        assert [d.ok for d in out] == [True, True]
        assert all("dry-run" in d.detail for d in out)

    def test_missing_token_is_a_failed_delivery_not_a_crash(self):
        out = Notifier(token=None, dry_run=False).send(["1"], "hi")
        assert out[0].ok is False and "token" in out[0].detail

    def test_no_recipients_is_a_no_op(self):
        assert Notifier(dry_run=True).send([], "hi") == []

    def test_long_messages_are_truncated_to_the_api_limit(self, monkeypatch):
        """Telegram rejects anything over 4096, so trim before the network."""
        from signals.telegram import Delivery

        seen = {}
        monkeypatch.setattr(
            Notifier, "_post",
            lambda self, chat, text: seen.setdefault("len", len(text))
            or Delivery(chat, True, ""),
        )
        Notifier(token="t", dry_run=False).send(["1"], "x" * (MAX_LEN + 500))
        assert seen["len"] <= MAX_LEN
        assert seen["len"] > MAX_LEN - 100      # trimmed, not gutted

    def test_escape(self):
        assert escape("a<b>&c") == "a&lt;b&gt;&amp;c"


class TestRunJob:
    def test_a_failure_routes_to_the_error_group(self, tmp_path):
        config = load_config(write_config(tmp_path, BASE))
        job = config.jobs[0]
        job.refresh = False
        job.symbol = "NOSUCHSYMBOL"          # load_bars will raise

        result = run_job(job, config)
        assert result.ok is False
        assert result.recipients == ["ops"]          # not the signal channel
        assert "failed" in result.message

    def test_stale_data_also_notifies_the_operator(self, monkeypatch, tmp_path):
        config = load_config(write_config(tmp_path, BASE))
        job = config.jobs[0]
        job.refresh = False
        stub_evaluate(monkeypatch, facts(stale=True))

        result = run_job(job, config)
        assert result.ok is True
        assert set(result.recipients) == {"eurusd", "ops"}

    def test_healthy_run_goes_only_to_the_signal_group(self, monkeypatch, tmp_path):
        config = load_config(write_config(tmp_path, BASE))
        job = config.jobs[0]
        job.refresh = False
        stub_evaluate(monkeypatch, facts())

        assert run_job(job, config).recipients == ["eurusd"]

    def test_refresh_is_skipped_when_metatrader_is_absent(self, monkeypatch, tmp_path):
        import signals.runner as runner

        config = load_config(write_config(tmp_path, BASE))
        job = config.jobs[0]
        job.refresh = True
        monkeypatch.setattr(runner, "mt5_available", lambda: (False, "wine not installed"))
        stub_evaluate(monkeypatch, facts())

        result = run_job(job, config)
        assert result.ok is True
        assert "skipped" in result.refreshed and "wine" in result.refreshed


class TestArtifacts:
    """Bars are upserted in place, so a report is the only record of what a
    message was computed from."""

    def test_bad_artifacts_value_is_rejected_at_load(self, tmp_path):
        body = dict(BASE)
        body["jobs"] = [{"name": "j", "artifacts": "sometimes"}]
        with pytest.raises(SystemExit, match="always/on_signal/none"):
            load_config(write_config(tmp_path, body))

    def test_always_writes_a_report(self, monkeypatch, tmp_path):
        config = load_config(write_config(tmp_path, BASE))
        job = config.jobs[0]
        job.refresh, job.artifacts = False, "always"
        stub_evaluate(monkeypatch, facts())
        _stub_artifacts(monkeypatch, tmp_path / "report")

        result = run_job(job, config)
        assert result.artifacts.endswith("report")

    def test_none_writes_nothing(self, monkeypatch, tmp_path):
        config = load_config(write_config(tmp_path, BASE))
        job = config.jobs[0]
        job.refresh, job.artifacts = False, "none"
        stub_evaluate(monkeypatch, facts())
        _stub_artifacts(monkeypatch, tmp_path / "report")

        assert run_job(job, config).artifacts == ""

    def test_on_signal_holds_off_until_something_fires(self, monkeypatch, tmp_path):
        config = load_config(write_config(tmp_path, BASE))
        job = config.jobs[0]
        job.refresh, job.artifacts = False, "on_signal"
        _stub_artifacts(monkeypatch, tmp_path / "report")

        stub_evaluate(monkeypatch, facts())
        assert run_job(job, config).artifacts == ""

        stub_evaluate(monkeypatch, facts(signal=True))
        assert run_job(job, config).artifacts.endswith("report")

    def test_a_broken_report_does_not_lose_the_signal(self, monkeypatch, tmp_path):
        """The message matters more than the evidence."""
        from signals.strategies.margin_zones import MarginZonesSignal

        config = load_config(write_config(tmp_path, BASE))
        job = config.jobs[0]
        job.refresh, job.artifacts = False, "always"
        stub_evaluate(monkeypatch, facts())

        def boom(self, job, config, facts):
            raise OSError("disk full")

        monkeypatch.setattr(MarginZonesSignal, "write_artifacts", boom)
        result = run_job(job, config)
        assert result.ok is True                      # signal survives
        assert "EURUSD" in result.message
        assert "disk full" in result.artifacts        # failure is reported


def _stub_artifacts(monkeypatch, directory):
    from signals.strategies.margin_zones import MarginZonesSignal

    monkeypatch.setattr(
        MarginZonesSignal, "write_artifacts", lambda self, job, config, facts: directory
    )
