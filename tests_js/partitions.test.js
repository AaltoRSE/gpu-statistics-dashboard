// Partitions tab tests: drive the real tabs/partitions.js module through
// loadPartitions against a stubbed fetch, inside a jsdom carrying the
// minimum markup every transitively-imported module touches at import
// time (tabs/jobs.js, users.js, nodes.js enter through the intentional
// router cycle). Covers the queue / wait-time contract: window totals +
// average durations, the selector union (historical names + queue-only
// groups), selected-partition queue filtering and summaries, null-wait
// display, sorting, empty states, and inert rendering of adversarial
// Slurm text.
//
// core/panel.js (reached through the router cycle) installs a module-level
// setInterval freshness clock at import; nothing the tests do can unref
// it, so the runner would hang after the last test. The after() hook at
// the bottom ends the process once the suite has reported.
import assert from "node:assert/strict";
import { test, beforeEach, afterEach, after } from "node:test";
import { JSDOM } from "jsdom";


// One table helper: every table the tabs build at import time needs a
// real <thead> (createTable reads it) and a <tbody>.
function table(id, keys) {
  const ths = keys.map((k) =>
    `<th data-k="${k}">${k}</th>`).join("");
  return `<table id="${id}"><thead><tr>${ths}</tr></thead><tbody></tbody></table>`;
}

const MIN_MARKUP = `
<div id="errBox"></div>
<select id="autoRefresh"><option value="0"></option></select>
<button id="themeBtn" type="button"></button>

<section id="tab-jobs" class="tabpage">
  <select id="jWindow"></select>
  <input id="jRunning" type="checkbox">
  <input id="jLimit" value="100">
  <input id="jSearch">
  <button id="jRefresh" type="button"></button>
  <select id="jPartition"></select>
  <span id="jobsLimitBadge" hidden></span>
  <span id="jMetaCount"></span><span id="jMeta"></span><span id="jCount"></span>
  ${table("jobTable", ["jobid", "name", "user", "partition", "nodes", "state", "start", "gpus", "mean_util", "vram_avg"])}
  <div id="jobEffHistPlot"></div><div id="jobEffHistEmpty"></div>
  <div id="jobDetailResults"></div>
  <span id="jobDetailTitle"></span><span id="jobDetailMeta"></span>
  <div id="jobDetailStats"></div><div id="jobDetailPlot"></div>
  <button id="jobDetailBack" type="button"></button>
  <button id="jobDetailClose" type="button"></button>
  <button id="jobExplorerToggle" type="button"></button>
  <div id="jobExplorer"></div><span id="jobExplorerNote"></span>
</section>

<section id="tab-partitions" class="tabpage">
  <select id="pWindow"><option value="24" selected>24</option></select>
  <select id="pPartition"></select>
  <input id="pRunning" type="checkbox">
  <div id="partitionsResults" class="results-panel">
    <div class="results-content">
      <div id="partBarPlot"></div>
      <div id="partOccupancyPlot"></div>
      <div id="partTrendPlot"></div>
      <span id="pCount"></span>
      ${table("partTable", ["name", "gpus_total", "job_count", "average_wait_seconds", "mean_util"])}
      <span id="pQueueCount"></span>
      <div id="pQueueStats"></div>
      ${table("queueTable", ["jobid", "name", "user", "partition", "requested_gpus", "wait_seconds", "reason"])}
    </div>
  </div>
  <div id="vramResults" class="results-panel">
    <div class="results-content">
      <input id="vNormalize" type="checkbox">
      <select id="vGpuType"><option value="">all</option></select>
      <span id="vTrack"></span>
      <input id="vUtilMin" type="range" value="0">
      <input id="vUtilMax" type="range" value="100">
      <strong id="vUtilMinVal">0</strong>
      <strong id="vUtilMaxVal">100</strong>
      <div id="partVramPlot"></div>
      <div id="vramMeta"></div>
    </div>
  </div>
</section>

<section id="tab-users" class="tabpage">
  <select id="uWindow"></select>
  <input id="uRunning" type="checkbox">
  <input id="uSearch">
  <button id="uRefresh" type="button"></button>
  <span id="uMetaCount"></span><span id="uMeta"></span><span id="uCount"></span>
  ${table("userTable", ["user", "jobs", "running_jobs", "mean_util"])}
  <div id="userSelectedBanner"></div><span id="userSelectedName"></span>
  <button id="userSelectedClear" type="button"></button>
  <div id="userJobsResults"></div><span id="userJobsTitle"></span>
  ${table("userJobsTable", ["jobid", "name", "user", "partition", "state", "start", "gpus", "mean_util", "vram_avg"])}
</section>

<section id="tab-nodes" class="tabpage">
  <input id="nSearch">
  <select id="nGpuType"></select>
  <input id="nBusy" type="checkbox">
  <input id="nGpuOnly" type="checkbox">
  <button id="nRefresh" type="button"></button>
  <span id="nMeta"></span><span id="nCount"></span>
  ${table("nodeTable", ["name", "gpu_type", "gpus_alloc", "util", "active"])}
  <div id="nodeDetailResults"></div>
  <span id="nodeDetailTitle"></span>
  <select id="ndWindow"></select><select id="ndJob"></select>
  <div id="nodeDetailUtilPlot"></div><div id="nodeDetailVramPlot"></div>
</section>

<div id="health"></div>
`;

let dom;
let document;
let window;

function stubGlobals() {
  document = dom.window.document;
  window = dom.window;
  globalThis.document = document;
  globalThis.window = window;
  // router.js's setUrl reads the bare `location` and `history` globals.
  globalThis.location = window.location;
  globalThis.history = window.history;
  globalThis.localStorage = window.localStorage;
  globalThis.fetch = async (path) => {
    const url = String(path);
    if (url.startsWith("/api/partitions?")) {
      return { ok: true, json: async () => globalThis.__partitionsPayload };
    }
    if (url.startsWith("/api/partitions/vram?")) {
      return {
        ok: true,
        json: async () => ({ jobs: [], total: 0,
          window: { start: 0, end: 0 }, step: 120 }),
      };
    }
    return { ok: true, json: async () => ({}) };
  };
  // The tab renders every chart through Plotly.react.
  globalThis.Plotly = { react: () => Promise.resolve() };
}

const RESPONSE = {
  window: { start: 1788013600, end: 1788100000 },
  step: 120,
  partitions: [
    { name: "gpu-h100", mean_util: 36.67, max_util: 60.0, job_count: 2,
      average_wait_seconds: 4500, wait_sample_count: 2, wait_candidate_count: 3,
      gpus_alloc: 2, gpus_total: 16, mean_occupancy: 12.5 },
    { name: "h200_3g.71gb", mean_util: 85.0, max_util: 90.0, job_count: 1,
      average_wait_seconds: null, wait_sample_count: 0, wait_candidate_count: 1,
      gpus_alloc: 1, gpus_total: 8, mean_occupancy: 12.5 },
  ],
  trend: {
    "gpu-h100": [[1000, 25], [1120, 35]],
    "h200_3g.71gb": [[1000, 80]],
  },
  queue: [
    { jobid: "600", name: "<img src=x onerror=window.__pwned=1>", user: "alice",
      account: "acc", partition: "gpu-h100", gpu_group: "gpu-h100",
      qos: "normal", priority: 10, submit: "2026-08-30T15:26:40",
      wait_seconds: 7200, requested_gpus: 2, requested_gpu_type: "h100",
      reason: "Resources" },
    { jobid: "601", name: "short", user: "bob", account: "acc",
      partition: "gpu-h100", gpu_group: "gpu-h100", qos: "", priority: 20,
      submit: "2026-08-30T16:26:40", wait_seconds: 3600, requested_gpus: 1,
      requested_gpu_type: "h100", reason: "Priority" },
    { jobid: "602", name: "array", user: "carol", account: "acc",
      partition: "gpu-fresh", gpu_group: "gpu-fresh", qos: "", priority: 5,
      submit: "Unknown", wait_seconds: null, requested_gpus: 8,
      requested_gpu_type: "h200", reason: "Priority" },
  ],
};

// The tab modules bind their table <tbody> elements (and event listeners)
// at import time, so ONE JSDOM is built for the whole process and every
// test resets the dynamic regions instead of swapping the document.
let tab;

beforeEach(async () => {
  if (!dom) {
    dom = new JSDOM(MIN_MARKUP, { url: "http://localhost/partitions" });
    stubGlobals();
    tab = await import("../static/js/tabs/partitions.js");
  }
  // Fresh dynamic state per test: selection, table bodies, stat rows,
  // count chips, VRAM controls — everything a load writes.
  tab.setSelectedPartition("");
  for (const id of ["partTable", "queueTable", "jobTable",
                    "userTable", "userJobsTable", "nodeTable"]) {
    const tb = document.querySelector("#" + id + " tbody");
    if (tb) tb.innerHTML = "";
  }
  document.getElementById("pQueueStats").innerHTML = "";
  document.getElementById("pQueueCount").textContent = "";
  document.getElementById("pCount").textContent = "";
  document.getElementById("pPartition").innerHTML = "";
  globalThis.__partitionsPayload = JSON.parse(JSON.stringify(RESPONSE));
});
afterEach(() => {
  // DOM globals stay (one shared JSDOM); only the fetch payload resets.
  delete globalThis.__partitionsPayload;
});

async function load() {
  await tab.loadPartitions();
  return tab;
}

test("loadPartitions renders window totals and average durations", async () => {
  await load();
  const rows = [...document.querySelectorAll("#partTable tbody tr")];
  assert.equal(rows.length, 2);
  const cells = (tr) => [...tr.querySelectorAll("td")].map((td) => td.textContent.trim());
  // default sort is mean_util desc: the 85% MIG group renders first
  const h200 = rows[0];
  const h100 = rows[1];
  assert.equal(cells(h200)[4], "85%"); // pctBar renders the rounded percent
  assert.equal(cells(h100)[2], "2"); // jobs in window
  assert.equal(cells(h100)[3], "1h 15m"); // 4500 s through fmtDuration
  assert.equal(cells(h200)[3], "—"); // null average renders as em dash
  const waitTitle = [...h100.querySelectorAll("td")][3].getAttribute("title");
  assert.equal(waitTitle, "Based on 2 of 3 jobs observed in this window");
});

test("selector unions historical names with queue-only groups", async () => {
  const tab = await load();
  const options = [...document.querySelectorAll("#pPartition option")].map((o) => o.value);
  assert.deepEqual(options, ["", "gpu-fresh", "gpu-h100", "h200_3g.71gb"]);
  // selecting the queue-only partition keeps it in the selector
  tab.applyPartitionSelection("gpu-fresh");
  const after = [...document.querySelectorAll("#pPartition option")].map((o) => o.value);
  assert.ok(after.includes("gpu-fresh"));
});

test("selecting a partition filters queue rows and summaries client-side", async () => {
  const tab = await load();
  tab.applyPartitionSelection("gpu-h100");
  const rows = [...document.querySelectorAll("#queueTable tbody tr")];
  assert.equal(rows.length, 2); // 600 and 601, not the gpu-fresh array
  const stats = [...document.querySelectorAll("#pQueueStats .stat")].map((s) =>
    s.querySelector(".stat-value").textContent);
  assert.deepEqual(stats, ["2", "3", "1h 30m", "2h 0m"]); // count, GPUs, mean (5400), max
  tab.applyPartitionSelection("gpu-fresh");
  const freshRows = [...document.querySelectorAll("#queueTable tbody tr")];
  assert.equal(freshRows.length, 1);
  assert.ok(freshRows[0].textContent.includes("602"));
});

test("queue rows sort by known wait desc with unknown last", async () => {
  await load();
  const ids = [...document.querySelectorAll("#queueTable tbody tr")].map(
    (tr) => tr.querySelector("td").textContent.trim());
  assert.deepEqual(ids, ["600", "601", "602"]);
  // the null-wait row renders the em dash in its Wait cell
  const waitCells = [...document.querySelectorAll("#queueTable tbody tr")].map(
    (tr) => tr.querySelectorAll("td")[5].textContent.trim());
  assert.deepEqual(waitCells, ["2h 0m", "1h 0m", "—"]);
});

test("all-partitions and selected empty states differ", async () => {
  const tab = await load();
  tab.applyPartitionSelection("");
  globalThis.__partitionsPayload.queue = [];
  await tab.loadPartitions();
  const allMsg = document.querySelector("#queueTable tbody").textContent;
  assert.ok(allMsg.includes("No jobs currently queued."));
  assert.ok(!allMsg.includes("for this partition."));
  tab.applyPartitionSelection("gpu-h100");
  const selMsg = document.querySelector("#queueTable tbody").textContent;
  assert.ok(selMsg.includes("No jobs currently queued for this partition."));
});

test("adversarial Slurm text renders as inert text, never elements", async () => {
  await load();
  assert.equal(dom.window.__pwned, undefined, "onerror must never fire");
  assert.equal(document.querySelectorAll("#queueTable img").length, 0);
  assert.equal(document.querySelectorAll("#queueTable script").length, 0);
  // the payload is still visible verbatim as text in the Name cell
  const nameCell = document.querySelector("#queueTable tbody tr td:nth-child(2)");
  assert.ok(nameCell.textContent.includes("<img src=x onerror=window.__pwned=1>"));
  assert.equal(nameCell.querySelector("img"), null);
  // ...and the title attribute holds it without breaking out
  assert.equal(nameCell.getAttribute("title"), "<img src=x onerror=window.__pwned=1>");
});

test("queue-only partitions never fabricate historical rows", async () => {
  await load();
  const names = [...document.querySelectorAll("#partTable tbody tr")].map(
    (tr) => tr.dataset.partition);
  assert.deepEqual(names.sort(), ["gpu-h100", "h200_3g.71gb"]);
  assert.ok(!names.includes("gpu-fresh"));
});

test("running_only is sent and queue unchanged by it", async () => {
  const tab = await load();
  document.getElementById("pRunning").checked = true;
  let requested = "";
  globalThis.fetch = async (path) => {
    requested = String(path);
    return { ok: true, json: async () => globalThis.__partitionsPayload };
  };
  await tab.loadPartitions();
  assert.ok(requested.includes("running_only=true"), requested);
  const ids = [...document.querySelectorAll("#queueTable tbody tr")].map(
    (tr) => tr.querySelector("td").textContent.trim());
  assert.deepEqual(ids, ["600", "601", "602"]);
});

after(() => {
  // See the header: panel.js's interval timer keeps the loop alive.
  process.exit(0);
});
