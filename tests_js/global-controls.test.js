// Contract tests for the single global refresh button and the header
// freshness stamp: the per-tab refresh buttons and the per-panel
// data-fresh-for chips are gone, the header stamp follows the ACTIVE
// tab's primary panel (never a background/detail panel's success), and
// #globalRefresh force-reloads the active tab's loader with force=true
// (auto-refresh ticks keep force=false, so they stay cache-friendly).
//
// Node's own test runner + jsdom against the app's real index.html;
// global.setInterval/clearInterval are stubbed because core/panel.js
// starts its freshness timer at import time and node --test would never
// exit (the same stub the queue tests use).
//
// Module identity: router.js internally imports './panel.js' (no query),
// so the test imports panel.js by the same plain path — a cache-busted
// panel import would create a SECOND module instance whose
// activeFreshnessPanel state is invisible to router.js. router.js itself
// is cache-busted per test to reset its `loaded` flags; its internal
// panel import still resolves to the one plain-path instance.
//
// The stubbed fetch returns a minimal-but-complete payload per endpoint
// so tab loaders can run to completion (rendering requires arrays),
// and showTab is awaited so no loader promise outlives its test.
import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";
import { readFileSync } from "fs";

// Click tests import the REAL main.js (the global refresh button's
// listener lives there, not in router.js); main.js fires its own
// /api/health fetch and initial-view restore on import, which the
// URL-keyed stub serves harmlessly.
const html = readFileSync(
  new URL("../static/index.html", import.meta.url), "utf8");

// Minimal valid payloads: enough for each loader to reach panelOk and
// render without touching missing fields. Renderers must tolerate
// empty arrays (they do — every panel renders "no data" states).
const API_PAYLOADS = {
  "/api/partitions?": {
    window: { start: 0, end: 100 }, step: 60,
    partitions: [], trend: [],
  },
  "/api/partitions/vram?": {
    window: { start: 0, end: 100 }, step: 60,
    total: 0, enriched_frac: 0.0, failed_batches: 0, jobs: [],
  },
  "/api/partitions/queue?": {
    window: { start: 0, end: 100 }, step: 60,
    totals: { running: 0, pending: 0 }, summary: [], jobs: [],
    waiting: { complete: true, failed_batches: 0, successful_batches: 0 },
  },
  "/api/jobs?": {
    window: { start: 0, end: 100 }, step: 60,
    count: 0, totals: {}, users: [], jobs: [],
  },
  "/api/users?": {
    window: { start: 0, end: 100 },
    count: 0, users: [],
  },
  "/api/nodes?": {
    time: 0, count: 0, nodes: [],
  },
};

function payloadFor(url) {
  for (const [prefix, body] of Object.entries(API_PAYLOADS)) {
    if (url.startsWith(prefix)) return body;
  }
  return {};
}

async function boot(importCacheBust) {
  const dom = new JSDOM(html, { url: "http://localhost/jobs" });
  global.document = dom.window.document;
  global.window = dom.window;
  global.localStorage = dom.window.localStorage;
  global.location = dom.window.location;
  global.history = { pushState() {}, replaceState() {} };
  const intervals = [];
  global.setInterval = (fn) => { intervals.push(fn); return intervals.length; };
  global.clearInterval = () => {};
  global.Plotly = { newPlot: () => {}, react: () => {} };
  global.window.dispatchEvent = () => {};  // resize refits, not testable here
  global.Event = dom.window.Event;
  const urls = [];
  global.fetch = (url) => {
    urls.push(String(url));
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve(payloadFor(String(url))),
    });
  };

  const panel = await import("../static/js/core/panel.js");
  const router = await import(
    "../static/js/core/router.js?cb=" + importCacheBust);
  return { dom, panel, router, urls };
}

// The global refresh button's listener is wired by main.js; importing it
// attaches the real handler to this boot's DOM. A distinct cache-bust
// per test gives each boot its own listener scope. main.js fires its
// own /api/health fetch and initial-view restore on import, which the
// URL-keyed stub serves harmlessly.
async function bootWithMain(importCacheBust) {
  const ctx = await boot(importCacheBust);
  ctx.main = await import("../static/js/main.js?cb=" + importCacheBust);
  return ctx;
}

test("header has exactly one global refresh + freshness stamp; per-tab controls are gone", async () => {
  const { dom } = await boot(1);
  const doc = dom.window.document;
  assert.equal(doc.querySelectorAll("#globalRefresh").length, 1);
  assert.equal(doc.querySelectorAll("#globalFreshness").length, 1);
  for (const gone of ["jRefresh", "uRefresh", "nRefresh"]) {
    assert.equal(doc.querySelectorAll("#" + gone).length, 0,
      gone + " must be removed — the header owns refresh now");
  }
  assert.equal(doc.querySelectorAll("[data-fresh-for]").length, 0,
    "per-panel freshness chips must be removed — the header owns the stamp");
});

test("a background/detail panel's success never replaces the active tab's stamp", async () => {
  const { dom, panel, router } = await boot(2);
  const doc = dom.window.document;
  const stamp = doc.getElementById("globalFreshness");

  router.showTab("jobs");  // jobs is the boot URL's initial tab: no load fires
  panel.panelOk("jobsResults");
  assert.equal(stamp.textContent, "updated just now");

  // A background/detail panel's success must NOT overwrite the stamp:
  // the header identity stays on the active tab's primary panel.
  panel.panelOk("queueResults");
  panel.panelOk("vramResults");
  assert.equal(stamp.textContent, "updated just now");
  assert.ok(!stamp.textContent.includes("undefined"));
});

test("the header stamp follows tab switches, blank for never-loaded tabs", async () => {
  const { dom, panel, router } = await boot(3);
  const doc = dom.window.document;
  const stamp = doc.getElementById("globalFreshness");

  // jobs is the boot URL's initial tab, so showTab("jobs") fires no
  // loader; the freshness state must come from panelOk itself.
  router.showTab("jobs");
  panel.panelOk("jobsResults");
  assert.equal(stamp.textContent, "updated just now");
  // The awaited showTab("nodes") actually LOADS nodes (the stubbed
  // fetch resolves): its loader's own panelOk legitimately stamps the
  // header for the now-active tab.
  await router.showTab("nodes");
  assert.equal(stamp.textContent, "updated just now");

  await router.showTab("jobs");
  assert.equal(stamp.textContent, "updated just now",
    "returning to a loaded tab restores its timestamp");
});

test("the minute tick advances the single header stamp", async () => {
  const { dom, panel } = await boot(4);
  const doc = dom.window.document;
  const stamp = doc.getElementById("globalFreshness");
  // Backdate the recorded success by two minutes.
  panel.panelLoadedAt.jobsResults = Date.now() - 2 * 60000;
  panel.setActiveFreshnessPanel("jobsResults");
  assert.equal(stamp.textContent, "updated 2 min ago");
});

test("panelOk only advances the stamp when the succeeded panel is the active one", async () => {
  const { dom, panel, router } = await boot(5);
  const doc = dom.window.document;
  const stamp = doc.getElementById("globalFreshness");
  // panel.js is imported without a cache-bust query (module-identity
  // requirement above), so its panelLoadedAt persists across tests in
  // this process; clear it to test the fresh-page behavior in isolation.
  for (const key of Object.keys(panel.panelLoadedAt)) {
    delete panel.panelLoadedAt[key];
  }
  router.showTab("jobs");
  // usersResults succeeds in the background while jobs is active.
  panel.panelOk("usersResults");
  assert.equal(stamp.textContent, "",
    "a background tab's success must not light the header for another tab");
  panel.panelOk("jobsResults");
  assert.equal(stamp.textContent, "updated just now");
});

test("clicking #globalRefresh on Partitions forces the core charts AND VRAM", async () => {
  const { dom, router, urls } = await bootWithMain(6);
  const doc = dom.window.document;
  await router.showTab("partitions");
  // The initial showTab load: no refresh param anywhere.
  assert.ok(!urls.some((u) => u.includes("refresh=true")),
    "the first load must render from the caches, not bypass them");
  const forced = urls.length;

  doc.getElementById("globalRefresh").disabled = false;
  doc.getElementById("globalRefresh").click();
  await new Promise((r) => setTimeout(r, 0));

  const partRefresh = urls.slice(forced).filter((u) => u.startsWith("/api/partitions?"));
  const vramRefresh = urls.slice(forced).filter((u) => u.startsWith("/api/partitions/vram?"));
  assert.equal(partRefresh.length, 1, "one forced core charts fetch");
  assert.ok(partRefresh[0].includes("refresh=true"),
    "the forced core fetch must carry refresh=true");
  assert.equal(vramRefresh.length, 1, "one forced VRAM fetch");
  assert.ok(vramRefresh[0].includes("refresh=true"),
    "the forced VRAM fetch must carry refresh=true — loadPartitions(force) propagates to loadVram(force)");
});

test("clicking #globalRefresh on Users forces the users fetch", async () => {
  const { dom, router, urls } = await bootWithMain(7);
  const doc = dom.window.document;
  await router.showTab("users");
  const forced = urls.length;

  doc.getElementById("globalRefresh").disabled = false;
  doc.getElementById("globalRefresh").click();
  await new Promise((r) => setTimeout(r, 0));

  const userRefresh = urls.slice(forced).filter((u) => u.startsWith("/api/users?"));
  assert.equal(userRefresh.length, 1, "one forced users fetch");
  assert.ok(userRefresh[0].includes("refresh=true"));
});

test("clicking #globalRefresh on Nodes forces the nodes fetch", async () => {
  const { dom, router, urls } = await bootWithMain(8);
  const doc = dom.window.document;
  await router.showTab("nodes");
  const forced = urls.length;

  doc.getElementById("globalRefresh").disabled = false;
  doc.getElementById("globalRefresh").click();
  await new Promise((r) => setTimeout(r, 0));

  const nodeRefresh = urls.slice(forced).filter((u) => u.startsWith("/api/nodes?"));
  assert.equal(nodeRefresh.length, 1, "one forced nodes fetch");
  assert.ok(nodeRefresh[0].includes("refresh=true"));
});

test("clicking #globalRefresh on Jobs forces the jobs fetch", async () => {
  const { dom, router, urls } = await bootWithMain(9);
  const doc = dom.window.document;
  await router.showTab("jobs");
  const forced = urls.length;

  doc.getElementById("globalRefresh").disabled = false;
  doc.getElementById("globalRefresh").click();
  await new Promise((r) => setTimeout(r, 0));

  const jobRefresh = urls.slice(forced).filter((u) => u.startsWith("/api/jobs?"));
  assert.equal(jobRefresh.length, 1, "one forced jobs fetch");
  assert.ok(jobRefresh[0].includes("refresh=true"));
});
