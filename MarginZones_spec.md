# Margin Zones (MZ) Strategy — What's Built, and Where It's Shaky

Status snapshot of the `margin_zones` strategy: what's implemented, what was
added most recently, what's deliberately left out, and the assumptions/known
problems worth knowing before trusting the numbers. Not a spec itself — see
`MarginZones_revised.md` (base) and
`MarginZones_revised_with_CFD_rollover_points(1).md` (adds §3.5 and §5, the
source for the rollover work below) for the actual requirements.

---

## 1. Implemented

### 1.1 Core zone maths

`backtester/strategies/margin_zones/margins.py`, `envelopes.py`

- `ContractSpec` / `MarginObservation` / `MarginLog` — a dated CSV log of CME
  maintenance-margin readings (`configs/margins.csv`), read as-of the date a
  ZigZag pivot occurred, never today's figure applied backwards.
- `FMZ = MM / (PP * NP)`, `IMZ = FMZ * 1.1`, `MZ = IMZ - FMZ`.
- Zone percentage levels, including **50% MZ** (`Envelope.mid_price`) — the
  zone's own midpoint, `(FMZ + IMZ) / 2`.
- `build_envelopes()` projects a `[FMZ, IMZ]` band from every confirmed H4
  ZigZag pivot, out to the next one.

### 1.2 Chart & report

`backtester/strategies/margin_zones/report.py`, `scripts/plot_zones.py`,
`scripts/plot_zones_template.html`

- `scripts/plot_zones.py` — standalone CLI, `--save` writes a timestamped
  `runs/<stamp>_zones_<symbol>_<tf>_<contract>_dev<n>/` directory:
  `chart.html`, `pivots.csv`, `envelopes.csv`, `summary.json`.
- Interactive chart: price, ZigZag spine + pivots, `[FMZ, IMZ]` shaded bands,
  a dashed 50% MZ hairline, zoom/filter/scale controls, hover tooltip, table
  view.

### 1.3 Live signal

`signals/strategies/margin_zones.py`, `configs/signals.yaml`

- `eurusd-heartbeat` job: scheduled Telegram message with the latest
  confirmed pivot, the live zone, and distance from price to it. Writes the
  same report artifacts as the CLI via `write_artifacts()`, so the message
  and the saved chart are computed from the same objects, not recomputed
  twice.
- No trading condition wired up yet — this is a heartbeat proving the data
  path, not a signal.

### 1.4 Added this session: 50% Extremum-to-50% MZ Level (§3.5)

`Envelope.e50_price` in `envelopes.py`

- A level halfway between the ZigZag extremum itself and the 50% MZ
  midpoint — **not** a level inside the zone. Algebraically
  `(pivot.price + mid_price) / 2`, which is identical to the spec's
  `pivot ± (FMZ+IMZ)/4 * pip_size`.
- Verified against the spec's own worked example (MM=2900, PP=6.25, NP=2 →
  E50 = 121.8 pips from the extremum).
- Surfaced in `envelopes.csv` (`e50_price` column), the chart (a solid
  hairline outside the shaded band, nearer the extremum than FMZ — it was
  dotted at first, changed to solid because dotted was unreadable at the
  opacity needed to stay visually subordinate to FMZ/IMZ), and the Telegram
  message (`50% Ext-MZ` line).

### 1.5 Added this session: Daily CFD Rollover Price Points (§5)

`backtester/strategies/margin_zones/rollover.py`

- `RolloverPoint(day, roll_time, price, bar_time)` and `rollover_points()`:
  for every calendar day spanned by the bars (the *full* range, including
  days with zero bars — see §2.2 below for why that matters), find the last
  bar strictly before that day's `T_roll`, and emit a point at its close.
  Skips a day if no bar precedes its `T_roll`, or if the candidate bar is the
  same one already used for the previous point (handles weekends: Saturday's
  `T_roll` resolves to Friday's last bar → one point; Sunday's resolves to
  the *same* bar → skipped rather than duplicated).
- Sampled from a separate, finer timeframe (`rollover_timeframe`, default
  `M15`) than whatever the zones themselves are drawn on (`H4` for the
  current job) — loaded independently so zone resolution and rollover
  resolution are decoupled.
- Surfaced in `rollover.csv` (`day, roll_time, price, bar_time`), the chart
  (small dots along the price line, positioned by timestamp via an `xAt()`
  interpolation helper since the main x-axis is bar-*index*-based on the
  zone timeframe, not time-based), and the Telegram message (`Rollover`
  line: latest point + total count + the assumed break time).
- CLI: `--rollover-hour` / `--rollover-tz` / `--rollover-timeframe` /
  `--no-rollover` on `plot_zones.py`. Job config: same three keys under
  `params:` in `signals.yaml` (currently set on `eurusd-heartbeat`:
  `rollover_timeframe: M15`, `rollover_hour: 0`, `rollover_tz: UTC`).

---

## 2. Assumptions & known problems

### 2.1 `rollover_hour` / `rollover_tz` are guessed, not read from the broker

The spec (§5.1) says `T_roll` "must be determined from the trading-session
schedule of the specific CFD symbol and broker" and "must not be hardcoded."
In practice it *is* hardcoded — as a config value you set, not something read
from MT5. Checked directly against the installed `MetaTrader5` Python package
(v5.0.6090) in the Wine prefix:

- `symbol_info_session_quote()` / `symbol_info_session_trade()` — the MQL5
  calls that carry the actual weekly break schedule — **do not exist** in the
  Python wrapper (`AttributeError` on call).
- `symbol_info()`'s ~90 fields include `session_open` / `session_close`, but
  those are *today's session prices*, not a schedule of break times.

So there is currently no way to ask the terminal what time XAUUSD's or
EURUSD's break actually starts. `rollover_hour: 0, rollover_tz: UTC` (broker
midnight, since bar timestamps are broker-server time labelled UTC) is a
default, not a verified fact about either broker. If the real break is at a
different hour, every rollover point is silently reading the wrong bar.

### 2.2 M15 history is capped well short of the H4/D1 range

MT5's terminal-level "Max bars in chart" setting (Tools > Options > Charts,
default 100,000) truncates *M15* history far more than H4/D1 at the same
setting. Concretely, right now:

```
EURUSD  D1   1,197 bars  2022-01-03 .. 2026-08-10
EURUSD  H1  28,605 bars  2022-01-03 .. 2026-08-10
EURUSD  H4   7,162 bars  2022-01-03 .. 2026-08-10
EURUSD  M15 101,137 bars 2022-07-14 .. 2026-08-10   <- starts 6 months later
```

ZigZag pivots (built on H4) go back to the full H4 range. Rollover points
(built on M15) can only go back to 2022-07-14 — not a bug in
`rollover_points()`, just a ceiling on the input data. Raising "Max bars in
chart" in the terminal and re-fetching would extend it; not done yet.

### 2.3 Margin readings only exist for 6E

`configs/contracts/` has specs for `6E`, `6B`, and `GC` — but
`configs/margins.csv` has maintenance-margin readings for **`6E` only** (20
dated rows). `build_envelopes()` silently produces zero envelopes for a
pivot with no covering reading, so **zones cannot currently be drawn for any
`GC`-backed instrument (XAUUSD) or `6B`-backed one (GBPUSD)** — only EURUSD.
This isn't a code gap, it's missing input data (CME margin history entered by
hand, since CME blocks scripted access), but it means the strategy is
effectively single-instrument right now regardless of what data is fetched.

### 2.4 Rollover price falls back to bar Close, never a tick

Per §5.2's explicit fallback ("if tick data is unavailable... use the Close
price of the final available bar"), `rollover_points()` always uses a bar
close — this codebase doesn't store tick data at all (`data/bars/` is
OHLC-only; see `backtester/data/README.md`). So every rollover point is, at
best, as precise as the sampling timeframe (M15 → up to ~15 minutes of
staleness relative to the true pre-break tick).

### 2.5 "Trading day" is data-driven, not a real broker calendar

A day gets no point either because no bar precedes its `T_roll` at all, or
because the candidate bar is identical to the previous point's (the weekend
case). There's no actual holiday/session calendar consulted — an unusual
broker closure that still has a stray bar or two around it could produce a
point where a real trading calendar would say there shouldn't be one, or
vice versa. Works correctly for the ordinary weekly weekend gap; unverified
against actual holiday behavior.

### 2.6 Rollover points aren't in the hover tooltip yet

The dots are drawn on the chart, but hovering near one shows whichever H4 bar
the crosshair snaps to (bar-index based), not the rollover point's own
day/price. A weekend rollover point in particular sits in the *compressed*
gap between Friday's last bar and Monday's first — visually adjacent to both,
but not independently hoverable. Flagged, not fixed.

### 2.7 §5.5 Rollover Crossing Events — not built

The `..._crossings.md` variant adds True/False crossing classification
against the E50 level; the `..._points(1).md` variant (implemented here)
doesn't. Deliberately deferred — only the rollover points themselves are
built this round.

### 2.8 Two near-duplicate spec files, code still cites the older one

`MarginZones_revised_with_CFD_rollover_crossings.md` and
`MarginZones_revised_with_CFD_rollover_points(1).md` are both untracked, in
the repo root, and identical except for §5.5. Code docstrings (e.g.
`margins.py`'s `MarginZones` class) still point at `MarginZones_revised.md`,
the pre-existing tracked spec that predates both. Nothing has been renamed or
reconciled — if `MarginZones_revised.md` should be superseded by one of the
two newer files, or the CFD-rollover content folded into it, that's still
undone.
