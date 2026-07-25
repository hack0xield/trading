"""Argument parsing shared by the scripts.

A run is defined by a config file, command-line flags, or both — flags win, so
a YAML file can hold the stable part of a setup while the shell varies one
knob. `--param k=v` is repeatable and typed against the strategy's parameter
dataclass, which means a misspelling fails immediately instead of being ignored
for the length of the run.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .core.broker import ExecutionConfig
from .core.engine import EngineConfig
from .core.instrument import Instrument, load_instrument

DEFAULT_DATA = "parquet://data/bars"


def load_config(path: str | Path | None) -> dict:
    """Read a YAML or JSON run config. YAML support is optional."""
    if not path:
        return {}
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if file.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                f"{file} is YAML but PyYAML is not installed. "
                "pip install pyyaml, or use a .json config."
            ) from exc
        return yaml.safe_load(text) or {}
    return json.loads(text)


def parse_kv(pairs: list[str] | None) -> dict:
    """`['stop_pct=2', 'direction=SELL']` -> dict. Values stay strings."""
    out: dict[str, str] = {}
    for item in pairs or []:
        if "=" not in item:
            raise argparse.ArgumentTypeError(f"Expected key=value, got {item!r}")
        key, _, value = item.partition("=")
        out[key.strip()] = value.strip()
    return out


def add_data_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("data")
    group.add_argument("--symbol", "-s", help="e.g. XAUUSD")
    group.add_argument("--timeframe", "-t", help="M1 M5 M15 H1 H4 D1 ...")
    group.add_argument(
        "--data",
        "-d",
        help=f"store URI: parquet://dir | csv://dir | sqlite:///f.db | postgresql://... "
        f"(default {DEFAULT_DATA})",
    )
    group.add_argument("--start", help="first bar, e.g. 2023-01-01")
    group.add_argument("--end", help="last bar, inclusive")
    group.add_argument(
        "--no-validate", action="store_true", help="skip the bar-quality checks"
    )


def add_execution_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("execution costs")
    group.add_argument("--balance", type=float, help="starting balance (default 10000)")
    group.add_argument("--leverage", type=float, help="account leverage (default 100)")
    group.add_argument(
        "--spread-points", type=float, help="fixed spread in points; omit to use the bar's own"
    )
    group.add_argument("--slippage-points", type=float, help="points lost on every fill")
    group.add_argument("--commission", type=float, help="commission per lot per side")
    group.add_argument("--no-swap", action="store_true", help="ignore overnight financing")
    group.add_argument(
        "--intrabar",
        choices=["conservative", "optimistic", "ohlc"],
        help="which level wins when a bar contains both stop and target "
        "(default conservative: the stop)",
    )


def add_strategy_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("strategy")
    group.add_argument("--strategy", "-S", help="registered strategy name")
    group.add_argument(
        "--param",
        "-p",
        action="append",
        metavar="KEY=VALUE",
        help="strategy parameter; repeatable",
    )
    group.add_argument("--warmup", type=int, help="bars to skip before trading starts")


def merge(config: dict, args: argparse.Namespace) -> dict:
    """Fold CLI flags over a config file. Only flags that were given count."""
    merged = dict(config)
    for key in ("symbol", "timeframe", "data", "start", "end", "strategy"):
        value = getattr(args, key, None)
        if value is not None:
            merged[key] = value

    params = dict(merged.get("params") or {})
    params.update(parse_kv(getattr(args, "param", None)))
    merged["params"] = params

    execution = dict(merged.get("execution") or {})
    flag_map = {
        "balance": "initial_balance",
        "leverage": "leverage",
        "spread_points": "spread_points",
        "slippage_points": "slippage_points",
        "commission": "commission_per_lot",
        "intrabar": "intrabar",
    }
    for flag, field in flag_map.items():
        value = getattr(args, flag, None)
        if value is not None:
            execution[field] = value
    if getattr(args, "no_swap", False):
        execution["apply_swap"] = False
    merged["execution"] = execution

    if getattr(args, "warmup", None) is not None:
        merged["warmup_bars"] = args.warmup
    if getattr(args, "no_validate", False):
        merged["validate"] = False
    return merged


def execution_from(config: dict) -> ExecutionConfig:
    known = {f for f in ExecutionConfig.__dataclass_fields__}
    data = {k: v for k, v in (config.get("execution") or {}).items() if k in known}
    unknown = set(config.get("execution") or {}) - known
    if unknown:
        raise ValueError(
            f"Unknown execution setting(s): {', '.join(sorted(unknown))}. "
            f"Valid: {', '.join(sorted(known))}"
        )
    return ExecutionConfig(**data)


def engine_from(config: dict) -> EngineConfig:
    return EngineConfig(
        warmup_bars=int(config.get("warmup_bars", 0)),
        close_at_end=bool(config.get("close_at_end", True)),
        progress_every=int(config.get("progress_every", 0)),
    )


def instrument_from(config: dict, symbol: str) -> Instrument:
    """Instrument spec from the config's `instrument:` block, else the spec file."""
    inst = load_instrument(symbol, config.get("spec_dir"))
    for key, value in (config.get("instrument") or {}).items():
        if not hasattr(inst, key):
            raise ValueError(f"Unknown instrument field {key!r}")
        setattr(inst, key, value)
    return inst


def require(config: dict, *keys: str) -> None:
    missing = [k for k in keys if not config.get(k)]
    if missing:
        raise SystemExit(
            f"Missing required setting(s): {', '.join(missing)}. "
            f"Pass them as flags or put them in a --config file."
        )
