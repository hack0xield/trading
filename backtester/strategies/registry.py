"""Name -> strategy class lookup, so scripts take `--strategy day_open`.

Adding a strategy means writing one file and decorating the class with
`@register`; the import in `strategies/__init__.py` is the only other line
needed.
"""

from __future__ import annotations

from dataclasses import fields

from ..core.strategy import Strategy

_REGISTRY: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    """Class decorator. Registers under `cls.name`."""
    name = getattr(cls, "name", "")
    if not name or name == "base":
        raise ValueError(f"{cls.__name__} must define a unique `name`")
    if name in _REGISTRY and _REGISTRY[name] is not cls:
        raise ValueError(f"Strategy name {name!r} is already taken by {_REGISTRY[name].__name__}")
    _REGISTRY[name] = cls
    return cls


def get_strategy(name: str) -> type[Strategy]:
    key = str(name).strip()
    if key not in _REGISTRY:
        raise KeyError(f"Unknown strategy {name!r}. Available: {', '.join(available())}")
    return _REGISTRY[key]


def available() -> list[str]:
    return sorted(_REGISTRY)


def describe(name: str) -> str:
    """Parameter listing for `--list-strategies`."""
    cls = get_strategy(name)
    lines = [f"{cls.name}  -  {cls.description or cls.__doc__ or ''}".strip()]
    for f in fields(cls.params_class):
        annotation = f.type if isinstance(f.type, str) else getattr(f.type, "__name__", f.type)
        lines.append(f"    {f.name:<20} {str(annotation):<16} default={f.default!r}")
    return "\n".join(lines)


def describe_all() -> str:
    return "\n\n".join(describe(name) for name in available())
