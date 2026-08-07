# Scheduled signals — status and deployment plan

Handoff document. The signal pipeline works end to end **locally**; deploying it
to an always-on host is the open work. This file carries the decision, the
reasoning behind it, and the traps already paid for, so the next session does
not rediscover them.

---

## Decision: a dedicated Linux VPS running MetaTrader under Wine

Chosen so the server is **byte-identical to the development machine**, and so
every future signal keeps access to MT5 rather than being limited to whatever a
third-party data feed exposes.

### What was rejected, and why

| Option | Why not |
|---|---|
| **The development PC** | Switched off after work. Non-starter for a scheduled sender. |
| **Claude cloud routine** | Scheduling works (min 1 h, cron in UTC), but every run is a *fresh sandbox*: a Wine prefix with MT5 is 1–2 GB and would be rebuilt every run, the installer is a GUI, and root plus outbound TCP to the broker are both required. Viable only with an HTTP data source instead of MT5. |
| **GitHub Actions (`windows-latest`)** | Genuinely possible — real Windows VMs, MT5 installs natively, free tier covers a daily run. Rejected because a cold terminal has **no history cache** and must re-download bars from the broker every run, which is slow and fails *silently short* rather than erroring. Runner IPs also rotate, which brokers may throttle. |
| **HTTP data source, no MT5** | Would remove Wine, display and login problems entirely, and margin zones genuinely do not need broker-specific prices. Rejected to keep MT5 available for future signals that do need it. |
| **Windows VPS** | MT5's native platform and *more* reliable — the Wine quirks below simply do not occur. Rejected for environment consistency with dev, and roughly 2x the cost. Still the fallback if Wine proves troublesome on a headless box. |

### Why Linux+Wine is defensible despite Wine being the flakier runtime

- `mt5linux.sh` in the repo root is MetaQuotes' own installer script and is
  already proven on the dev machine. VPS setup is largely: run it.
- The Wine-specific bugs have **already been found and worked around** (see
  Traps). That cost is sunk.
- Development on Wine and deployment on Windows would be the *safe* direction
  if the two ever diverge — Wine is the harder environment, so code surviving
  it will very likely survive Windows. Switching later is cheap (see
  "Portability" below).

---

## What already works

Verified on the dev machine, sending a real Telegram message:

```
scripts/run_signals.py --check      # bot token + MetaTrader reachability
scripts/run_signals.py --chats      # discover chat ids
scripts/run_signals.py --dry-run    # print what would be sent (the default)
scripts/run_signals.py --send       # deliver
```

The pipeline is `refresh → evaluate → compose → notify → artifacts`:

1. **refresh** — `fetch-mt5.sh` then `manage_data.py convert`, into `data/bars`
2. **evaluate** — the job's strategy computes facts from stored bars
3. **compose** — facts rendered as a Telegram message
4. **notify** — one HTTPS POST per chat, stdlib only
5. **artifacts** — a timestamped report written to `runs/`

Configuration is `configs/signals.yaml` (gitignored; see
`configs/signals.example.yaml`). Jobs name a `strategy` and carry a typed
`params` block validated at load, so a misspelled parameter fails before any
work happens. Recipient groups are joined to jobs by name.

**No condition logic exists yet.** The one strategy, `margin_zones`, always
reports; the message says so explicitly. `facts["in_zone"]` and
`facts["distance_to_zone"]` are computed and are what a condition will key on.
`artifacts: on_signal` is wired but never fires because nothing sets
`facts["signal"]`.

Tests: 233 passing, no network access required.

---

## Deployment plan

### 1. Prerequisites

- **Push the repo to GitHub.** There is still no git remote (`git remote -v` is
  empty). The server needs somewhere to pull from.
- **VPS**: ≥2 GB RAM, Ubuntu. MT5 plus Wine on 1 GB will thrash.

### 2. Provision

```bash
./mt5linux.sh                      # MetaQuotes' Wine + MT5 installer
sudo apt install xvfb              # MT5 is a GUI app; there is no desktop here
```

Then install the Windows Python inside the prefix and the `MetaTrader5`
package, matching `mt5-mcp-server/README.md`.

Run the terminal under a virtual display and **leave it running** — a
persistent, logged-in terminal keeps its history cache warm, which is the whole
reason this beats GitHub Actions.

### 3. Schedule

`deploy/` holds a systemd user service and timer, written for the dev machine
and **never installed or tested**. Two things must be fixed before use:

- **`ExecStartPre=/usr/bin/flock … true` is wrong** — it takes the lock and
  releases it immediately, guarding nothing. The lock must wrap `ExecStart`.
- **`loginctl enable-linger $USER`** is required, or user units stop at logout.
  (`Linger=no` on the dev machine when checked.)

`DISPLAY=:1` in the unit must point at the Xvfb display, not a desktop session.

### 4. Secrets

- Telegram token: `configs/telegram.json` or `TELEGRAM_BOT_TOKEN`
  (`resolve_token()` prefers the environment, so a systemd `Environment=` line
  or an `EnvironmentFile` works without a file in the checkout).
- Broker credentials: `mt5-mcp-server/config.json`, reused by the fetcher.
- Both are gitignored. Neither may enter `signals.yaml`.

---

## Traps already paid for

**`symbol_select` fails spuriously under Wine.** Returns `False` with
`(-3, 'Terminal: Out of memory')` when the symbol is *already* in Market Watch,
on a host with gigabytes free. `scripts/fetch_mt5.py:select()` only calls it
when `info.visible` is false. Do not "fix" this by trusting the return value.

**The terminal caps history at 100,000 bars.** Tools → Options → Charts →
"Max bars in chart". M15 requests come back truncated *silently* — the fetch
reports success. Raise it on the server.

**MT5 access is not concurrent-safe.** The MCP server (`server.py`) and the
fetcher both drive one terminal; a background fetch died silently while the MCP
server held it. Anything touching MT5 needs the same `flock`.

**Margin data cannot be fetched.** `cmegroup.com` returns 403 to every scripted
request including `robots.txt`, stating scripted access is prohibited. Nothing
in this repo attempts to defeat that. `scripts/margins.py import` reads the PDF
a browser downloads. `configs/margins.csv` currently ends **2026-05-01** — it
must be refreshed manually, and nothing on the server will do it.

**Staleness is judged from the newest bar, never from fetch success.** A fetch
can succeed and return nothing over a weekend.

---

## Known gaps, roughly in priority order

1. **`mt5_available()` checks the wrong thing.** It verifies Wine and the
   binary exist, not that the terminal is *connected and logged in*. On the dev
   machine a dead terminal is obvious; on a server it is a silent stale-data
   signal. Harden this before running unattended.

2. **No minimum-bar-count guard.** A truncated history download passes the
   staleness check. Assert a plausible bar count before signalling.

3. **No per-job schedule.** All jobs run at whatever cadence the single timer
   fires. Two designs were discussed:
   - *(a)* one systemd timer per cadence, filtered with `--job` — works today,
     zero new machinery, schedule and job definition live in different files
   - *(b)* `schedule:` inside the job plus a small state store, one timer waking
     hourly and each job deciding whether it is due — self-describing, and the
     same store is needed for signal de-duplication anyway

   *(b)* is the better long-term shape. Neither is built.

4. **No de-duplication.** A condition that stays true will re-fire every run.
   Needs the state store from (3).

5. **No condition logic.** The actual signal. Note the measured constraint: at
   2% ZigZag deviation on EURUSD H4, **37% of zones are touched before the
   anchoring pivot is confirmed**, and confirmation lags a median of 21 bars
   (3.5 days). A zone-entry signal is structurally late; decide whether to
   accept that or signal on the provisional extreme and tolerate retractions.

---

## Portability

The Wine coupling is **two files, about seven lines**. Everything else
mentioning Wine is a docstring.

- `scripts/fetch-mt5.sh` — `exec wine 'C:\Python311\python.exe' …`
- `signals/runner.py:mt5_available()` — checks `wine` and `~/.mt5/…/python.exe`

`scripts/fetch_mt5.py` is plain Python plus `MetaTrader5` and does not know
which host it is on. The bars are identical either way: same broker, same
terminal binary, same package — Wine changes the *invocation*, not the data.

Making the launcher platform-aware (~30 lines) would let the same checkout run
on Linux+Wine or Windows unchanged, and is worth doing before the deployment
choice becomes load-bearing.

Everything downstream of the fetch reads through `BarStore`, which does not
care where bars came from — so swapping to an HTTP source later is one new
fetcher and no other change.
