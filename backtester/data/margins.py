"""CME margin zones — contract specs, margin readings, and the zone maths.

The idea (see `Margin Zones.md`): exchange maintenance margin is the exchange's
own estimate of a one-day adverse move it needs collateral against. Divide it
by the value of a pip and you get that estimate expressed as a **distance in
pips** — how far price has to move against one contract to burn the margin.
That distance is then projected from a swing pivot as a level.

    FMZ = MM / (PP * NP)          forward margin zone, in pips
    IMZ = FMZ * initial_ratio     initial margin zone (1.1 by default)
    MR  = IMZ - FMZ               the band between them

`PP * NP` is just the **pip value**: PP is the exchange's per-tick dollar value
and NP converts ticks to pips. The note fixes NP at 2, which is right for
EUR/USD futures (a 0.00005 tick is half a 0.0001 pip) but *not* in general —
NP is `pip_size / tick_size`, which is 1 for GBP/USD and 1 for gold. Since the
note's own historical-margin link points at the metals table, that distinction
matters: using 2 on gold would halve every zone.

Margin numbers are **not fetched**. CME serves 403 to scripted requests with
"use of scripts, software, spiders, robots ... is strictly prohibited", so
readings are entered by hand from the site and kept in a dated log here —
which also gives you the weekly consistency check the note asks for.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

DEFAULT_INITIAL_RATIO = 1.1
CONTRACT_DIR = Path(__file__).resolve().parents[2] / "configs" / "contracts"
MARGIN_LOG = Path(__file__).resolve().parents[2] / "configs" / "margins.csv"


@dataclass(slots=True)
class ContractSpec:
    """A futures contract's stable properties.

    Stable is the operative word: contract size and tick never move, so these
    live in version control. Margin does move, and lives in the log instead.
    """

    code: str                  # exchange product code, e.g. "6E"
    name: str
    exchange: str = "CME"
    contract_size: float = 0.0  # units of `base_currency` per contract
    base_currency: str = ""
    quote_currency: str = "USD"
    tick_size: float = 0.0      # minimum price fluctuation
    tick_value: float = 0.0     # quote-currency value of one tick, per contract
    pip_size: float = 0.0       # what this desk calls a pip

    @property
    def np(self) -> float:
        """The note's NP: how many ticks make a pip."""
        if self.tick_size <= 0:
            return 0.0
        return self.pip_size / self.tick_size

    @property
    def pip_value(self) -> float:
        """`PP * NP` — quote-currency value of a one-pip move, per contract."""
        return self.tick_value * self.np

    def problems(self) -> list[str]:
        """Internal consistency. Catches a mistyped tick or contract size.

        The load-bearing check is the last one: for a contract quoted in the
        account currency, `contract_size * pip_size` must equal the pip value
        derived from the exchange's tick figures. Two independent routes to the
        same number, so a typo in either shows up immediately. (The same
        cross-check caught a broker reporting a 10x-wrong tick value in
        scripts/fetch_mt5.py.)
        """
        out = []
        if not self.code:
            out.append("ERROR: no contract code")
        for name in ("contract_size", "tick_size", "tick_value", "pip_size"):
            if getattr(self, name) <= 0:
                out.append(f"ERROR: {name} must be > 0")
        if out:
            return out

        if abs(self.np - round(self.np)) > 1e-9:
            out.append(
                f"WARN: pip_size/tick_size = {self.np:g} is not a whole number of ticks"
            )
        derived = self.contract_size * self.pip_size
        if self.quote_currency == "USD" and derived > 0:
            drift = abs(derived - self.pip_value) / derived
            if drift > 0.01:
                out.append(
                    f"ERROR: pip value disagrees — contract_size*pip_size = {derived:,.4f} "
                    f"but tick_value*NP = {self.pip_value:,.4f}. One of "
                    f"contract_size / tick_size / tick_value / pip_size is wrong."
                )
        return out

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ContractSpec":
        known = set(cls.__slots__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass(slots=True)
class MarginObservation:
    """One dated reading of the exchange margin for a contract.

    `as_of` is the date the figure was *effective on the exchange*, not the day
    it was typed in. Margins change at contract roll and on volatility, so the
    log is a time series and the strategy must read the value that was current
    on the bar it is evaluating — never today's number applied to 2022.
    """

    code: str
    as_of: date
    maintenance: float           # MM, quote currency per contract
    initial: float | None = None  # if the exchange publishes it; else derived
    source: str = ""             # where a human read it
    note: str = ""

    def initial_or_derived(self, ratio: float = DEFAULT_INITIAL_RATIO) -> float:
        return self.initial if self.initial else self.maintenance * ratio

    def to_row(self) -> dict:
        return {
            "code": self.code,
            "as_of": self.as_of.isoformat(),
            "maintenance": self.maintenance,
            "initial": "" if self.initial is None else self.initial,
            "source": self.source,
            "note": self.note,
        }

    @classmethod
    def from_row(cls, row: dict) -> "MarginObservation":
        initial = (row.get("initial") or "").strip()
        return cls(
            code=row["code"].strip().upper(),
            as_of=date.fromisoformat(row["as_of"].strip()),
            maintenance=float(row["maintenance"]),
            initial=float(initial) if initial else None,
            source=(row.get("source") or "").strip(),
            note=(row.get("note") or "").strip(),
        )


@dataclass(slots=True)
class MarginZones:
    """The computed zones for one contract at one point in time."""

    code: str
    as_of: date
    maintenance: float
    initial: float
    pip_value: float
    fmz: float   # pips
    imz: float   # pips
    mr: float    # pips

    def price_distance(self, spec: ContractSpec, pips: float) -> float:
        """Pips -> a price delta, for drawing a level on a chart."""
        return pips * spec.pip_size

    def levels(
        self,
        pivot: float,
        spec: ContractSpec,
        direction: int = 1,
        fractions: tuple[float, ...] = (0.5, 1.0),
        basis: str = "fmz",
    ) -> dict[str, float]:
        """Project zone levels from a swing pivot.

        `direction` is +1 to project upward from a low, -1 downward from a high.
        `basis` picks what the fractions are fractions *of* — the note is
        ambiguous here (see the module docstring in scripts/margins.py), so it
        is a parameter rather than a guess.
        """
        base = {"fmz": self.fmz, "imz": self.imz, "mr": self.mr}.get(basis)
        if base is None:
            raise ValueError(f"basis must be one of fmz/imz/mr, got {basis!r}")
        sign = 1 if direction >= 0 else -1
        return {
            f"{int(f * 100)}%": pivot + sign * self.price_distance(spec, base * f)
            for f in fractions
        }

    def to_dict(self) -> dict:
        data = asdict(self)
        data["as_of"] = self.as_of.isoformat()
        return data


def compute_zones(
    spec: ContractSpec,
    observation: MarginObservation,
    initial_ratio: float = DEFAULT_INITIAL_RATIO,
) -> MarginZones:
    """FMZ / IMZ / MR for a contract, from a dated margin reading."""
    pip_value = spec.pip_value
    if pip_value <= 0:
        raise ValueError(f"{spec.code}: pip value is zero — check the contract spec")
    if observation.maintenance <= 0:
        raise ValueError(f"{spec.code}: maintenance margin must be > 0")

    fmz = observation.maintenance / pip_value
    initial = observation.initial_or_derived(initial_ratio)
    imz = initial / pip_value
    return MarginZones(
        code=spec.code,
        as_of=observation.as_of,
        maintenance=observation.maintenance,
        initial=initial,
        pip_value=pip_value,
        fmz=fmz,
        imz=imz,
        mr=imz - fmz,
    )


def validate_observation(
    observation: MarginObservation,
    spec: ContractSpec | None = None,
    today: date | None = None,
    max_age_days: int = 7,
    previous: MarginObservation | None = None,
) -> list[str]:
    """Quality checks on a margin reading.

    ERROR means the number cannot be used. WARN means look at it — most
    usefully when a reading is stale (the note asks for a weekly check) or has
    jumped, which is the signature of a contract roll or a volatility event.
    """
    out: list[str] = []
    today = today or datetime.now(timezone.utc).date()

    if observation.maintenance <= 0:
        out.append("ERROR: maintenance margin must be > 0")
    if observation.initial is not None:
        if observation.initial <= 0:
            out.append("ERROR: initial margin must be > 0 when given")
        elif observation.initial < observation.maintenance:
            out.append(
                f"ERROR: initial ({observation.initial:,.0f}) is below maintenance "
                f"({observation.maintenance:,.0f}); the exchange never sets it that way"
            )
    if observation.as_of > today:
        out.append(f"ERROR: as_of {observation.as_of} is in the future")

    age = (today - observation.as_of).days
    if age > max_age_days:
        out.append(
            f"WARN: reading is {age} days old (> {max_age_days}); "
            f"re-check the margin on the exchange"
        )
    if not observation.source:
        out.append("WARN: no source recorded — you will not be able to re-check it")

    if previous is not None and previous.maintenance > 0:
        change = (observation.maintenance - previous.maintenance) / previous.maintenance
        if abs(change) > 0.25:
            out.append(
                f"WARN: maintenance moved {change * 100:+.0f}% from the "
                f"{previous.as_of} reading — contract roll or a volatility event?"
            )

    if spec is not None:
        out.extend(spec.problems())
        if spec.pip_value > 0 and observation.maintenance > 0:
            fmz = observation.maintenance / spec.pip_value
            # A one-day margin move outside this range means the pip value is
            # almost certainly wrong by a factor of ten.
            if not 10 <= fmz <= 5000:
                out.append(
                    f"ERROR: FMZ works out at {fmz:,.0f} pips, which is not a plausible "
                    f"one-day move — check pip_size/tick_value for {spec.code}"
                )
    return out


# ----------------------------------------------------------------- storage

def load_spec(code: str, directory: Path | str | None = None) -> ContractSpec:
    path = Path(directory or CONTRACT_DIR) / f"{code.upper()}.json"
    if not path.exists():
        available = sorted(p.stem for p in Path(directory or CONTRACT_DIR).glob("*.json"))
        raise FileNotFoundError(
            f"No contract spec for {code!r} at {path}. Available: {', '.join(available) or 'none'}"
        )
    with open(path, "r", encoding="utf-8") as fh:
        return ContractSpec.from_dict(json.load(fh))


def save_spec(spec: ContractSpec, directory: Path | str | None = None) -> Path:
    directory = Path(directory or CONTRACT_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{spec.code.upper()}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(spec.to_dict(), fh, indent=2)
    return path


def list_specs(directory: Path | str | None = None) -> list[str]:
    directory = Path(directory or CONTRACT_DIR)
    return sorted(p.stem for p in directory.glob("*.json")) if directory.exists() else []


FIELDS = ("code", "as_of", "maintenance", "initial", "source", "note")


@dataclass
class MarginLog:
    """The dated log of margin readings, one CSV for every contract."""

    path: Path = field(default_factory=lambda: MARGIN_LOG)

    def read(self) -> list[MarginObservation]:
        if not Path(self.path).exists():
            return []
        with open(self.path, "r", newline="", encoding="utf-8") as fh:
            rows = [MarginObservation.from_row(r) for r in csv.DictReader(fh) if r.get("code")]
        rows.sort(key=lambda o: (o.code, o.as_of))
        return rows

    def for_code(self, code: str) -> list[MarginObservation]:
        return [o for o in self.read() if o.code == code.upper()]

    def latest(self, code: str, on: date | None = None) -> MarginObservation | None:
        """The reading in force on `on` — never a later one.

        Backtests must not use a margin figure published after the bar being
        evaluated; that is lookahead, the same class of bug the engine is built
        to prevent.
        """
        rows = [o for o in self.for_code(code) if on is None or o.as_of <= on]
        return rows[-1] if rows else None

    def add(self, observation: MarginObservation) -> None:
        """Append, replacing any existing reading for the same code and date."""
        rows = [
            o for o in self.read()
            if not (o.code == observation.code and o.as_of == observation.as_of)
        ]
        rows.append(observation)
        rows.sort(key=lambda o: (o.code, o.as_of))
        self.write(rows)

    def write(self, rows: list[MarginObservation]) -> None:
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow(row.to_row())
