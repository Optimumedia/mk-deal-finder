// Loads the <script> of docs/index.html into a Node vm with a do-nothing DOM, then runs the
// expressions given as JSON on stdin and prints their results as JSON. Used by tests/test_dashboard.py.
"use strict";
const fs = require("fs"), vm = require("vm");
const html = fs.readFileSync(process.argv[2], "utf8");
const m = /<script>([\s\S]*?)<\/script>\s*<\/body>/.exec(html);
if (!m) { console.error("no inline script found"); process.exit(2); }

// Every property / call / `new` on a stub gives another stub; it reads as "" in strings and iterates as empty.
function stub(){
  const f = function(){};
  return new Proxy(f, {
    get(t, k){ if (k === Symbol.toPrimitive) return () => ""; if (k === Symbol.iterator) return function*(){};
      if (k === "then") return undefined; if (k === "length") return 0; return stub(); },
    set(){ return true; }, apply(){ return stub(); }, construct(){ return stub(); },
  });
}
const store = new Map();
const ctx = {
  console, Intl, Date, Math, JSON, URLSearchParams, Promise, setTimeout: () => 0, clearTimeout(){}, setInterval: () => 0, clearInterval(){},
  document: stub(), location: {hostname:"example.github.io", hash:"", pathname:"/", search:""}, history: stub(), navigator: stub(),
  localStorage: {getItem: k => store.has(k) ? store.get(k) : null, setItem: (k, v) => store.set(k, String(v))},
  matchMedia: () => ({matches:false, addEventListener(){}}), getComputedStyle: stub(), requestAnimationFrame(){}, MutationObserver: stub(),
  CSS: {escape: s => s}, innerWidth: 375, scrollY: 0, scrollTo(){}, addEventListener(){},
  fetch: () => Promise.reject(new Error("no network in tests")), AbortController: stub(),
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(m[1], ctx, {filename:"index.html"});
const cases = JSON.parse(fs.readFileSync(0, "utf8"));
const out = {};
for (const [name, expr] of Object.entries(cases)) {
  try { out[name] = vm.runInContext(expr, ctx); } catch (e) { out[name] = {error: String(e && e.stack || e)}; }
}
process.stdout.write(JSON.stringify(out));
