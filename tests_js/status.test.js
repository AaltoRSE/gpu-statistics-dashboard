// Functional tests for the Status tab: the live panel (occupancy, running
// and pending jobs split CPU/GPU, partitions) and the history panel
// (24 h / 7 d summaries, final states, hourly charts) render from their
// own endpoints; an unreadable source reads as unavailable — never as 0
// or an empty queue; the jobs-per-hour class toggle re-renders without a
// fetch; and "/" lands on Status. Node's own test runner + jsdom against
// the app's real index.html, Plotly and timers stubbed as in queue.test.js.
import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";
import { readFileSync } from "fs";

const html = readFileSync(new URL("../static/index.html", import.meta.url), "utf8");

const LIVE = {
  time: 1790759194,
  nodes_available: true, jobs_available: true, queue_available: true,
  cpu: { alloc: 9798, idle: 7278, unavailable: 156, total: 17232, occupancy_pct: 56.9 },
  gpu: { alloc: 375, idle: 90, unavailable: 10, total: 475, occupancy_pct: 78.9 },
  nodes: { total: 291, unavailable: 4, by_state: [] },
  running: { cpu: { jobs: 708, cpus: 6006, gpus: 0 },
             gpu: { jobs: 303, cpus: 3928, gpus: 375 } },
  pending: {
    cpu: { jobs: 214, tasks: 31530, eligible_jobs: 30, blocked_jobs: 184,
           eligible_age_avg_s: 7200, eligible_age_max_s: 86400 },
    gpu: { jobs: 434, tasks: 3355, eligible_jobs: 224, blocked_jobs: 210,
           eligible_age_avg_s: 3600, eligible_age_max_s: 1823569 },
  },
  pending_reasons: [
    { reason: "Dependency", cpu_jobs: 153, gpu_jobs: 119 },
    { reason: "Priority", cpu_jobs: 21, gpu_jobs: 207 },
  ],
  partitions: [
    { name: "batch-csl", kind: "cpu", nodes: 48, cpus_alloc: 1375, cpus_total: 1920,
      gpus_alloc: 0, gpus_total: 0, running_jobs: 150, pending_jobs: 198 },
    { name: "gpu-h200-141g-short", kind: "gpu", nodes: 14, cpus_alloc: 600,
      cpus_total: 896, gpus_alloc: 100, gpus_total: 112, running_jobs: 90,
      pending_jobs: 40 },
  ],
};

function summary(submitted, failed) {
  return {
    submitted, started: submitted, completed: submitted - failed, failed,
    cancelled: 1, other: 0, ended: submitted + 1,
    states: { COMPLETED: submitted - failed, OUT_OF_MEMORY: failed, CANCELLED: 1 },
    wait_p50_s: 30, wait_p90_s: 7200, wait_avg_s: 3600, wait_samples: submitted,
  };
}

const T = [1790154000, 1790157600, 1790161200];

function series(scale) {
  return {
    submitted: [1 * scale, null, 3 * scale], started: [1, null, 3],
    completed: [1, null, 2], failed: [0, null, 1 * scale], cancelled: [0, null, 0],
    other: [0, null, 0], wait_avg_s: [60, null, 0], wait_p50_s: [30, null, 0],
    cpus_alloc: [100.5, null, 120], gpus_alloc: [0, null, 4.5],
  };
}

const HISTORY = {
  available: true,
  window: { start: T[0], end: T[2] + 600 },
  windows: { day: { start: T[1], end: T[2] + 600 }, week: { start: T[0], end: T[2] + 600 } },
  summary: {
    day: { cpu: summary(10, 2), gpu: summary(5, 1), all: summary(15, 3) },
    week: { cpu: summary(70, 7), gpu: summary(35, 4), all: summary(105, 11) },
  },
  series: { t: T, cpu: series(1), gpu: series(10) },
  coverage: { chunks: 8, failed_chunks: 0, complete: true, records: 10,
              stale_running_rows: 3, unverified_running_rows: 0,
              running_verified: true, blocked_pending_counted: true,
              blocked_pending_jobs: 2 },
  capacity: { cpus: 17232, gpus: 475 },
};

async function boot(opts = {}, bust = "") {
  const dom = new JSDOM(html, { url: "http://localhost" + (opts.path || "/status") });
  global.document = dom.window.document;
  global.window = dom.window;
  global.localStorage = dom.window.localStorage;
  global.location = dom.window.location;
  global.history = { pushState() {}, replaceState() {} };
  // showTab dispatches a resize event: it must be the page's own Event.
  global.Event = dom.window.Event;
  global.setInterval = () => 0;
  global.clearInterval = () => {};
  const plots = {};
  global.Plotly = { newPlot: () => {}, react: (el, traces, layout) => {
    plots[el.id] = { traces, layout };
  } };
  const urls = [];
  global.fetch = (url) => {
    url = String(url);
    urls.push(url);
    const ok = (body) => Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
    if (url.startsWith("/api/status/history/progress")) return ok(null);
    if (url.startsWith("/api/status/history")) {
      if (opts.historyError) return Promise.reject(new Error("history down"));
      return ok(opts.history || HISTORY);
    }
    if (url.startsWith("/api/status/live")) return ok(opts.live || LIVE);
    if (url.startsWith("/api/health")) return ok({ ok: true, prometheus: "http://p", time: "" });
    return ok({});
  };
  const mod = await import("../static/js/tabs/status.js?cb=" + bust);
  return { dom, mod, plots, urls, doc: dom.window.document };
}

test("live panel: occupancy, CPU/GPU split tiles and the partition table", async (t) => {
  const { dom, mod, doc } = await boot({}, "live");
  t.after(() => dom.window.close());
  await mod.loadStatus();
  const tiles = doc.getElementById("sLiveTiles").textContent;
  assert.match(tiles, /Core occupancy56\.9%/);
  assert.match(tiles, /9,798 of 17,232 cores allocated/);
  assert.match(tiles, /GPU occupancy78\.9%/);
  assert.match(tiles, /Running jobs1,011/);
  assert.match(tiles, /CPU 708 .* GPU 303/);
  assert.match(tiles, /Pending jobs648/);
  assert.match(tiles, /254 eligible · 394 blocked/);
  assert.match(tiles, /34,885 array tasks/);
  assert.equal(doc.getElementById("sLiveUnavailable").hidden, true);
  const rows = doc.querySelectorAll("#statusPartTable tbody tr");
  assert.equal(rows.length, 2);
  assert.match(rows[1].textContent, /gpu-h200-141g-short/);
  assert.match(rows[1].textContent, /100\/112/);
  assert.equal(doc.getElementById("statusLiveResults").classList.contains("loading"), false);
});

test("squeue down: pending reads unavailable, never 0 or an empty queue", async (t) => {
  const live = { ...LIVE, queue_available: false, pending: null, pending_reasons: null,
    partitions: LIVE.partitions.map((p) => ({ ...p, pending_jobs: null })) };
  const { dom, mod, doc, plots } = await boot({ live }, "squeue");
  t.after(() => dom.window.close());
  await mod.loadStatus();
  const tiles = doc.getElementById("sLiveTiles").textContent;
  assert.match(tiles, /Pending jobs—unavailable \(squeue\) — not an empty queue/);
  assert.doesNotMatch(tiles, /Pending jobs0/);
  const banner = doc.getElementById("sLiveUnavailable");
  assert.equal(banner.hidden, false);
  assert.match(banner.textContent, /pending jobs \(squeue\)/);
  const cells = [...doc.querySelectorAll("#statusPartTable tbody tr")]
    .map((tr) => tr.lastElementChild.textContent);
  assert.deepEqual(cells, ["—", "—"]);
  assert.equal(plots.sReasonPlot.traces.length, 0);
  assert.match(plots.sReasonPlot.layout.annotations[0].text, /not an empty queue/);
});

test("history panel: 24 h / 7 d summary split by class, states and hourly data", async (t) => {
  const { dom, mod, doc, plots } = await boot({}, "history");
  t.after(() => dom.window.close());
  await mod.loadStatus();
  const head = doc.querySelectorAll("#statusSummaryTable thead tr");
  assert.equal(head.length, 2);
  assert.match(head[0].textContent, /Past 24 hours[\s\S]*Past 7 days/);
  const body = [...doc.querySelectorAll("#statusSummaryTable tbody tr")]
    .map((tr) => [...tr.children].map((td) => td.textContent));
  assert.deepEqual(body[0], ["Submitted", "10", "5", "15", "70", "35", "105"]);
  assert.deepEqual(body[3], ["Failed", "2", "1", "3", "7", "4", "11"]);
  assert.deepEqual(body[6], ["Mean queue wait", "1h 0m", "1h 0m", "1h 0m",
                             "1h 0m", "1h 0m", "1h 0m"]);
  const tiles = doc.getElementById("sHistTiles").textContent;
  assert.match(tiles, /Submitted, past 24 h15/);
  assert.match(tiles, /Submitted, past 7 days105/);
  const states = doc.querySelector("#statusStateTable tbody").textContent;
  assert.match(states, /OUT_OF_MEMORY\s*failed/);
  assert.equal(doc.querySelectorAll("#statusHourlyTable tbody tr").length, 3);
  // an uncovered hour is a gap in every series, not a zero
  const gpuSubmitted = plots.sJobsPlot.traces.find((tr) => tr.name === "Submitted").y;
  assert.deepEqual(gpuSubmitted, [11, null, 33]);   // all = cpu + gpu
  // cores are stacked by class; the capacity line is drawn
  assert.deepEqual(plots.sCoresPlot.traces.map((tr) => tr.name), ["CPU jobs", "GPU jobs"]);
  assert.equal(plots.sCoresPlot.layout.shapes[0].y0, 17232);
  // the log wait axis floors a 0 s wait at 1 s; the hover keeps "0s"
  const cpuMedian = plots.sWaitPlot.traces.find((tr) => tr.name === "CPU jobs · median");
  assert.deepEqual(cpuMedian.y, [30, null, 1]);
  assert.equal(cpuMedian.customdata[2], "0s");
  assert.equal(doc.getElementById("sHistCoverage").hidden, true);
});

test("class toggle re-renders the jobs chart without a fetch", async (t) => {
  const { dom, mod, doc, plots, urls } = await boot({}, "toggle");
  t.after(() => dom.window.close());
  await mod.loadStatus();
  const before = urls.length;
  doc.querySelector('#sJobsClass button[data-cls="gpu"]').click();
  assert.equal(urls.length, before);
  const submitted = plots.sJobsPlot.traces.find((tr) => tr.name === "Submitted").y;
  assert.deepEqual(submitted, [10, null, 30]);
  assert.equal(doc.querySelector('#sJobsClass button[data-cls="gpu"]')
    .getAttribute("aria-pressed"), "true");
  assert.equal(doc.querySelector('#sJobsClass button[data-cls="all"]')
    .getAttribute("aria-pressed"), "false");
});

test("sacct unavailable: history says so and the live panel still renders", async (t) => {
  const { dom, mod, doc } = await boot({ history: { available: false } }, "nohist");
  t.after(() => dom.window.close());
  await mod.loadStatus();
  assert.equal(doc.getElementById("sHistBody").hidden, true);
  const banner = doc.getElementById("sHistCoverage");
  assert.equal(banner.hidden, false);
  assert.match(banner.textContent, /not an idle week/);
  assert.match(doc.getElementById("sLiveTiles").textContent, /Core occupancy56\.9%/);
});

test("a failed history request keeps the live panel and offers a retry", async (t) => {
  const { dom, mod, doc } = await boot({ historyError: true }, "histerr");
  t.after(() => dom.window.close());
  await mod.loadStatus();
  const err = doc.querySelector("#statusHistoryResults .panel-error");
  assert.ok(err, "history panel shows its own error");
  assert.ok(err.querySelector("button"), "with a retry button");
  assert.equal(doc.querySelector("#statusLiveResults .panel-error"), null);
  assert.match(doc.getElementById("sLiveTiles").textContent, /Running jobs1,011/);
});

test("partial accounting coverage is disclosed, not hidden", async (t) => {
  const history = { ...HISTORY, coverage: { ...HISTORY.coverage, failed_chunks: 1,
                                            complete: false } };
  const { dom, mod, doc } = await boot({ history }, "partial");
  t.after(() => dom.window.close());
  await mod.loadStatus();
  const banner = doc.getElementById("sHistCoverage");
  assert.equal(banner.hidden, false);
  assert.match(banner.textContent, /1 of 8 days of accounting data could not be read/);
  assert.doesNotMatch(banner.textContent, /pending queue/);
});

test("an unreadable queue is disclosed as missing blocked submissions", async (t) => {
  const history = { ...HISTORY, coverage: { ...HISTORY.coverage,
                                            blocked_pending_counted: false } };
  const { dom, mod, doc } = await boot({ history }, "noqueue");
  t.after(() => dom.window.close());
  await mod.loadStatus();
  const banner = doc.getElementById("sHistCoverage");
  assert.equal(banner.hidden, false);
  assert.match(banner.textContent, /still blocked by a dependency or hold/);
});

test("the root path lands on the Status tab", async (t) => {
  const { dom, doc, urls } = await boot({ path: "/" }, "root");
  t.after(() => dom.window.close());
  // The router module is shared by every test in this file; an earlier
  // test's successful load must not make this first visit skip its fetch.
  const router = await import("../static/js/core/router.js");
  router.loaded.status = false;
  await import("../static/js/main.js?cb=root");
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.ok(doc.getElementById("tab-status").classList.contains("active"));
  assert.ok(!doc.getElementById("tab-jobs").classList.contains("active"));
  assert.match(doc.title, /^Status/);
  assert.ok(urls.some((u) => u.startsWith("/api/status/live")));
  assert.ok(urls.some((u) => u.startsWith("/api/status/history")));
  assert.ok(!urls.some((u) => u.startsWith("/api/jobs")));
});
