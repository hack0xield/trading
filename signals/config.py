"""Signal configuration: who gets told what, and which jobs produce it.

Deliberately separated from the strategy code. A job says *what to compute*;
a recipient group says *who hears about it*; the two are joined by name, so
adding a channel or re-pointing a job is a config edit rather than a code
change.

Secrets never live here. The Telegram bot token is read from a separate file
that is gitignored, the same arrangement as `mt5-mcp-server/config.json`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class Group:
    """A set of Telegram chats that receive the same messages."""

    name: str
    chat_ids: list[str] = field(default_factory=list)
    description: str = ""


@dataclass(slots=True)
class Job:
    """One thing to compute on a schedule, and who to tell about it.

    Only `symbol` and `timeframe` are universal — every strategy reads a bar
    series. Everything else a particular strategy needs goes in `params`,
    typed and validated against that strategy's own parameter block, so a
    margin-zone job carries `contract` and a future breakout job carries
    whatever it needs without either polluting the shared schema.
    """

    name: str
    strategy: str = "margin_zones"
    symbol: str = "EURUSD"
    timeframe: str = "H4"
    params: dict = field(default_factory=dict)
    refresh: bool = True          # try to pull fresh bars before computing
    # What to leave behind in runs/. Bars are upserted in place, so the state
    # that produced a message is not reconstructible afterwards — "always" is
    # the only setting that makes a past signal auditable.
    artifacts: str = "always"     # always | on_signal | none
    notify: list[str] = field(default_factory=list)
    on_error: list[str] = field(default_factory=list)
    enabled: bool = True


@dataclass(slots=True)
class SignalConfig:
    """Shared plumbing. Anything strategy-specific lives in `Job.params`."""

    groups: dict[str, Group] = field(default_factory=dict)
    jobs: list[Job] = field(default_factory=list)
    token_file: str = "configs/telegram.json"
    data: str = "parquet://data/bars"     # where bars live — shared by every strategy

    def group(self, name: str) -> Group:
        if name not in self.groups:
            raise KeyError(
                f"Unknown recipient group {name!r}. Defined: {', '.join(sorted(self.groups))}"
            )
        return self.groups[name]

    def chat_ids_for(self, names: list[str]) -> list[str]:
        """Chat ids for several groups, de-duplicated, order preserved.

        Two groups may legitimately share a chat — the operator channel often
        overlaps a signal channel — and nobody wants the message twice.
        """
        seen: list[str] = []
        for name in names:
            for chat in self.group(name).chat_ids:
                if chat not in seen:
                    seen.append(chat)
        return seen

    def resolve_token(self) -> str | None:
        """Bot token from the environment, else the token file.

        The environment wins so a scheduled cloud run can inject it as a secret
        without a file ever existing in the checkout.
        """
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if token:
            return token
        path = Path(self.token_file)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return (json.load(fh).get("bot_token") or "").strip() or None


def load_config(path: str | Path) -> SignalConfig:
    path = Path(path)
    if not path.exists():
        raise SystemExit(
            f"No signal config at {path}.\n"
            f"Copy the example:  cp configs/signals.example.yaml {path}"
        )
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise SystemExit("PyYAML is needed for a .yaml config; or use .json") from exc
        raw = yaml.safe_load(text) or {}
    else:
        raw = json.loads(text)

    groups = {
        name: Group(name=name, **{k: v for k, v in (body or {}).items()
                                  if k in ("chat_ids", "description")})
        for name, body in (raw.get("groups") or {}).items()
    }
    # Chat ids are often numeric in YAML; Telegram wants them as strings.
    for group in groups.values():
        group.chat_ids = [str(c) for c in group.chat_ids]

    known = set(Job.__slots__)
    jobs = []
    for body in raw.get("jobs") or []:
        unknown = set(body) - known
        if unknown:
            raise SystemExit(
                f"{path}: job {body.get('name', '?')!r} has unknown key(s): "
                f"{', '.join(sorted(unknown))}"
            )
        jobs.append(Job(**body))

    config = SignalConfig(
        groups=groups,
        jobs=jobs,
        token_file=raw.get("token_file", "configs/telegram.json"),
        data=raw.get("data", "parquet://data/bars"),
    )

    # Fail at load, not at send time: a typo'd group name or parameter should
    # not surface halfway through a scheduled run, after the work is done.
    from .strategies import get_strategy

    for job in config.jobs:
        for name in list(job.notify) + list(job.on_error):
            config.group(name)
        if job.artifacts not in ("always", "on_signal", "none"):
            raise SystemExit(
                f"{path}: job {job.name!r} — artifacts must be "
                f"always/on_signal/none, got {job.artifacts!r}"
            )
        try:
            get_strategy(job.strategy)(job.params)
        except KeyError as exc:
            raise SystemExit(f"{path}: job {job.name!r} — {exc.args[0]}") from exc
        except ValueError as exc:
            raise SystemExit(f"{path}: job {job.name!r} params — {exc}") from exc
    return config
