"""Shared fixtures. Also puts the repo root on the path so `backtester` imports."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtester.core.broker import ExecutionConfig, SimulatedBroker  # noqa: E402
from backtester.core.instrument import Instrument  # noqa: E402
from backtester.core.types import Bar  # noqa: E402

UTC = timezone.utc
START = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.fixture
def gold() -> Instrument:
    """XAUUSD with costs switched off, so test arithmetic stays exact.

    contract_size 100 and tick_value 1.0 mean a $1 move on 0.1 lot is $10.
    """
    return Instrument(
        symbol="XAUUSD",
        digits=2,
        contract_size=100.0,
        tick_size=0.01,
        min_volume=0.01,
        volume_step=0.01,
        spread_points=0.0,
        commission_per_lot=0.0,
        swap_long=0.0,
        swap_short=0.0,
    )


@pytest.fixture
def config() -> ExecutionConfig:
    return ExecutionConfig(
        initial_balance=10_000.0,
        spread_points=0.0,
        slippage_points=0.0,
        commission_per_lot=0.0,
        apply_swap=False,
        use_bar_spread=False,
    )


@pytest.fixture
def broker(gold, config) -> SimulatedBroker:
    return SimulatedBroker(gold, config)


def bar(
    minute: int = 0,
    open_: float = 1800.0,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
    spread: float = 0.0,
    step: timedelta = timedelta(minutes=15),
) -> Bar:
    """A bar `minute` steps after START; high/low default to spanning open/close."""
    close = open_ if close is None else close
    return Bar(
        time=START + minute * step,
        open=open_,
        high=max(open_, close) if high is None else high,
        low=min(open_, close) if low is None else low,
        close=close,
        volume=100,
        spread=spread,
    )


def series(prices: list[float], **kwargs) -> list[Bar]:
    """Bars whose open and close both sit at the given price."""
    return [bar(i, open_=p, close=p, **kwargs) for i, p in enumerate(prices)]


@pytest.fixture
def make_bar():
    return bar


@pytest.fixture
def make_series():
    return series


# --------------------------------------------------------------- network opt-in

def pytest_addoption(parser):
    parser.addoption(
        "--network",
        action="store_true",
        default=False,
        help="also run tests that reach the public internet",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "network: needs the public internet; opt in with --network")


def pytest_collection_modifyitems(config, items):
    """Skip network tests unless asked for.

    The suite has to stay runnable offline and deterministic — a red bar caused
    by someone else's firewall teaches you nothing about this code.
    """
    if config.getoption("--network"):
        return
    skip = pytest.mark.skip(reason="needs --network")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def cme_url() -> str:
    return "https://www.cmegroup.com/markets/fx/g10/euro-fx.margins.html"
