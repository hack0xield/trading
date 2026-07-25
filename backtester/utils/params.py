"""Typed strategy parameters that survive a round-trip through the CLI.

Everything reaching a strategy from outside — `--param stop_pct=2`, a YAML file,
an optimizer sweep — arrives as strings or loosely typed scalars. Coercing them
against the dataclass annotations here means a strategy body can rely on
`self.p.stop_pct` being a float, and a typo in a parameter name fails loudly at
startup instead of being silently ignored for a 10-minute run.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass, fields, asdict

TRUE_VALUES = {"1", "true", "yes", "y", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "off"}


def _unwrap_optional(annotation):
    """`int | None` -> `int`; leaves everything else alone."""
    origin = typing.get_origin(annotation)
    if origin is typing.Union or str(origin) == "types.UnionType":
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def coerce(value, annotation):
    if value is None:
        return None
    target = _unwrap_optional(annotation)

    if target is bool or annotation is bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in TRUE_VALUES:
            return True
        if text in FALSE_VALUES:
            return False
        raise ValueError(f"Cannot read {value!r} as a boolean")

    origin = typing.get_origin(target)
    if origin in (list, tuple, set):
        item_type = (typing.get_args(target) or (str,))[0]
        items = value.split(",") if isinstance(value, str) else list(value)
        return [coerce(str(i).strip(), item_type) for i in items]

    if target in (int, float, str):
        if target is int and isinstance(value, str) and value.strip() == "":
            return None
        # "2.0" -> 2 rather than a ValueError, since sweeps produce both.
        if target is int and isinstance(value, str):
            return int(float(value))
        return target(value)

    return value


@dataclass
class StrategyParams:
    """Base for a strategy's parameter block."""

    @classmethod
    def from_dict(cls, data: dict | None = None, **overrides):
        merged = dict(data or {})
        merged.update(overrides)
        known = {f.name: f.type for f in fields(cls)}

        unknown = set(merged) - set(known)
        if unknown:
            raise ValueError(
                f"Unknown parameter(s) for {cls.__name__}: {', '.join(sorted(unknown))}. "
                f"Valid: {', '.join(sorted(known))}"
            )

        kwargs = {}
        for name, value in merged.items():
            annotation = known[name]
            # `from __future__ import annotations` leaves these as strings.
            if isinstance(annotation, str):
                annotation = _ANNOTATION_LOOKUP.get(annotation, annotation)
            try:
                kwargs[name] = coerce(value, annotation)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Bad value for {name}={value!r}: {exc}") from exc
        return cls(**kwargs)

    def to_dict(self) -> dict:
        return asdict(self)

    def describe(self) -> str:
        return ", ".join(f"{k}={v}" for k, v in sorted(self.to_dict().items()))


# Resolving string annotations without eval; covers what parameter blocks use.
_ANNOTATION_LOOKUP = {
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "int | None": typing.Optional[int],
    "float | None": typing.Optional[float],
    "str | None": typing.Optional[str],
    "bool | None": typing.Optional[bool],
    "list[int]": typing.List[int],
    "list[str]": typing.List[str],
    "list[float]": typing.List[float],
    "list[int] | None": typing.Optional[typing.List[int]],
    "list[str] | None": typing.Optional[typing.List[str]],
}
