"""Contract specifications.

P&L in a backtest is only as good as the contract spec behind it. Getting
`contract_size` wrong for XAUUSD (100 oz, not 1) silently scales every result by
100x, so specs are explicit and can be dumped straight out of MT5 rather than
guessed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass(slots=True)
class Instrument:
    """Everything the broker simulation needs to price a fill.

    `tick_value` is the account-currency value of one `tick_size` move for one
    lot. For symbols quoted in the account currency (XAUUSD on a USD account)
    it is simply `contract_size * tick_size`, and that is what is assumed when
    it is left at 0.
    """

    symbol: str
    digits: int = 2
    contract_size: float = 100.0     # units per 1.0 lot
    tick_size: float = 0.01          # minimum price increment
    tick_value: float = 0.0          # 0 -> derived from contract_size * tick_size
    min_volume: float = 0.01
    max_volume: float = 100.0
    volume_step: float = 0.01
    spread_points: float = 20.0      # typical spread, in `tick_size` units
    commission_per_lot: float = 0.0  # account currency, charged per side
    swap_long: float = 0.0           # points per lot per night (negative = cost)
    swap_short: float = 0.0
    currency: str = "USD"

    def __post_init__(self) -> None:
        if self.tick_value <= 0:
            self.tick_value = self.contract_size * self.tick_size

    @property
    def spread(self) -> float:
        """Default spread in price units."""
        return self.spread_points * self.tick_size

    def round_price(self, price: float) -> float:
        return round(price, self.digits)

    def round_volume(self, volume: float) -> float:
        """Snap to the broker's volume step and clamp to the allowed range."""
        if self.volume_step <= 0:
            return volume
        steps = round(volume / self.volume_step)
        snapped = steps * self.volume_step
        snapped = max(self.min_volume, min(self.max_volume, snapped))
        # volume_step is often 0.01; float noise there turns 0.1 into 0.09999.
        return round(snapped, 8)

    def value_of(self, price_delta: float, volume: float) -> float:
        """Account-currency P&L of a price move for a given lot size."""
        return price_delta / self.tick_size * self.tick_value * volume

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Instrument":
        fields = {f for f in cls.__slots__}
        return cls(**{k: v for k, v in data.items() if k in fields})


# Sensible defaults for common symbols. These are *typical retail* values —
# override them with a spec dumped from your own broker before trusting the
# absolute P&L of a run (scripts/fetch_mt5.py --dump-spec writes one).
BUILTIN_SPECS: dict[str, Instrument] = {
    "XAUUSD": Instrument(
        symbol="XAUUSD",
        digits=2,
        contract_size=100.0,     # 100 troy ounces per lot
        tick_size=0.01,
        min_volume=0.01,
        volume_step=0.01,
        spread_points=25.0,      # ~$0.25
        commission_per_lot=0.0,
        swap_long=-10.0,         # nominal: ~-$10 per lot per night
        swap_short=2.0,
    ),
    "EURUSD": Instrument(
        symbol="EURUSD",
        digits=5,
        contract_size=100_000.0,
        tick_size=0.00001,
        spread_points=10.0,
        min_volume=0.01,
        volume_step=0.01,
    ),
    "BTCUSD": Instrument(
        symbol="BTCUSD",
        digits=2,
        contract_size=1.0,
        tick_size=0.01,
        spread_points=2000.0,
        min_volume=0.01,
        volume_step=0.01,
    ),
}

DEFAULT_SPEC_DIR = Path(__file__).resolve().parents[2] / "configs" / "instruments"


def load_instrument(symbol: str, spec_dir: Path | str | None = None) -> Instrument:
    """Resolve a spec: JSON file in `spec_dir` first, then the built-in table.

    Falling back to a generic 1:1 contract for an unknown symbol is deliberate —
    it keeps exotic symbols runnable — but the numbers will be nominal, so the
    caller is expected to supply a spec file for anything it cares about.
    """
    directory = Path(spec_dir) if spec_dir else DEFAULT_SPEC_DIR
    path = directory / f"{symbol.upper()}.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            return Instrument.from_dict(json.load(fh))

    key = symbol.upper()
    if key in BUILTIN_SPECS:
        spec = BUILTIN_SPECS[key]
        return Instrument.from_dict(spec.to_dict())  # copy, callers may mutate

    return Instrument(symbol=symbol, digits=5, contract_size=1.0, tick_size=0.00001)


def save_instrument(inst: Instrument, spec_dir: Path | str | None = None) -> Path:
    directory = Path(spec_dir) if spec_dir else DEFAULT_SPEC_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{inst.symbol.upper()}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(inst.to_dict(), fh, indent=2)
    return path
