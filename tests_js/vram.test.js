// Functional tests for the Partitions tab's VRAM distribution loading:
// while the batched sacct enrichment runs, the panel's loading chip must
// show real "batch X of Y" progress from /api/partitions/vram/progress —
// best-effort only (a 404 from a stale backend stops the poll; a stale
// request's poll must never rewrite the current chip) — and the data
// response alone decides the panel's outcome. The backend now returns
// every VRAM-bearing candidate, so the old "top of N" truncation caption
// must be gone.
//
// Node's own test runner + jsdom against the app's real index.html, same
// harness shape as queue.test.js: timers are stubbed so node --test can
// never hang, and poll ticks are driven manually.
import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";
import { readFileSync } from "fs";

const html = readFileSync(new URL("../static/index.html", import.meta.url), "utf8");

const VRAM_BODY = {
  window: { start: 1000, end: 2000 }, step: 120, total: 4,
  enriched_frac: 1, failed_batches: 0,
  jobs: [
    { jobid: "1", user: "ann", partition: "h200", gpu_type: "h200",
      mean_util: 50, vram_gb: 12, gpu_hours: 2, gpu_hours_eff: 1 },
    { jobid: "2", user: "bob", partition: "h200", gpu_type: "h200",
      mean_util: 10, vram_gb: 30, gpu_hours: 2, gpu_hours_eff: 0.2 },
  ],
};

const CORE_BODY = {
  window: { start: 1000, end: 2000 },
  partitions: [], trend: {}, step: 120,
};

// boot: build the DOM, route the fetch stub by URL, import a fresh
// partitions module against it. Options:
//   gateVram     unresolved promise placeholder for /api/partitions/vram
//                (releaseVram() resolves every pending VRAM response)
//   vram         body for /api/partitions/vram (default VRAM_BODY)
//   progress     body for /api/partitions/vram/progress (default null)
//   progress404  make the progress route answer 404 (stale backend)
async function boot(opts, bust) {
  const dom = new JSDOM(html, { url: "http://localhost/partitions" });
  global.document = dom.window.document;
  global.window = dom.window;
  global.localStorage = dom.window.localStorage;
  global.location = dom.window.location;
  // Keep the runner's event loop alive-leak-free, as in queue.test.js.
  global.history = { pushState() {}, replaceState() {} };
  // Capture interval callbacks so tests can drive poll ticks manually;
  // panel.js's freshness timer also registers one at import time, which
  // is why ticks target the LAST registered interval.
  const intervals = [];
  const clearedIds = [];
  global.setInterval = (fn) => { intervals.push(fn); return intervals.length; };
  global.clearInterval = (id) => { clearedIds.push(id); };
  global.Plotly = { newPlot: () => {}, react: () => {} };
  const urls = [];
  const vramResolvers = [];
  global.fetch = (url) => {
    urls.push(String(url));
    const u = String(url);
    if (u.startsWith("/api/partitions/vram/progress")) {
      if (opts.progress404) {
        return Promise.resolve({ ok: false, status: 404,
                                 json: () => Promise.resolve({}) });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve(
        opts.progress !== undefined ? opts.progress : null) });
    }
    if (u.startsWith("/api/partitions/vram")) {
      if (opts.gateVram) {
        return new Promise((resolve) => {
          vramResolvers.push(() => resolve({
            ok: true, json: () => Promise.resolve(opts.vram || VRAM_BODY),
          }));
        });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve(
        opts.vram || VRAM_BODY) });
    }
    if (u.startsWith("/api/partitions/queue/live")) {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({
        queue: {},
        totals: { unique_pending_jobs: 0, unique_gpus_requested: 0 },
        queue_available: true, waiting_jobs: [],
      }) });
    }
    if (u.startsWith("/api/partitions/queue")) {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({
        queue: {},
        totals: { unique_pending_jobs: 0, unique_gpus_requested: 0 },
        queue_available: true, waiting_jobs: [],
        wait_history_available: true,
      }) });
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve(
      { ...CORE_BODY }) });
  };
  const mod = await import("../static/js/tabs/partitions.js?cb=" + bust);
  return { dom, mod, urls, intervals, clearedIds,
           releaseVram: () => { while (vramResolvers.length) vramResolvers.shift()(); } };
}

test("VRAM loading chip shows batch progress and clears after the response", async (t) => {
  const ctx = await boot({
    gateVram: true,
    progress: { done: 2, total: 5, failed_batches: 1 },
  }, 1);
  t.after(() => ctx.dom.window.close());
  const doc = ctx.dom.window.document;
  const pending = ctx.mod.loadVram();
  const chip = doc.querySelector("#vramResults .results-loading");
  assert.ok(chip, "the VRAM panel has its loading chip");
  // the chip resets to plain text before the first poll tick lands
  assert.match(chip.textContent, /Loading VRAM distribution/);
  assert.ok(!/batch/.test(chip.textContent), chip.textContent);
  // one poll tick: the enrichment's batch state rewrites the chip in place
  await ctx.intervals.at(-1)();
  assert.match(chip.innerHTML, /batch 2 of 5/);
  assert.match(chip.innerHTML, /1 failed/);
  // release the data request: the overlay clears and the chart renders
  ctx.releaseVram();
  await pending;
  const panel = doc.getElementById("vramResults");
  assert.equal(panel.classList.contains("loading"), false);
  assert.equal(panel.getAttribute("aria-busy"), "false");
  // the poll interval was cleared on completion
  assert.ok(ctx.clearedIds.length >= 1);
  // every candidate arrived: the meta line shows the plain job count,
  // never the old truncation caption
  const meta = doc.getElementById("vramMeta").textContent;
  assert.match(meta, /^2 jobs/);
  assert.ok(!meta.includes("top of"), meta);
});

test("VRAM progress polling stops after a 404 from a stale backend", async (t) => {
  // A backend older than the progress route answers every poll with 404.
  // The poller must clear its own interval on the first 404 tick instead
  // of fetching once per second until the data request lands.
  const ctx = await boot({ gateVram: true, progress404: true }, 2);
  t.after(() => ctx.dom.window.close());
  const pending = ctx.mod.loadVram();
  await ctx.intervals.at(-1)(); // one tick gets the 404
  assert.ok(ctx.clearedIds.length >= 1,
    "the 404 cleared the poll interval (no per-second request storm)");
  // the data request still decides the panel's outcome
  ctx.releaseVram();
  await pending;
  assert.equal(ctx.dom.window.document.getElementById("vramResults")
    .classList.contains("loading"), false);
});

test("a new request resets the chip and a superseded poll cannot rewrite it", async (t) => {
  const ctx = await boot({
    gateVram: true,
    progress: { done: 4, total: 9, failed_batches: 0 },
  }, 3);
  t.after(() => ctx.dom.window.close());
  const doc = ctx.dom.window.document;
  const chip = () => doc.querySelector("#vramResults .results-loading");
  const first = ctx.mod.loadVram();      // token 1, poll interval A
  const stalePoll = ctx.intervals.at(-1);
  await stalePoll();                     // token 1 is current: chip updates
  assert.match(chip().textContent, /batch 4 of 9/);
  const second = ctx.mod.loadVram();     // token 2: chip reset synchronously
  assert.ok(!/batch/.test(chip().textContent),
    "a new request resets the chip before its first poll");
  await ctx.intervals.at(-1)();          // token 2 is current: chip updates
  assert.match(chip().textContent, /batch 4 of 9/);
  await stalePoll();                     // token 1 is stale: must not write
  assert.match(chip().textContent, /batch 4 of 9/,
    "the stale request's poll left the current chip alone");
  ctx.releaseVram();
  await Promise.all([first, second]);
});
