"""Strategy implementations.

Every strategy module is imported here so that decorating a class with
`@register` is enough to make `--strategy <name>` work from the CLI.
"""

from . import day_open, sma_cross  # noqa: F401  (imported for their @register side effect)
from .margin_zones import mz50  # noqa: F401
from .registry import available, describe, describe_all, get_strategy, register

__all__ = ["available", "describe", "describe_all", "get_strategy", "register"]
