"""Name -> signal strategy lookup.

The same shape as `backtester/strategies/registry.py`, and for the same
reason: a job names its strategy in config, so adding one is a file plus a
decorator rather than an edit to the runner.
"""

from __future__ import annotations

from dataclasses import fields

from .base import SignalStrategy

_REGISTRY: dict[str, type[SignalStrategy]] = {}


def register(cls: type[SignalStrategy]) -> type[SignalStrategy]:
    name = getattr(cls, "name", "")
    if not name or name == "base":
        raise ValueError(f"{cls.__name__} must define a unique `name`")
    if name in _REGISTRY and _REGISTRY[name] is not cls:
        raise ValueError(f"Signal strategy {name!r} is taken by {_REGISTRY[name].__name__}")
    _REGISTRY[name] = cls
    return cls


def get_strategy(name: str) -> type[SignalStrategy]:
    key = str(name).strip()
    if key not in _REGISTRY:
        raise KeyError(f"Unknown signal strategy {name!r}. Available: {', '.join(available())}")
    return _REGISTRY[key]


def available() -> list[str]:
    return sorted(_REGISTRY)


def describe(name: str) -> str:
    cls = get_strategy(name)
    lines = [f"{cls.name}  -  {cls.description}".strip()]
    for f in fields(cls.params_class):
        lines.append(f"    {f.name:<18} default={f.default!r}")
    return "\n".join(lines)


def describe_all() -> str:
    return "\n\n".join(describe(n) for n in available())
