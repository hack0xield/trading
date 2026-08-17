"""Run the scheduled jobs: refresh data, compute, compose, notify.

The shape is deliberately boring — no LLM, no judgement, one code path that
produces the same output for the same inputs. Whatever decides *when* to
signal lives in the strategy's `evaluate`; the runner only decides who hears
about the result and what happens when it breaks. Adding a strategy therefore
never touches this file.

The refresh step is the only part that is environment-dependent. On the
machine with MetaTrader it pulls fresh bars; anywhere else — a cloud runner,
a laptop with the terminal shut — it reports that it skipped and the run
continues against whatever is in the store. That distinction is surfaced in
the message rather than hidden, because a signal computed on week-old bars is
worse than no signal.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from .config import Job, SignalConfig  # noqa: E402
from .strategies import get_strategy  # noqa: E402
from .telegram import escape  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@dataclass(slots=True)
class JobResult:
    job: str
    ok: bool = True
    refreshed: str = "skipped"
    error: str = ""
    message: str = ""
    artifacts: str = ""
    facts: dict = field(default_factory=dict)
    recipients: list[str] = field(default_factory=list)


def mt5_available() -> tuple[bool, str]:
    """Can this machine actually reach a MetaTrader terminal?

    Checked rather than assumed: the whole point of the cloud question is that
    the answer differs by host, and the run must degrade instead of failing.
    """
    if not (ROOT / "scripts" / "fetch-mt5.sh").exists():
        return False, "scripts/fetch-mt5.sh missing"
    if shutil.which("wine") is None:
        return False, "wine not installed (MetaTrader5 is a Windows binary)"
    if not (Path.home() / ".mt5" / "drive_c" / "Python311" / "python.exe").exists():
        return False, "no Wine Python in ~/.mt5"
    return True, "ok"


def refresh(job: Job, config: SignalConfig, timeout: int = 900) -> str:
    """Pull fresh bars if this host can. Returns a human-readable status."""
    ok, why = mt5_available()
    if not ok:
        return f"skipped ({why})"

    fetch = [
        str(ROOT / "scripts" / "fetch-mt5.sh"),
        "--symbol", job.symbol, "--timeframe", job.timeframe,
        "--start", (datetime.now(timezone.utc).date().replace(month=1, day=1)).isoformat(),
        "--out", "csv://data/incoming",
    ]
    convert = [
        sys.executable, str(ROOT / "scripts" / "manage_data.py"), "convert",
        "--from", "csv://data/incoming", "--to", config.data, "--symbol", job.symbol,
    ]
    try:
        for command in (fetch, convert):
            # Not capture_output=True: that pipes stdout/stderr, and a pipe
            # only reports EOF once *every* process holding its write end
            # closes it. fetch-mt5.sh's mt5.initialize() can auto-launch the
            # MT5 terminal as a side effect if it is not already running,
            # and that terminal inherits the pipe (Wine's CreateProcess
            # inherits stdio handles by default) — then holds it open for as
            # long as it keeps running, which is indefinitely, since it is a
            # GUI app with no reason to exit. subprocess.run() then blocks
            # forever waiting for an EOF that only comes from closing every
            # holder, not just this command's own (already-exited) process.
            # A temp file has no such multi-writer EOF semantics: the parent
            # only needs its direct child to exit, which it already does.
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as out:
                done = subprocess.run(
                    command, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT,
                    text=True, timeout=timeout,
                )
                if done.returncode != 0:
                    out.seek(0)
                    tail = out.read().strip().splitlines()[-1:] or ["no output"]
                    return f"failed ({Path(command[0]).name}: {tail[0][:120]})"
        return "fetched"
    except subprocess.TimeoutExpired:
        return f"failed (timed out after {timeout}s)"
    except Exception as exc:
        return f"failed ({type(exc).__name__}: {exc})"


def run_job(job: Job, config: SignalConfig) -> JobResult:
    result = JobResult(job=job.name)
    if job.refresh:
        result.refreshed = refresh(job, config)

    try:
        strategy = get_strategy(job.strategy)(job.params)
        facts = strategy.evaluate(job, config)
    except Exception as exc:
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"
        result.recipients = list(job.on_error) or list(job.notify)
        result.message = (
            f"❌ <b>{escape(job.name)}</b> failed\n\n"
            f"<code>{escape(result.error)}</code>\n\n"
            f"Data refresh: {escape(result.refreshed)}"
        )
        return result

    result.facts = facts
    result.message = strategy.compose(job, facts)

    # "on_signal" only writes when something fired; until a condition exists
    # nothing ever does, so it behaves as "none" for now.
    fired = bool(facts.get("signal"))
    if job.artifacts == "always" or (job.artifacts == "on_signal" and fired):
        try:
            directory = strategy.write_artifacts(job, config, facts)
            if directory:
                result.artifacts = str(directory)
        except Exception as exc:
            # A failed report must not lose the signal itself.
            result.artifacts = f"failed ({type(exc).__name__}: {exc})"

    result.recipients = list(job.notify)
    if facts.get("stale") and job.on_error:
        # A stale feed is an operations problem, so the operator hears about it
        # too — without suppressing the signal itself.
        for name in job.on_error:
            if name not in result.recipients:
                result.recipients.append(name)
    return result


def run_all(config: SignalConfig, only: str | None = None) -> list[JobResult]:
    results = []
    for job in config.jobs:
        if not job.enabled or (only and job.name != only):
            continue
        results.append(run_job(job, config))
    return results
