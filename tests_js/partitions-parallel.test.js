// Partitions tab load ordering: the VRAM distribution request must leave
// together with the core /api/partitions request (it renders under its own
// panel and token), not trail behind an await on the core response — the
// old shape serialized it and left the VRAM panel with neither chip nor
// error whenever the core load failed first. A failed core load must leave
// VRAM loading on its own, still able to finish under its own response.
//
// Node's own test runner + jsdom against the app's real index.html, same
// harness shape as vram.test.js/queue.test.js: timers are stubbed so the
// runner can never hang, and the core/VRAM responses are held behind
// gates the tests release explicitly.
import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";
import { readFileSync } from "fs";

const html = readFileSync(new URL("../static/index.html", import.meta.url), "utf8");

const CORE_BODY = {
  window: { start: 1000, end: 2000 },
  partitions: [], trend: {}, step: 120,
};
const VRAM_BODY = {
  window: { start: 1000, end: 2000 }, step: 120, total: 0,
  enriched_frac: 1, failed_batches: 0, jobs: [],
};

// boot: build the DOM, route the fetch stub by URL, import a fresh
// partitions module against it. Options:
//   gateCore   hold /api/partitions behind a gate (release("core"))
//   gateVram   hold /api/partitions/vram behind a gate (release("vram"))
//   coreError  make /api/partitions reject
async function boot(opts, importCacheBust) {
  const dom = new JSDOM(html, { url: "http://localhost/partitions" });
  global.document = dom.window.document;
  global.window = dom.window;
  global.localStorage = dom.window.localStorage;
  global.location = dom.window.location;
  // As in queue.test.js: setUrl only calls pushState, and jsdom's own
  // History object would keep the runner's event loop alive after every
  // booted test.
  global.history = { pushState() {}, replaceState() {} };
  global.setInterval = () => 0;
  global.clearInterval = () => {};
  global.Plotly = { newPlot: () => {}, react: () => {} };
  const urls = [];
  const gates = { core: [], vram: [] };
  global.fetch = (url) => {
    urls.push(String(url));
    const u = String(url);
    if (u.startsWith("/api/partitions/vram/progress")) {
      return Promise.resolve({ ok: false, status: 404,
                               json: () => Promise.resolve({}) });
    }
    if (u.startsWith("/api/partitions/vram")) {
      if (opts.gateVram) {
        return new Promise((resolve) => gates.vram.push(() => resolve({
          ok: true, json: () => Promise.resolve({ ...VRAM_BODY }) })));
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ ...VRAM_BODY }) });
    }
    if (u.startsWith("/api/partitions/queue")) {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({
        queue: {},
        totals: { unique_pending_jobs: 0, unique_gpus_requested: 0 },
        queue_available: true, waiting_jobs: [], wait_history_available: true,
      }) });
    }
    if (u.startsWith("/api/partitions")) {
      if (opts.coreError) return Promise.reject(new Error("prometheus down"));
      if (opts.gateCore) {
        return new Promise((resolve) => gates.core.push(() => resolve({
          ok: true, json: () => Promise.resolve({ ...CORE_BODY }) })));
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ ...CORE_BODY }) });
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ ...CORE_BODY }) });
  };
  const mod = await import("../static/js/tabs/partitions.js?cb=" + importCacheBust);
  return { dom, mod, urls,
           release: (which) => { while (gates[which].length) gates[which].shift()(); } };
}

test("the VRAM request leaves together with the core request, not after it", async (t) => {
  const ctx = await boot({ gateCore: true, gateVram: true }, 1);
  t.after(() => ctx.dom.window.close());
  const doc = ctx.dom.window.document;
  const pending = ctx.mod.loadPartitions();
  // Both endpoints are already in flight — the VRAM fetch no longer waits
  // for the core response, and neither panel's chip waits on the other.
  assert.ok(ctx.urls.some((u) => u.startsWith("/api/partitions?")), ctx.urls);
  assert.ok(ctx.urls.some((u) => u.startsWith("/api/partitions/vram?")), ctx.urls);
  assert.ok(doc.getElementById("partitionsResults").classList.contains("loading"));
  assert.ok(doc.getElementById("vramResults").classList.contains("loading"));
  // Each response unblocks only its own panel, whichever lands first.
  ctx.release("vram");
  await new Promise((r) => setTimeout(r, 20));
  assert.ok(!doc.getElementById("vramResults").classList.contains("loading"));
  assert.ok(doc.getElementById("partitionsResults").classList.contains("loading"));
  ctx.release("core");
  await pending;
  assert.ok(!doc.getElementById("partitionsResults").classList.contains("loading"));
});

test("a failed core load leaves the VRAM panel loading under its own chip", async (t) => {
  const ctx = await boot({ coreError: true, gateVram: true }, 2);
  t.after(() => ctx.dom.window.close());
  const pending = ctx.mod.loadPartitions();
  await pending;
  const doc = ctx.dom.window.document;
  // The core panel reports its failure as before...
  const corePanel = doc.getElementById("partitionsResults");
  assert.ok(!corePanel.classList.contains("loading"));
  assert.match(corePanel.querySelector(".panel-error").textContent,
    /Could not load the GPU type data/i);
  // ...while VRAM, already in flight, keeps its own chip instead of sitting
  // blank with neither chip nor error (the old trailing-await behavior).
  const vramPanel = doc.getElementById("vramResults");
  assert.ok(vramPanel.classList.contains("loading"));
  const chip = vramPanel.querySelector(".card .results-loading");
  assert.ok(chip, "the VRAM card carries its chip");
  assert.equal(chip.textContent, "Loading VRAM distribution…");
  // Its own response still completes the panel on its own.
  ctx.release("vram");
  await new Promise((r) => setTimeout(r, 20));
  assert.ok(!vramPanel.classList.contains("loading"));
  assert.equal(vramPanel.querySelector(".panel-error"), null);
});
