#!/usr/bin/env python3
"""Record CME margin readings and compute margin zones.

    scripts/margins.py contracts                      # specs on file, with checks
    scripts/margins.py import -f ~/Downloads/cme.xlsx --code 6E   # the history, in one go
    scripts/margins.py fetch --code 6E                # try CME (see below)
    scripts/margins.py add --code 6E --maintenance 2900 --as-of 2026-07-27 \
        --source "cmegroup.com > FX > G10 > Euro FX > Margins"
    scripts/margins.py show --code 6E                 # FMZ / IMZ / MR
    scripts/margins.py show --code 6E --pivot 1.0850 --direction up
    scripts/margins.py check                          # validate the whole log

**Margins cannot be fetched automatically.** CME answers scripted requests with
HTTP 403 and the message "use of scripts, software, spiders, robots, avatars,
agents, tools or other scraping mechanisms is strictly prohibited" — that block
covers the whole host, `robots.txt` included. `fetch` makes one plain,
identifying request so you can see the current status for yourself, and never
tries to work around the block. When it fails it prints exactly which two
numbers to read off the page and the `add` command to record them.

That is not much of a loss in practice: the note itself says to check the
maintenance margin about once a week, and margins only really move at contract
roll or on a volatility event. One reading a week, typed in, is the workflow.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtester.data.margins import (  # noqa: E402
    DEFAULT_INITIAL_RATIO,
    MarginLog,
    MarginObservation,
    compute_zones,
    list_specs,
    load_spec,
    validate_observation,
)

MARGIN_PAGE = "https://www.cmegroup.com/markets/fx/g10/euro-fx.margins.html"
HISTORICAL = (
    "https://www.cmegroup.com/solutions/risk-management/margin-services/"
    "historical-margins.html"
)


def cmd_contracts(args) -> int:
    codes = list_specs(args.contracts)
    if not codes:
        print("No contract specs found.")
        return 1
    print(f"{'code':6} {'name':26} {'contract':>12} {'tick':>10} {'NP':>4} {'pip value':>11}")
    print("-" * 74)
    bad = 0
    for code in codes:
        spec = load_spec(code, args.contracts)
        print(
            f"{spec.code:6} {spec.name[:26]:26} {spec.contract_size:>12,.0f} "
            f"{spec.tick_size:>10g} {spec.np:>4g} {spec.pip_value:>11,.2f}"
        )
        for problem in spec.problems():
            print(f"       ! {problem}")
            bad += problem.startswith("ERROR")
    return 1 if bad else 0


def cmd_fetch(args) -> int:
    """Make one honest request, report what happened, then explain the manual path."""
    import urllib.error
    import urllib.request

    url = args.url or MARGIN_PAGE
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "trading-backtester/0.1 (personal research; contact via repo)"},
    )
    print(f"GET {url}")
    status, body = None, ""
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            body = response.read(400).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read(400).decode("utf-8", "replace")
    except Exception as exc:  # network down, DNS, TLS
        print(f"  request failed: {exc}")

    if status:
        print(f"  HTTP {status}")
        if body.strip():
            print(f"  {body.strip()[:220]}")

    if status == 200:
        print(
            "\nThe page came back, but the margin figure is rendered by client-side\n"
            "JavaScript, so it is not in this HTML. Read it in a browser and record it\n"
            "with `add` — the numbers are the point, not the transport."
        )
    else:
        print(
            "\nCME is refusing scripted access, which is their stated policy. Nothing\n"
            "here will try to get around that."
        )

    print(
        f"\nDo this instead — two numbers, once a week:\n"
        f"  1. Maintenance margin (MM)\n"
        f"     {MARGIN_PAGE}\n"
        f"     (or the history: {HISTORICAL})\n"
        f"  2. Tick value (PP) — only if you are adding a new contract; it never changes\n"
        f"     the same product page > Contract Specs > minimum price fluctuation\n"
        f"\nThen record it:\n"
        f"  scripts/margins.py add --code {args.code or '6E'} --maintenance <MM> \\\n"
        f"      --as-of {date.today().isoformat()} --source \"cmegroup.com margins page\"\n"
    )
    return 0 if status == 200 else 2


def cmd_import(args) -> int:
    """Load a margin history exported from CME's historical-margins tool.

    The export is what makes the block a non-issue: a browser downloads the
    whole series in one click, and this reads it. Column names vary between
    CME's tabs and vintages, so they are matched by alias rather than position.
    """
    path = Path(args.file)
    if not path.exists():
        raise SystemExit(f"No such file: {path}")

    if path.suffix.lower() == ".pdf":
        rows = _read_cme_pdf(path, args.roll)
    else:
        rows = _read_table(path)
    if not rows:
        raise SystemExit(f"{path}: no rows found")

    columns = {k.strip().lower(): k for k in rows[0]}

    def pick(*names):
        for want in names:
            for key, original in columns.items():
                if key == want or key.replace("_", " ") == want:
                    return original
        for want in names:
            for key, original in columns.items():
                if want in key:
                    return original
        return None

    col_date = pick("start date", "effective date", "as of", "date")
    col_maint = pick("maintenance rate", "maintenance margin", "maintenance", "maint")
    col_init = pick("initial rate", "initial margin", "initial")
    col_prod = pick("product code", "clearing code", "product", "commodity", "code")

    if not col_date or not col_maint:
        raise SystemExit(
            f"{path}: could not find a date and a maintenance column.\n"
            f"  columns seen: {', '.join(rows[0])}\n"
            f"  pass --date-col / --maintenance-col to name them explicitly"
        )
    col_date = args.date_col or col_date
    col_maint = args.maintenance_col or col_maint

    log = MarginLog(Path(args.log))
    spec = load_spec(args.code, args.contracts)
    added, skipped = 0, 0

    parsed = []
    for row in rows:
        if col_prod and args.match:
            if args.match.lower() not in str(row.get(col_prod, "")).lower():
                continue
        as_of = _parse_date(row.get(col_date))
        maintenance = _parse_number(row.get(col_maint))
        if as_of is None or maintenance is None or maintenance <= 0:
            skipped += 1
            continue
        parsed.append((as_of, maintenance, _parse_number(row.get(col_init)) if col_init else None))

    parsed.sort(key=lambda r: r[0])
    if not args.all and parsed:
        # These reports are one row per business day, but margin is a step
        # function that moves a couple of times a year. Keeping only the days
        # it actually changed carries identical information for a
        # point-in-time lookup, and leaves a log a human can still read.
        kept = [parsed[0]]
        for row in parsed[1:]:
            if row[1] != kept[-1][1] or row[2] != kept[-1][2]:
                kept.append(row)
        print(f"  {len(parsed):,} daily observation(s) -> {len(kept)} change-point(s)")
        parsed = kept

    for as_of, maintenance, initial in parsed:
        observation = MarginObservation(
            code=spec.code, as_of=as_of, maintenance=maintenance,
            initial=initial if initial and initial > 0 else None,
            source=f"CME export {path.name}", note=args.note,
        )
        problems = [p for p in validate_observation(observation, spec, today=as_of)
                    if p.startswith("ERROR")]
        if problems:
            print(f"  skip {as_of}: {problems[0]}")
            skipped += 1
            continue
        if not args.dry_run:
            log.add(observation)
        added += 1

    verb = "would import" if args.dry_run else "imported"
    print(f"{verb} {added} reading(s) for {spec.code}, skipped {skipped}, from {path}")
    if added and not args.dry_run:
        print(f"  now: scripts/margins.py check --log {args.log}")
    return 0 if added else 1


def _read_cme_pdf(path: Path, roll: str | None) -> list[dict]:
    """Parse CME's "Minimum Performance Bond Requirements" PDF.

    This is the report behind the historical-margins page — the one a browser
    downloads. Records are a flat five-line run:

        2020-06-30 / EURO FUTURE / CME / 2275 / EC-01

    repeated once per roll month per business date, so a six-year EC report is
    ~36,000 records over ~950 pages. The "Margin" column is the *minimum*
    performance bond, i.e. maintenance — which is why IMZ = FMZ * 1.1 lines up
    with the customer initial requirement.

    Column names are normalised on the way out so the caller's alias matching
    needs no special case for PDFs.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise SystemExit(
            "Reading CME's margin PDF needs pypdf:  pip install pypdf"
        ) from exc

    reader = PdfReader(str(path))
    print(f"  reading {len(reader.pages):,} pages...", flush=True)
    lines: list[str] = []
    for page in reader.pages:
        lines += [ln.strip() for ln in (page.extract_text() or "").splitlines() if ln.strip()]

    is_date = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    is_roll = re.compile(r"^[A-Z0-9]{1,4}-\d+$")
    rows: list[dict] = []
    i = 0
    while i < len(lines) - 4:
        # A record is date, description, exchange, margin, roll code. Anything
        # else (page headers, column titles) fails the shape test and is walked
        # past one line at a time.
        if is_date.match(lines[i]) and is_roll.match(lines[i + 4]):
            try:
                maintenance = float(lines[i + 3].replace(",", ""))
            except ValueError:
                i += 1
                continue
            rows.append({
                "as_of": lines[i],
                "description": lines[i + 1],
                "exchange": lines[i + 2],
                "maintenance": maintenance,
                "product": lines[i + 4],
            })
            i += 5
        else:
            i += 1

    if not rows:
        raise SystemExit(f"{path}: no margin records recognised — is this the CME report?")

    # Every roll month carries the same margin on a given date, so one is
    # enough; keeping all 36 would just repeat each reading 36 times.
    codes = sorted({r["product"] for r in rows})
    if roll is None:
        by_date: dict[str, set] = {}
        for r in rows:
            by_date.setdefault(r["as_of"], set()).add(r["maintenance"])
        disagree = sum(1 for v in by_date.values() if len(v) > 1)
        if disagree:
            print(
                f"  ! {disagree} date(s) have different margins per roll month; "
                f"using {codes[0]} — pass --roll to choose another"
            )
        roll = codes[0]
    rows = [r for r in rows if r["product"] == roll]
    print(f"  {len(rows):,} record(s) for {roll} of {len(codes)} roll month(s)")
    return rows


def _read_table(path: Path) -> list[dict]:
    """CSV, or XLSX when openpyxl is around."""
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise SystemExit(
                "Reading .xlsx needs openpyxl (pip install openpyxl), or re-save "
                "the file as CSV from the spreadsheet."
            ) from exc
        sheet = load_workbook(path, read_only=True, data_only=True).active
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            return []
        # CME exports often carry a title block before the real header.
        head = 0
        for n, row in enumerate(rows[:20]):
            filled = [str(c).strip().lower() for c in row if c not in (None, "")]
            if any("maint" in c for c in filled):
                head = n
                break
        header = [str(c or f"col{j}").strip() for j, c in enumerate(rows[head])]
        return [dict(zip(header, r)) for r in rows[head + 1:] if any(c is not None for c in r)]

    import csv as _csv

    with open(path, "r", newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = _csv.Sniffer().sniff(sample, delimiters=",;\t")
        except _csv.Error:
            dialect = _csv.excel
        return [r for r in _csv.DictReader(fh, dialect=dialect) if any(v for v in r.values())]


def _parse_date(value):
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()[:32]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d",
                "%m/%d/%y", "%d-%b-%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _parse_number(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("$", "").replace(" ", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def cmd_add(args) -> int:
    spec = load_spec(args.code, args.contracts)
    log = MarginLog(Path(args.log))
    as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.now(timezone.utc).date()

    observation = MarginObservation(
        code=spec.code,
        as_of=as_of,
        maintenance=args.maintenance,
        initial=args.initial,
        source=args.source,
        note=args.note,
    )
    previous = log.latest(spec.code, on=as_of)
    problems = validate_observation(observation, spec, previous=previous)
    for problem in problems:
        print(f"  ! {problem}")
    if any(p.startswith("ERROR") for p in problems):
        print("\nNot recorded — fix the errors above.")
        return 1

    log.add(observation)
    zones = compute_zones(spec, observation, args.initial_ratio)
    print(
        f"Recorded {spec.code} MM={observation.maintenance:,.0f} as of {as_of} in {args.log}\n"
        f"  FMZ {zones.fmz:,.1f} pips · IMZ {zones.imz:,.1f} pips · MR {zones.mr:,.1f} pips"
    )
    return 0


def cmd_show(args) -> int:
    spec = load_spec(args.code, args.contracts)
    log = MarginLog(Path(args.log))
    on = date.fromisoformat(args.on) if args.on else None
    observation = log.latest(spec.code, on=on)
    if observation is None:
        print(
            f"No margin reading for {spec.code}"
            f"{' on or before ' + args.on if args.on else ''}.\n"
            f"Add one:  scripts/margins.py add --code {spec.code} --maintenance <MM>\n"
            f"Where to find it:  scripts/margins.py fetch --code {spec.code}"
        )
        return 1

    zones = compute_zones(spec, observation, args.initial_ratio)
    print(f"{spec.code} — {spec.name}")
    print(f"  reading      MM {observation.maintenance:,.0f} / IM {zones.initial:,.0f} "
          f"(as of {observation.as_of})")
    print(f"  pip value    {spec.pip_value:,.2f} {spec.quote_currency}"
          f"   (PP {spec.tick_value:g} x NP {spec.np:g})")
    print(f"  FMZ          {zones.fmz:,.1f} pips   = {zones.fmz * spec.pip_size:,.5g} in price")
    print(f"  IMZ          {zones.imz:,.1f} pips   = {zones.imz * spec.pip_size:,.5g} in price")
    print(f"  MR           {zones.mr:,.1f} pips")

    for problem in validate_observation(observation, spec):
        print(f"  ! {problem}")

    if args.pivot:
        direction = -1 if args.direction == "down" else 1
        fractions = tuple(float(f) for f in args.fractions.split(","))
        print(f"\n  levels from pivot {args.pivot:g}, {args.direction}, as fractions of {args.basis.upper()}:")
        for label, level in zones.levels(
            args.pivot, spec, direction=direction, fractions=fractions, basis=args.basis
        ).items():
            print(f"    {label:>6} {basis_label(args.basis)}   {level:,.5f}")
    return 0


def basis_label(basis: str) -> str:
    return {"fmz": "FMZ", "imz": "IMZ", "mr": "MR"}[basis]


def cmd_check(args) -> int:
    log = MarginLog(Path(args.log))
    rows = log.read()
    if not rows:
        print(f"{args.log} has no readings yet.")
        return 1

    failures = 0
    by_code: dict[str, list[MarginObservation]] = {}
    for row in rows:
        by_code.setdefault(row.code, []).append(row)

    for code, observations in sorted(by_code.items()):
        try:
            spec = load_spec(code, args.contracts)
        except FileNotFoundError as exc:
            print(f"{code}: ERROR {exc}")
            failures += 1
            continue
        print(f"{code}: {len(observations)} reading(s), "
              f"{observations[0].as_of} .. {observations[-1].as_of}")
        previous = None
        for n, observation in enumerate(observations):
            # Staleness is a question about the *current* reading. In a history
            # every earlier row is old by definition, so only the last one is
            # held to the weekly check — otherwise an imported six-year series
            # buries its real problems under twenty "this is old" warnings.
            is_latest = n == len(observations) - 1
            problems = validate_observation(
                observation, spec, previous=previous,
                max_age_days=7 if is_latest else 10**6,
            )
            for problem in problems:
                print(f"  {observation.as_of} ! {problem}")
                failures += problem.startswith("ERROR")
            previous = observation
    print("\nno errors" if not failures else f"\n{failures} error(s)")
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CME margin readings and margin-zone maths.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--log", default="configs/margins.csv", help="margin reading log")
    common.add_argument("--contracts", default=None, help="contract spec directory")
    common.add_argument(
        "--initial-ratio", type=float, default=DEFAULT_INITIAL_RATIO,
        help="initial/maintenance ratio when the exchange figure is unknown (default 1.1)",
    )
    subs = parser.add_subparsers(dest="command", required=True)

    subs.add_parser("contracts", parents=[common], help="list contract specs and check them"
                    ).set_defaults(func=cmd_contracts)

    fetch = subs.add_parser("fetch", parents=[common], help="try CME, then explain the manual path")
    fetch.add_argument("--code", help="contract code, for the printed instructions")
    fetch.add_argument("--url", help="override the URL to probe")
    fetch.set_defaults(func=cmd_fetch)

    imp = subs.add_parser(
        "import", parents=[common],
        help="load a margin history exported from CME's historical-margins tool",
    )
    imp.add_argument("--file", "-f", required=True, help="the downloaded .csv or .xlsx")
    imp.add_argument("--code", required=True, help="contract code to file it under")
    imp.add_argument("--match", help="only rows whose product column contains this")
    imp.add_argument("--date-col", dest="date_col", help="override the date column name")
    imp.add_argument("--maintenance-col", dest="maintenance_col", help="override it")
    imp.add_argument("--note", default="")
    imp.add_argument("--roll", help="roll month to keep from a CME PDF (default: the first)")
    imp.add_argument("--all", action="store_true",
                     help="keep every daily row, not just the days margin changed")
    imp.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    imp.set_defaults(func=cmd_import)

    add = subs.add_parser("add", parents=[common], help="record a margin reading")
    add.add_argument("--code", required=True)
    add.add_argument("--maintenance", type=float, required=True, help="MM per contract")
    add.add_argument("--initial", type=float, help="IM per contract, if published")
    add.add_argument("--as-of", dest="as_of", help="date the figure is effective (default today)")
    add.add_argument("--source", default="", help="where you read it")
    add.add_argument("--note", default="")
    add.set_defaults(func=cmd_add)

    show = subs.add_parser("show", parents=[common], help="compute zones")
    show.add_argument("--code", required=True)
    show.add_argument("--on", help="use the reading in force on this date")
    show.add_argument("--pivot", type=float, help="project levels from this price")
    show.add_argument("--direction", choices=["up", "down"], default="up")
    show.add_argument("--fractions", default="0.5,1.0")
    show.add_argument("--basis", choices=["fmz", "imz", "mr"], default="fmz")
    show.set_defaults(func=cmd_show)

    subs.add_parser("check", parents=[common], help="validate every reading in the log"
                    ).set_defaults(func=cmd_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
