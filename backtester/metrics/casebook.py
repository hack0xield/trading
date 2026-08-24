"""The Backtest Database and its aggregate views.

Implements `impl-spec/Claude Specification_ Backtest Data Collection and
Reporting.md`: one row per trade in a `Backtest Database`, six aggregate tables
that are *views* over it, and a report built from those. Every figure in the
report comes from the one table, which is the point of the specification —
`Total Statistics`, `Direction`, `Models`, `Sessions`, `Weekdays` and `Months`
must never be computed independently, or they drift apart.

Three things the specification leaves to the implementation, decided here:

**What counts as break-even.** BE is a third of the sample data and 17% of its
cases, but nothing defines it — the reference book's BE reasons are handwritten
("carried to the next day"). A trade is BE here when it returned less than
`be_threshold` in either direction, which is mechanical and needs no strategy
behaviour. It matters more than it looks: the specification's winrate is
`W / (W + L)`, with BE excluded from the denominator, so misclassifying a
flat trade moves the headline number. On the reference data the same 184 cases
score 78.95% that way and 64.17% counted over all trades.

**What `Models` holds.** In the reference book it is a discretionary label —
BOS, Inversion, Engulfing — chosen by the person reviewing the chart. A
backtest has no equivalent, so it carries the strategy's name.

**Which hours a session covers.** The specification's five windows span twelve
hours of the twenty-four; the rest were simply hours the author did not trade.
An automated run fires whenever its rule does, so the gaps are filled with
buckets named after themselves (`18:00-03:00`), leaving every trade classified
and the table summing to Total Trades.

Hours are read straight off the bar timestamps, which are broker-server time
(see `CLAUDE.md`). That is deliberate: this broker's clock follows New York's
daylight saving, so the sessions sit at stable broker-local hours and move
around in UTC. Converting would introduce the error, not remove it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..core.types import BacktestResult, Trade

#: The specification's §3.4 windows, as `(name, from_hour, to_hour)`.
SPEC_SESSIONS: tuple[tuple[str, int, int], ...] = (
    ("Asia", 3, 6),
    ("Frankfurt", 9, 10),
    ("London", 10, 12),
    ("Lunch", 12, 14),
    ("New York", 14, 18),
)

#: A trade returning less than this in either direction is break-even.
DEFAULT_BE_THRESHOLD = 0.1

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def session_windows(
    named: tuple[tuple[str, int, int], ...] = SPEC_SESSIONS,
) -> list[tuple[str, int, int]]:
    """The named windows plus a self-named bucket for every hour they miss.

    `[("Asia", 3, 6)]` alone leaves 21 hours homeless; this returns the gaps
    too, as `("06:00-03:00", 6, 27)`. The wrap-around bucket runs past 24 so a
    single `from <= hour < to` test covers it after adding 24 to the hour.
    """
    if not named:
        raise ValueError("at least one named session is needed")
    ordered = sorted(named, key=lambda w: w[1])
    out: list[tuple[str, int, int]] = []
    cursor = ordered[0][1]
    for name, start, end in ordered:
        if start > cursor:
            out.append((f"{cursor:02d}:00-{start:02d}:00", cursor, start))
        out.append((name, start, end))
        cursor = end
    first = ordered[0][1]
    if cursor % 24 != first:
        out.append((f"{cursor % 24:02d}:00-{first:02d}:00", cursor, first + 24))
    return out


def session_for(hour: int, windows: list[tuple[str, int, int]]) -> str:
    for name, start, end in windows:
        if start <= hour < end or start <= hour + 24 < end:
            return name
    return "Unclassified"          # unreachable while windows come from above


@dataclass(frozen=True, slots=True)
class Case:
    """One row of the Backtest Database (§2)."""

    number: int
    pair: str
    trade: Trade
    model: str
    session: str
    be_threshold: float

    # ------------------------------------------------------------ classifiers

    @property
    def direction(self) -> str:
        return "Long" if self.trade.side.value == "BUY" else "Short"

    @property
    def weekday(self) -> str:
        return WEEKDAYS[self.trade.entry_time.weekday()]

    @property
    def month(self) -> str:
        return f"{self.trade.entry_time:%Y %b}"

    @property
    def day(self) -> date:
        return self.trade.entry_time.date()

    # ---------------------------------------------------------------- R units

    @property
    def rr(self) -> float:
        """Potential reward-to-risk, from the bracket the trade carried."""
        return self.trade.planned_rr or 0.0

    @property
    def risk(self) -> float:
        """Risk in R. One by definition — R *is* the unit of risk."""
        return 1.0

    @property
    def r_result(self) -> float:
        """Realised return in R.

        The specification books a win as its full planned RR. That holds when
        price reached the target, and overstates when a trade left for any
        other reason — a time stop, or the run ending mid-trade — so what is
        actually realised is used instead. For a target or stop exit the two
        agree, which is most trades.
        """
        return self.trade.r_multiple or 0.0

    @property
    def result(self) -> str:
        if abs(self.r_result) <= self.be_threshold:
            return "BE"
        return "Win" if self.r_result > 0 else "Lose"

    @property
    def result_pct(self) -> float:
        """§4: the R this case contributed. Zero for BE by construction."""
        return 0.0 if self.result == "BE" else round(self.r_result, 4)

    def as_row(self) -> dict:
        """§2's columns. The five review fields are for a human to fill in."""
        trade = self.trade
        return {
            "Case #": self.number,
            "Pair": self.pair,
            "Date": trade.entry_time.isoformat(),
            "Weekday": self.weekday,
            "Direction": self.direction,
            "Models": self.model,
            "Sessions": self.session,
            "Months": self.month,
            "RR": round(self.rr, 2),
            "Result": self.result,
            "Risk": self.risk,
            "Result Pct": self.result_pct,
            "Win?": "Yes" if self.result == "Win" else "No",
            "Lose?": "Yes" if self.result == "Lose" else "No",
            "BE?": "Yes" if self.result == "BE" else "No",
            "BE Reason": "",
            "News Event": "",
            "Mistake": "",
            "To Improve": "",
            "Needs validation": "",
            # Beyond the specification, but a case you cannot trace back to a
            # trade is not reviewable.
            "entry_price": trade.entry_price,
            "exit_price": trade.exit_price,
            "stop_loss": trade.sl,
            "take_profit": trade.tp,
            "exit_reason": trade.reason.value,
            "net_pnl": round(trade.net_pnl, 2),
            "tag": trade.tag,
        }


def build(
    result: BacktestResult,
    be_threshold: float = DEFAULT_BE_THRESHOLD,
    sessions: tuple[tuple[str, int, int], ...] = SPEC_SESSIONS,
) -> list[Case]:
    """The Backtest Database for one run, in trade order."""
    windows = session_windows(sessions)
    return [
        Case(
            number=n,
            pair=result.symbol,
            trade=trade,
            model=result.strategy,
            session=session_for(trade.entry_time.hour, windows),
            be_threshold=be_threshold,
        )
        for n, trade in enumerate(result.trades, start=1)
    ]


# --------------------------------------------------------------- aggregations

def _stats(cases: list[Case]) -> dict:
    """§4, for any slice of the database.

    `winrate` divides by wins plus losses, not by every case: §3.1 defines it
    that way and break-even trades are neither. Counting them in the
    denominator drags the figure toward zero for a strategy that simply exits
    flat often.
    """
    wins = [c for c in cases if c.result == "Win"]
    losses = [c for c in cases if c.result == "Lose"]
    decided = len(wins) + len(losses)
    return {
        "trades": len(cases),
        "wins": len(wins),
        "losses": len(losses),
        "be": len(cases) - decided,
        "winrate_pct": (len(wins) / decided * 100.0) if decided else 0.0,
        "gained_rr": sum(c.result_pct for c in cases),
        "average_rr": (sum(c.rr for c in cases) / len(cases)) if cases else 0.0,
    }


def _grouped(cases: list[Case], key, order: list[str] | None = None) -> list[dict]:
    buckets: dict[str, list[Case]] = {}
    for case in cases:
        buckets.setdefault(key(case), []).append(case)
    names = [n for n in (order or []) if n in buckets] + [
        n for n in buckets if n not in (order or [])
    ]
    return [{"name": n, **_stats(buckets[n])} for n in names]


def total_statistics(cases: list[Case]) -> dict:
    return _stats(cases)


def by_direction(cases: list[Case]) -> list[dict]:
    return _grouped(cases, lambda c: c.direction, ["Long", "Short"])


def by_model(cases: list[Case]) -> list[dict]:
    return _grouped(cases, lambda c: c.model)


def by_session(
    cases: list[Case], sessions: tuple[tuple[str, int, int], ...] = SPEC_SESSIONS
) -> list[dict]:
    windows = session_windows(sessions)
    rows = _grouped(cases, lambda c: c.session, [w[0] for w in windows])
    spans = {name: f"{start % 24:02d}:00-{end % 24:02d}:00" for name, start, end in windows}
    for row in rows:
        row["time"] = spans.get(row["name"], "")
    return rows


def by_weekday(cases: list[Case]) -> list[dict]:
    return _grouped(cases, lambda c: c.weekday, list(WEEKDAYS))


def by_month(cases: list[Case]) -> list[dict]:
    rows = _grouped(cases, lambda c: c.month)
    # `%Y %b` sorts alphabetically by month name, which is not chronological.
    first = {c.month: c.trade.entry_time for c in cases}
    return sorted(rows, key=lambda r: first[r["name"]])


def tables(
    cases: list[Case], sessions: tuple[tuple[str, int, int], ...] = SPEC_SESSIONS
) -> dict[str, list[dict]]:
    """Every §3 view, keyed by the name the specification gives it."""
    return {
        "total_statistics": [{"name": "Total", **total_statistics(cases)}],
        "direction": by_direction(cases),
        "models": by_model(cases),
        "sessions": by_session(cases, sessions),
        "weekdays": by_weekday(cases),
        "months": by_month(cases),
    }


# -------------------------------------------------------------------- report

def _rows(header: list[str], body: list[list[str]], align: str = "") -> list[str]:
    rule = [("---:" if a == "r" else "---") for a in (align or "l" * len(header))]
    return (
        [f"| {' | '.join(header)} |", f"|{'|'.join(rule)}|"]
        + [f"| {' | '.join(r)} |" for r in body]
    )


def markdown_report(
    result: BacktestResult,
    cases: list[Case],
    sessions: tuple[tuple[str, int, int], ...] = SPEC_SESSIONS,
) -> str:
    """§5's report. Every number here is read off `cases`, nothing recomputed."""
    t = total_statistics(cases)
    period = (
        f"{result.start:%Y-%m-%d} - {result.end:%Y-%m-%d}"
        if result.start and result.end else "-"
    )
    out = [
        "# Backtest Summary",
        "",
        f"**Instrument:** {result.symbol}  ",
        f"**Strategy:** {result.strategy}  ",
        f"**Tested period:** {period}",
        "",
        "### Total Statistics",
        "",
        *_rows(["Metric", "Value"], [
            ["Total Trades", f"{t['trades']}"],
            ["Wins", f"{t['wins']}"],
            ["Losses", f"{t['losses']}"],
            ["BE", f"{t['be']}"],
            ["Winrate", f"{t['winrate_pct']:.2f}%"],
            ["Average RR", f"{t['average_rr']:.2f} RR"],
            ["Gained RR", f"{t['gained_rr']:.2f} RR"],
        ], "lr"),
    ]

    for title, rows, extra in (
        ("Performance by Direction", by_direction(cases), []),
        ("Performance by Model", by_model(cases), ["Average RR"]),
        ("Performance by Session", by_session(cases, sessions), ["Time"]),
        ("Performance by Weekday", by_weekday(cases), []),
    ):
        head = [title.split(" by ")[-1]] + extra + ["Trades", "Winrate"]
        if "Average RR" in extra:
            head = [title.split(" by ")[-1], "Trades", "Winrate", "Average RR"]
        body = []
        for r in rows:
            if "Average RR" in extra:
                body.append([r["name"], str(r["trades"]), f"{r['winrate_pct']:.1f}%",
                             f"{r['average_rr']:.2f}"])
            elif "Time" in extra:
                body.append([r["name"], r.get("time", ""), str(r["trades"]),
                             f"{r['winrate_pct']:.1f}%"])
            else:
                body.append([r["name"], str(r["trades"]), f"{r['winrate_pct']:.1f}%"])
        out += ["", f"### {title}", "", *_rows(head, body, "l" + "r" * (len(head) - 1))]

    out += ["", "### Monthly Performance", "",
            *_rows(["Month", "Trades", "PnL (R)"],
                   [[r["name"], str(r["trades"]), f"{r['gained_rr']:+.2f}"]
                    for r in by_month(cases)], "lrr")]

    # §5's closing lists. Empty on a fresh run — these are review fields — but
    # the headings say where the notes go.
    review = ("BE Reason", "News Event", "Mistake", "To Improve", "Needs validation")
    filled = {name: [] for name in review}
    for case in cases:
        row = case.as_row()
        for name in review:
            if row[name]:
                filled[name].append(f"- Case {case.number}: {row[name]}")
    out += ["", "### Review notes", ""]
    for name in review:
        out += [f"**{name}**", "", *(filled[name] or ["- (none recorded)"]), ""]

    out += [
        "---",
        "",
        f"Break-even is any case returning within {cases[0].be_threshold:g}R of flat"
        if cases else "",
        "Winrate excludes break-even trades from its denominator, per §3.1.",
        "Session hours are broker-server time; this broker's clock follows New York's",
        "daylight saving, so sessions sit at stable broker-local hours.",
        "",
    ]
    return "\n".join(out)


# --------------------------------------------------------------------- saving

def write(
    directory,
    result: BacktestResult,
    be_threshold: float = DEFAULT_BE_THRESHOLD,
    sessions: tuple[tuple[str, int, int], ...] = SPEC_SESSIONS,
) -> list[Case]:
    """Write the database, its six views and the report into a run directory.

    Returns the cases, so a caller can report on them without rebuilding.
    """
    from pathlib import Path

    from ..data.results import write_rows

    directory = Path(directory)
    cases = build(result, be_threshold, sessions)
    if not cases:
        return cases

    write_rows(directory / "casebook.csv", [c.as_row() for c in cases])
    for name, rows in tables(cases, sessions).items():
        write_rows(directory / f"casebook_{name}.csv", rows)
    (directory / "report.md").write_text(
        markdown_report(result, cases, sessions), encoding="utf-8"
    )
    return cases
