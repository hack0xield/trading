"""Signal strategies.

Every module is imported here so `@register` is enough to make
`strategy: <name>` work from a job config.
"""

from . import margin_zones  # noqa: F401  (imported for its @register side effect)
from .base import SignalStrategy, bar_facts
from .registry import available, describe, describe_all, get_strategy, register

__all__ = [
    "SignalStrategy", "available", "bar_facts", "describe", "describe_all",
    "get_strategy", "register",
]
