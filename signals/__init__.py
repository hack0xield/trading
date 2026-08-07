"""Scheduled market signals: compute on a timer, deliver to Telegram.

No LLM in the runtime path — the decision to send is arithmetic, so it is code
that gives the same answer every run and can be unit-tested. See
`signals/README.md` for where this can and cannot be deployed.
"""

from .config import Group, Job, SignalConfig, load_config
from .runner import JobResult, mt5_available, run_all, run_job
from .telegram import Delivery, Notifier

__all__ = [
    "Delivery", "Group", "Job", "JobResult", "Notifier", "SignalConfig",
    "load_config", "mt5_available", "run_all", "run_job",
]
