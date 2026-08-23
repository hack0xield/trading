// Execute a chart page's script under a minimal DOM stub and report whether it
// runs to completion.
//
// The chart is one top-level <script>. Anything that throws in it - a `const`
// read before its declaration is the easy one - aborts the whole thing, and
// the page renders blank: no chart, no table, no error anyone sees without
// opening a console. This runs it headlessly so the suite notices instead.
//
// Driven by tests/test_senior25.py::TestChartRenders; also usable by hand:
//   node tests/render_check.js runs/<dir>/chart.html
const fs = require("fs"), vm = require("vm");
const html = fs.readFileSync(process.argv[2], "utf8");
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.log("FAIL: no <script> block"); process.exit(1); }

const created = { svg: 0, html: 0, level: 0, trade: 0 };
function node(tag, ns) {
  ns ? created.svg++ : created.html++;
  const n = {
    tag, children: [], attrs: {}, style: {}, dataset: {},
    textContent: "", innerHTML: "", className: "", hidden: false,
    clientWidth: Number(process.env.VIEW_W || 900), clientHeight: 400,
    scrollLeft: Number(process.env.VIEW_X || 0), scrollWidth: 40000,
    setAttribute(k, v) { this.attrs[k] = v; const s = String(v);
      if (s.includes("--level")) created.level++;
      if (s.includes("--trade-")) created.trade++; },
    getAttribute(k) { return this.attrs[k]; },
    append(...c) { this.children.push(...c); },
    get lastChild() { return this.children[this.children.length - 1]; },
    get firstChild() { return this.children[0]; },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {}, removeEventListener() {}, focus() {},
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 900, height: 400 }),
    scrollTo() {}, remove() {},
  };
  return n;
}
const byId = {};
const document = {
  body: node("body"),
  getElementById: (id) => (byId[id] ||= node("div")),
  createElement: (t) => node(t),
  createElementNS: (ns, t) => node(t, ns),
};
const window = { addEventListener() {}, innerWidth: 1200, innerHeight: 800 };
const sandbox = {
  document, window, console,
  requestAnimationFrame: (fn) => fn(),
  ResizeObserver: class { observe() {} disconnect() {} },
  Date, Math, JSON, Object, Array, String, Number, Boolean, isNaN, parseFloat, parseInt, Intl,
};
try {
  vm.createContext(sandbox);
  vm.runInContext(m[1], sandbox, { filename: "chart.js" });
  console.log(`OK: script ran to completion`);
  console.log(`  SVG elements created : ${created.svg}`);
  console.log(`  HTML elements created: ${created.html}`);
  console.log(`  title  : ${byId.title ? byId.title.textContent : "(not set)"}`);
  console.log(`  count  : ${byId.count ? byId.count.textContent : "(not set)"}`);
  console.log(`  pattern elements drawn: ${created.level}`);
  console.log(`  order elements drawn  : ${created.trade}`);
  console.log(`  legend items: ${byId.legend ? byId.legend.children.length : 0}`);
  console.log(`  table rows  : ${byId.tbody ? byId.tbody.children.length : (byId.rows ? byId.rows.children.length : "n/a")}`);
} catch (e) {
  console.log(`FAIL: ${e.name}: ${e.message}`);
  console.log((e.stack || "").split("\n").slice(0, 4).join("\n"));
  process.exit(1);
}
