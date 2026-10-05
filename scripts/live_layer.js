// ---- live session layer -------------------------------------------------
// Shared by both chart pages. A live session's report carries DATA.live
// (backtester/live/report.py); a backtest's does not, and then nothing here
// draws or runs. The page calls liveFade() for each trade, liveLevels() while
// fitting the scale, liveOverlay() at the end of each draw and liveInit() once.
//
// Bar times and trade times are the broker's clock, as on the rest of the
// chart; heartbeat and event times are real UTC.

const LIVE = DATA.live || null;
const LIVE_POLL_MS = 30000;
const LIVE_STALE_S = 120;          // a running trader rewrites its pulse every poll
const LIVE_VIEW_KEY = "live-view:" + (LIVE ? `${LIVE.strategy}:${LIVE.symbol}` : "");
const liveSec = (iso) => (iso ? Date.parse(iso) / 1000 : null);
const LIVE_FROM = LIVE && LIVE.account ? liveSec(LIVE.account.live_from) : null;
const LIVE_DIG = DATA.digits ?? 2;
const livePx = (v) => (v === null || v === undefined) ? "—"
  : Number(v).toLocaleString("en-US", { minimumFractionDigits: LIVE_DIG, maximumFractionDigits: LIVE_DIG });
const liveMoney = (v) => (v === null || v === undefined) ? "—"
  : (v < 0 ? "-" : "") + "$" + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const liveSigned = (v) => (v === null || v === undefined) ? "—" : (v > 0 ? "+" : "") + liveMoney(v);
const liveStamp = (s) => new Date(s * 1000).toISOString().slice(0, 16).replace("T", " ");

function liveAgo(seconds) {
  if (seconds === null || !isFinite(seconds)) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 90) return `${s} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  if (s < 172800) return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
  return `${Math.round(s / 86400)} days`;
}

function liveTfSeconds(tf) {
  const m = /^([MHDW])(\d+)$/.exec(String(tf || "").toUpperCase()) ||
            (/^MN/.test(String(tf || "").toUpperCase()) ? ["", "MN", "1"] : null);
  if (!m) return null;
  const unit = { M: 60, H: 3600, D: 86400, W: 604800, MN: 2592000 }[m[1]];
  return unit * Number(m[2]);
}

// ---- on the chart ------------------------------------------------------

// Trades entered before the session began are the replay's; they stay drawn,
// faded, because they are what put the strategy where it is now.
function liveFade(t) {
  return LIVE_FROM !== null && t.t0 < LIVE_FROM ? 0.3 : 1;
}

function liveIndex(sec) {
  const T = DATA.times;
  if (sec <= T[0]) return 0;
  let lo = 0, hi = T.length - 1;
  if (sec >= T[hi]) return hi;
  while (lo < hi - 1) {
    const mid = (lo + hi) >> 1;
    if (T[mid] <= sec) lo = mid; else hi = mid;
  }
  return lo;
}

// Prices the open book needs on screen, once the last bar is in view.
function liveLevels(a, b) {
  if (!LIVE || b < DATA.times.length - 1) return [];
  const out = [];
  for (const p of LIVE.positions || []) out.push(p.price, p.sl, p.tp);
  for (const o of LIVE.resting_orders || []) out.push(o.limit, o.sl, o.tp);
  return out.filter((v) => v !== null && v !== undefined);
}

function liveLine(plot, x1, x2, yy, color, dash, width, opacity) {
  plot.append(el("line", {
    x1, y1: yy, x2, y2: yy, stroke: color, "stroke-width": width,
    "stroke-dasharray": dash, "stroke-opacity": opacity,
  }));
}

function liveLabel(plot, xx, yy, text, color, opacity) {
  const t = el("text", {
    x: xx, y: yy - 4, "text-anchor": "end", fill: color, "fill-opacity": opacity,
    "font-size": 11, "font-weight": 600, "font-family": "system-ui, sans-serif",
  });
  t.style.fontVariantNumeric = "tabular-nums";
  t.textContent = text;
  plot.append(t);
}

function liveOverlay(plot, a, b, W) {
  if (!LIVE) return;
  const last = DATA.times.length - 1;

  // Where the session's account opened.
  if (LIVE_FROM !== null) {
    const i = liveIndex(LIVE_FROM);
    if (i >= a && i <= b) {
      const xx = x(i) - state.px / 2;
      plot.append(el("line", { x1: xx, y1: 0, x2: xx, y2: PLOT_H, stroke: "var(--level)",
                               "stroke-width": 1.5, "stroke-dasharray": "6 4" }));
      const t = el("text", { x: xx + 5, y: 13, fill: "var(--level)", "font-size": 11,
                             "font-weight": 600, "font-family": "system-ui, sans-serif" });
      t.textContent = LIVE.paper ? "paper account opens" : "session opens";
      plot.append(t);
    }
  }

  // The open book: each level runs from where it began to the right edge.
  const edge = Math.min(W, x(last) + state.px * 2) - 4;
  for (const p of LIVE.positions || []) {
    const i0 = liveIndex(liveSec(p.entry_time));
    if (i0 > b) continue;
    const x0 = Math.max(0, x(i0));
    const op = p.in_session === false ? 0.45 : 1;
    liveLine(plot, x0, edge, y(p.price), "var(--text-primary)", "", 1.25, 0.8 * op);
    if (p.sl !== null) liveLine(plot, x0, edge, y(p.sl), "var(--trade-loss)", "5 3", 1.5, op);
    if (p.tp !== null) liveLine(plot, x0, edge, y(p.tp), "var(--trade-win)", "5 3", 1.5, op);
    plot.append(el("circle", { cx: x(i0), cy: y(p.price), r: 4.5, fill: "var(--surface-1)",
                               stroke: "var(--text-primary)", "stroke-width": 2, "stroke-opacity": op }));
    if (b === last || b === last - 1) {
      liveLabel(plot, edge, y(p.price), `${p.side} ${p.volume} @ ${livePx(p.price)}`, "var(--text-primary)", op);
      if (p.sl !== null) liveLabel(plot, edge, y(p.sl), `SL ${livePx(p.sl)}`, "var(--trade-loss)", op);
      if (p.tp !== null) liveLabel(plot, edge, y(p.tp), `TP ${livePx(p.tp)}`, "var(--trade-win)", op);
    }
  }
  for (const o of LIVE.resting_orders || []) {
    const i0 = o.signal_time ? liveIndex(liveSec(o.signal_time)) : last;
    if (i0 > b) continue;
    const x0 = Math.max(0, x(i0));
    liveLine(plot, x0, edge, y(o.limit), "var(--level)", "2 3", 2, 1);
    if (o.void !== null && o.void !== undefined) {
      liveLine(plot, x0, edge, y(o.void), "var(--level)", "1 4", 1, 0.7);
    }
    if (b === last || b === last - 1) {
      liveLabel(plot, edge, y(o.limit), `LIMIT ${o.side} ${o.volume} @ ${livePx(o.limit)}`, "var(--level)", 1);
      if (o.void !== null && o.void !== undefined) {
        liveLabel(plot, edge, y(o.void), `void ${livePx(o.void)}`, "var(--level)", 0.8);
      }
    }
  }
  // A market order waiting for the next open sits just past the last bar.
  if ((LIVE.queued_orders || []).length && b >= last - 1) {
    const q = LIVE.queued_orders[0];
    const xx = Math.min(W - 6, x(last) + state.px * 1.5);
    const yy = y(DATA.close[last]);
    plot.append(el("path", {
      d: q.side === "BUY" ? `M${xx},${yy - 7}L${xx + 6},${yy + 4}L${xx - 6},${yy + 4}Z`
                          : `M${xx},${yy + 7}L${xx + 6},${yy - 4}L${xx - 6},${yy - 4}Z`,
      fill: "var(--level)", stroke: "var(--surface-1)", "stroke-width": 1.5,
    }));
  }
}

// ---- the panels ----------------------------------------------------------

function liveEl(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function liveTile(host, cls, label, value, tone) {
  const box = liveEl("div", cls);
  const v = liveEl("div", "value" + (tone ? " " + tone : ""), value);
  box.append(liveEl("div", "label", label), v);
  host.append(box);
}

function liveTable(caption, head, rows, open) {
  const d = liveEl("details");
  if (open) d.open = true;
  d.append(liveEl("summary", "", caption));
  const wrap = liveEl("div", "table-scroll");
  const table = liveEl("table");
  const thead = liveEl("thead"), tr = liveEl("tr");
  for (const h of head) tr.append(liveEl("th", "", h));
  thead.append(tr);
  const tbody = liveEl("tbody");
  for (const cells of rows) {
    const r = liveEl("tr");
    for (const [text, cls] of cells) r.append(liveEl("td", cls || "", text));
    tbody.append(r);
  }
  if (!rows.length) {
    const r = liveEl("tr");
    const td = liveEl("td", "live-empty", "none");
    td.colSpan = head.length;
    r.append(td);
    tbody.append(r);
  }
  table.append(thead, tbody);
  wrap.append(table);
  d.append(wrap);
  return d;
}

const LIVE_PHASES = {
  waiting_signal: "Waiting for a signal",
  entry_ordered: "Entry ordered",
  limit_resting: "Limit order resting",
  chain_waiting_cross: "Chain step waiting",
  in_position: "Position open",
  exit_warning: "Exit warning",
  exit_due: "Exit at next open",
  no_orders: "Not trading",
};

function liveHeader(pulse) {
  const host = $("livePulse");
  host.innerHTML = "";
  const now = Date.now() / 1000;
  const beat = liveSec((pulse && pulse.updated_at) || LIVE.generated);
  const running = pulse ? pulse.running : LIVE.running;
  const stale = running && now - beat > LIVE_STALE_S;
  const tone = !running ? "stopped" : stale ? "stale" : "running";
  host.append(liveEl("span", "live-dot " + tone));
  const what = LIVE.paper ? "Paper trading" : "Live trading";
  host.append(liveEl("strong", "", `${what} · ${!running ? "stopped" : stale ? "not responding" : "running"}`));

  const bits = [];
  const lastBar = liveSec((pulse && pulse.last_closed_bar) || LIVE.last_closed_bar);
  bits.push(`last bar ${liveStamp(lastBar)} (broker clock)`);
  const closed = liveSec(LIVE.bar_close_utc);
  const tf = liveTfSeconds(LIVE.timeframe);
  if (closed !== null && lastBar === liveSec(LIVE.last_closed_bar)) {
    bits.push(`closed ${liveAgo(now - closed)} ago`);
    if (running && tf && now > closed + tf + 900) {
      const weekday = new Date(now * 1000).getUTCDay();
      bits.push(weekday === 0 || weekday === 6
        ? "market closed for the weekend"
        : `next bar overdue by ${liveAgo(now - closed - tf)}`);
    }
  }
  bits.push(`heartbeat ${liveAgo(now - beat)} ago`);
  if (pulse && pulse.report_error) bits.push(`report failing: ${pulse.report_error}`);
  if (pulse && pulse.failure) bits.push(`stopped on: ${pulse.failure}`);
  host.append(liveEl("span", "live-bits", " · " + bits.join(" · ")));
}

function liveAccount(host) {
  const acc = LIVE.account;
  const box = liveEl("div", "live-card");
  if (!acc) {
    box.append(liveEl("p", "sub", "This session keeps no account of its own."));
    host.append(box);
    return;
  }
  const heading = LIVE.paper ? "Paper account" : "Session record";
  const since = acc.live_from
    ? `since the ${liveStamp(liveSec(acc.live_from))} bar, from ${liveMoney(acc.initial_balance)}`
    : "opens at the next bar";
  box.append(liveEl("h2", "section", `${heading} — ${since}`));
  const stats = liveEl("div", "stats");
  const eq = acc.equity - acc.initial_balance;
  liveTile(stats, "hero", "Equity", liveMoney(acc.equity), eq >= 0 ? "up" : "down");
  liveTile(stats, "tile", "Balance", liveMoney(acc.balance));
  liveTile(stats, "tile", "Floating P&L", liveSigned(acc.floating), acc.floating >= 0 ? "up" : "down");
  liveTile(stats, "tile", "Realised P&L", liveSigned(acc.realised), acc.realised >= 0 ? "up" : "down");
  liveTile(stats, "tile", "Trades", `${acc.trades}  (${acc.wins}W / ${acc.losses}L)`);
  liveTile(stats, "tile", "Win rate", acc.trades ? acc.win_rate_pct.toFixed(1) + "%" : "—");
  liveTile(stats, "tile", "Profit factor", acc.profit_factor === null ? "—" : acc.profit_factor.toFixed(2));
  if (!LIVE.paper && LIVE.broker_account && LIVE.broker_account.balance !== null) {
    liveTile(stats, "tile", "MT5 balance / equity",
             `${liveMoney(LIVE.broker_account.balance)} / ${liveMoney(LIVE.broker_account.equity)}`);
  }
  box.append(stats);
  host.append(box);
}

function liveStatus(host) {
  const st = LIVE.status;
  const box = liveEl("div", "live-card");
  box.append(liveEl("h2", "section", "Strategy"));
  if (!st) {
    box.append(liveEl("p", "sub", "This strategy does not report its state."));
    host.append(box);
    return;
  }
  const line = liveEl("p", "live-status");
  line.append(liveEl("span", "live-pill " + st.phase, LIVE_PHASES[st.phase] || st.phase),
              liveEl("span", "", " " + st.label));
  box.append(line);
  const facts = [];
  if (st.zone) {
    const z = st.zone;
    facts.push(`zone z${z.id} ${z.direction} (${z.kind} anchor ${livePx(z.anchor)}): ` +
               `E50 ${livePx(z.e50)} · MZ0 ${livePx(z.mz0)} · MZ100 ${livePx(z.mz100)}`);
  }
  facts.push(st.generation ? `chain generation ${st.generation}` +
             (st.chain_id !== null && st.chain_id !== undefined ? ` (chain ${st.chain_id})` : "")
             : "chain generation 0 (a ZigZag zone)");
  if (st.candidate) {
    facts.push(`live ${st.candidate.kind} candidate ${livePx(st.candidate.price)}` +
               (st.candidate.confirm_at !== null ? `, confirms at ${livePx(st.candidate.confirm_at)}` : ""));
  }
  for (const f of facts) box.append(liveEl("p", "sub live-fact", f));
  host.append(box);
}

function liveBook() {
  const rows = [];
  for (const p of LIVE.positions || []) {
    rows.push([
      ["position", ""], [p.side, p.side === "BUY" ? "up" : "down"], [String(p.volume), ""],
      [livePx(p.price), ""], [livePx(p.sl), ""], [livePx(p.tp), ""],
      [p.profit === null || p.profit === undefined ? "—" : liveSigned(p.profit),
       (p.profit || 0) >= 0 ? "up" : "down"],
      [liveStamp(liveSec(p.entry_time)), ""], [p.in_session === false ? "replay" : "session", ""],
      [p.tag || "", ""],
    ]);
  }
  for (const o of LIVE.resting_orders || []) {
    rows.push([
      ["limit order", ""], [o.side, o.side === "BUY" ? "up" : "down"], [String(o.volume), ""],
      [livePx(o.limit), ""], [livePx(o.sl), ""], [livePx(o.tp), ""],
      [o.void !== null && o.void !== undefined ? `void ${livePx(o.void)}` : "", ""],
      [o.signal_time ? liveStamp(liveSec(o.signal_time)) : "", ""], ["", ""], [o.tag || "", ""],
    ]);
  }
  for (const o of LIVE.queued_orders || []) {
    rows.push([
      [`${o.order} order, next open`, ""], [o.side, o.side === "BUY" ? "up" : "down"],
      [String(o.volume), ""], [o.limit !== null && o.limit !== undefined ? livePx(o.limit) : "market", ""],
      [livePx(o.sl), ""], [livePx(o.tp), ""], [o.problem || "", ""],
      [o.signal_time ? liveStamp(liveSec(o.signal_time)) : "", ""], ["", ""], [o.tag || "", ""],
    ]);
  }
  return liveTable(
    `Open positions and orders (${rows.length})`,
    ["Kind", "Side", "Volume", "Price", "SL", "TP", "Floating", "Since", "Account", "Tag"],
    rows, true);
}

const LIVE_SKIP = new Set(["time", "kind", "mode", "strategy", "symbol"]);

function liveEventText(e) {
  const f = (v) => (typeof v === "number" ? livePx(v) : v);
  switch (e.kind) {
    case "started":
      return `${e.paper ? "paper, " : ""}replayed from ${String(e.replay_from || "").slice(0, 10)}, ` +
             `${e.replay_trades} replay trades`;
    case "mode": return "handed over to the account";
    case "stopped": return e.failure ? `failed: ${e.failure}` : "stopped on request";
    case "error": return `${e.error}${e.retrying ? " (retrying)" : ""}`;
    case "order_intent":
      return `${e.order} ${e.side} ${e.volume}${e.limit ? " @ " + f(e.limit) : ""}, ` +
             `SL ${f(e.sl)} TP ${f(e.tp)}`;
    case "order_placed": return `limit ${e.side} ${e.volume} @ ${f(e.limit)} resting, ticket ${e.ticket}`;
    case "order_filled":
      return `${e.side} ${e.volume} @ ${f(e.price)}, SL ${f(e.sl)} TP ${f(e.tp)}`;
    case "order_cancelled": return `${e.side || ""} ${e.limit ? "@ " + f(e.limit) : ""} — ${e.reason}`;
    case "order_rejected": return e.reason || "";
    case "position_modified": return `SL ${f(e.sl_from)} → ${f(e.sl)}, TP ${f(e.tp_from)} → ${f(e.tp)}`;
    case "exit_intent": return `close ${e.side} ${e.volume}: ${e.reason}`;
    case "position_closed":
      return e.leftover ? "closed, not the strategy's"
        : `${e.side} ${e.reason ? e.reason.toLowerCase().replace(/_/g, " ") : ""} @ ${f(e.exit_price)}, ` +
          `P&L ${liveSigned(e.net_pnl)}`;
    default:
      return Object.entries(e).filter(([k]) => !LIVE_SKIP.has(k))
        .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" ");
  }
}

function liveLogs() {
  const host = $("liveLog");
  host.innerHTML = "";
  const journal = (LIVE.journal || []).slice().reverse();
  host.append(liveTable(
    `Strategy journal — why it entered, exited, created and cancelled (newest first, ${journal.length})`,
    ["Bar (broker clock)", "Event", "Zone", "Gen", "Detail"],
    journal.map((j) => [
      [j.time ? liveStamp(liveSec(j.time)) : "", ""], [String(j.event).replace(/_/g, " "), ""],
      [j.zone_id === "" ? "" : `z${j.zone_id}`, ""], [String(j.chain_depth), ""],
      [j.detail, "live-detail"],
    ]), true));
  const events = (LIVE.events || []).slice().reverse();
  host.append(liveTable(
    `Runner events — orders as the broker answered them (newest first, ${events.length})`,
    ["Time (UTC)", "Mode", "Event", "Detail"],
    events.map((e) => [
      [String(e.time || "").slice(0, 19).replace("T", " "), ""], [e.mode || "", ""],
      [String(e.kind).replace(/_/g, " "), e.kind === "error" || e.kind === "order_rejected" ? "down" : ""],
      [liveEventText(e), "live-detail"],
    ]), false));
}

// ---- keeping current -----------------------------------------------------

function liveSaveView() {
  try {
    const sc = $("scroller");
    sessionStorage.setItem(LIVE_VIEW_KEY, JSON.stringify({
      px: state.px, fromRight: N * state.px - sc.scrollLeft,
    }));
  } catch (e) { /* the page still works, it just opens at the newest bar */ }
}

function liveRestoreView() {
  const sc = $("scroller");
  let saved = null;
  try { saved = JSON.parse(sessionStorage.getItem(LIVE_VIEW_KEY) || "null"); } catch (e) { saved = null; }
  if (saved && saved.px) {
    state.px = saved.px;
    layout();
    sc.scrollLeft = Math.max(0, N * state.px - saved.fromRight);
  } else {
    sc.scrollLeft = Math.max(0, N * state.px - (sc.clientWidth || 900));
  }
  draw();
}

function livePoll() {
  fetch("pulse.json", { cache: "no-store" })
    .then((r) => (r.ok ? r.json() : null))
    .then((pulse) => {
      if (!pulse) return;
      if (pulse.report_generated && pulse.report_generated !== LIVE.generated) {
        liveSaveView();
        location.reload();
        return;
      }
      liveHeader(pulse);
    })
    .catch(() => liveHeader(null));
}

function liveInit() {
  if (!LIVE) return;
  $("live").hidden = false;
  const head = liveEl("p", "live-head");
  head.id = "livePulse";
  $("live").innerHTML = "";
  $("live").append(head);
  const cards = liveEl("div", "live-cards");
  liveAccount(cards);
  liveStatus(cards);
  $("live").append(cards, liveBook());
  liveHeader(null);
  liveLogs();

  $("statsHead").hidden = false;
  $("statsHead").textContent =
    `The replay this session continues — from ${liveStamp(DATA.times[0]).slice(0, 10)}, ` +
    `the backtest of the same strategy up to now`;
  for (const [color, glyph, text] of [
    ["var(--level)", "┆", LIVE.paper ? "Where the paper account opened" : "Where the session opened"],
    ["var(--text-primary)", "○", "Open position: entry, with its SL and TP to the right edge"],
    ["var(--level)", "┄", "Resting limit order, and the level that voids it"],
  ]) {
    const item = liveEl("div", "item");
    const sw = liveEl("span", "swatch"); sw.style.background = color;
    const g = liveEl("span", "glyph", glyph); g.style.color = color;
    item.append(sw, g, liveEl("span", "", text));
    $("legend").append(item);
  }

  liveRestoreView();
  if (typeof fetch === "function" && typeof setInterval === "function") {
    livePoll();
    setInterval(livePoll, LIVE_POLL_MS);
  }
}
