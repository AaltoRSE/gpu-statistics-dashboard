// Functional tests for the Partitions tab's pending-jobs (queue status)
// tables: the unavailable state must never read as "No pending jobs", the
// unknown-GPU disclosure must say "unknown" rather than a fabricated 0,
// and a reachable-but-empty queue must read "No pending jobs" with no
// warning. Node's own test runner + jsdom against the app's real
// index.html; global.setInterval is stubbed because core/panel.js starts
// its freshness timer at import time and node --test would never exit.
import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";
import { readFileSync } from "fs";
import { pathToFileURL } from "url";

const html = readFileSync(new URL("../static/index.html", import.meta.url), "utf8");

const WAITING = [
  { jobid: "500", user: "ann", partition: "gpu-h200,gpu-h200-ellis",
    state: "PENDING", submit: "2026-09-17T10:00:00", start: "",
    reason: "(Resources)", nodes: 2, gpus: 4, gpu_type: "h200",
    groups: ["h200"], wait_s: 5400, gpu_total: 8 },
  { jobid: "501", user: "bob", partition: "gpu-h200-mig,batch",
    state: "PENDING", submit: "2026-09-17T09:30:00",
    start: "2026-09-17T18:00:00", reason: "(Priority)",
    nodes: 1, gpus: 1, gpu_type: "",
    groups: ["h200", "h200_3g.71gb"], wait_s: 7200, gpu_total: 1 },
];

function boot({ queue, queueAvailable, waitingJobs = [], waitHistoryAvailable = true,
                partitions }, importCacheBust) {
  const dom = new JSDOM(html, { url: "http://localhost/partitions" });
  global.document = dom.window.document;
  global.window = dom.window;
  global.localStorage = dom.window.localStorage;
  global.location = dom.window.location;
  // A minimal stub: setUrl only calls pushState, and binding jsdom's own
  // History object here keeps the runner's event loop alive after every
  // booted test (the leak that hung the suite).
  global.history = { pushState() {}, replaceState() {} };
  global.setInterval = () => 0; // panel.js's freshness timer
  global.Plotly = { newPlot: () => {}, react: () => {} };
  global.fetch = () => Promise.resolve({
    ok: true,
    json: () => Promise.resolve({
      window: { start: 1000, end: 2000 },
      partitions: partitions || [{ name: "h200", mean_util: 50, max_util: 60,
                     job_count: 1, gpus_alloc: 1, gpus_total: 20,
                     mean_occupancy: 7.5 }],
      trend: { h200: [[1000, 50]] },
      queue,
      queue_available: queueAvailable,
      waiting_jobs: waitingJobs,
      wait_history_available: waitHistoryAvailable,
    }),
  });
  // cache-bust: each scenario gets a fresh module instance against its
  // own DOM (module-level table bindings would otherwise be frozen to
  // the first scenario's DOM).
  return import("../static/js/tabs/partitions.js?cb=" + importCacheBust)
    .then(async (mod) => {
      await mod.loadPartitions();
      return { dom, mod };
    });
}

test("queue table renders wait columns and an unknown-GPU state", async (t) => {
  const { dom } = await boot({
    queueAvailable: true,
    queue: {
      "h200": { jobs: 3, gpus: 12, gpus_min: 12, started_jobs: 2, avg_wait_s: 5400 },
      "h200_3g.71gb": { jobs: 1, gpus: null, gpus_min: 4, started_jobs: 0, avg_wait_s: null },
      "__total__": { jobs: 4, gpus: 12, gpus_min: 16, started_jobs: 2, avg_wait_s: 5400 },
    },
    waitingJobs: WAITING,
  }, 1);
  t.after(() => dom.window.close());
  const doc = dom.window.document;
  const rows = doc.querySelectorAll("#partQueueTable tbody tr");
  assert.equal(rows.length, 2); // __total__ is meta, never a row
  const text = doc.querySelector("#partQueueTable tbody").textContent;
  assert.match(text, /h200/);
  assert.match(text, /unknown/); // a null exact total is disclosed, not a 0
  assert.match(text, /12/);
  // the historical wait columns render real values and null as —
  assert.match(text, /1h 30m/);   // avg_wait_s 5400 for h200
  assert.match(text, /—/);        // null avg_wait_s for the MIG profile
  assert.equal(doc.getElementById("pQueueHint").hidden, true);
  assert.equal(doc.getElementById("pWaitHistoryHint").hidden, true);
  // the meta line is the UNIQUE cluster-wide count from __total__
  assert.match(doc.getElementById("pQueueMeta").textContent, /4 pending jobs/);
});
test("waiting jobs list de-duplicates rows and formats durations", async (t) => {
  const { dom } = await boot({
    queueAvailable: true,
    queue: { "__total__": { jobs: 2, gpus: 9, gpus_min: 9, started_jobs: 0, avg_wait_s: null } },
    waitingJobs: WAITING,
  }, 2);
  t.after(() => dom.window.close());
  const doc = dom.window.document;
  const rows = doc.querySelectorAll("#pendingJobsTable tbody tr");
  // job 501 can run on h200 AND the h200_3g.71gb profile but is listed
  // exactly once
  assert.equal(rows.length, 2);
  const text = doc.querySelector("#pendingJobsTable tbody").textContent;
  assert.match(text, /500/);
  assert.match(text, /501/);
  assert.match(text, /1h 30m/);   // wait_s 5400
  assert.match(text, /2h/);       // wait_s 7200
  assert.match(text, /\(Resources\)/);
  assert.match(text, /\(Priority\)/);
  // estimated start renders as a reformatted sacct time; empty stays —
  assert.match(text, /2026-09-17 18:00/);
  // the GPU-type cell shows the job's eligible types
  const row501 = [...rows].find((r) => r.textContent.includes("501"));
  assert.match(row501.textContent, /h200, h200_3g\.71gb/);
  // the raw Partition column is plain TEXT — a partition name is no
  // longer a selectable group and must not render as a link
  assert.equal(row501.querySelectorAll("a.partitionlink").length, 0);
  assert.match(row501.textContent, /gpu-h200-mig, batch/);
  assert.match(doc.getElementById("pWaitingMeta").textContent, /2 waiting/);
});
test("selecting a GPU type filters the waiting list by groups", async (t) => {
  const { dom, mod } = await boot({
    queueAvailable: true,
    queue: {
      "h200": { jobs: 2, gpus: 9, gpus_min: 9, started_jobs: 0, avg_wait_s: null },
      "h200_3g.71gb": { jobs: 1, gpus: 1, gpus_min: 1, started_jobs: 0, avg_wait_s: null },
      "__total__": { jobs: 2, gpus: 9, gpus_min: 9, started_jobs: 0, avg_wait_s: null },
    },
    waitingJobs: WAITING,
  }, 3);
  t.after(() => dom.window.close());
  const doc = dom.window.document;
  mod.setSelectedPartition("h200_3g.71gb");
  await mod.applyPartitionSelection("h200_3g.71gb");
  let rows = doc.querySelectorAll("#pendingJobsTable tbody tr");
  assert.equal(rows.length, 1); // only job 501 lists the profile in groups
  assert.match(rows[0].textContent, /501/);
  // the by-type summary stays visible while filtered
  assert.equal(doc.querySelectorAll("#partQueueTable tbody tr").length, 2);
  // pending-only GPU types stay selectable even with no utilization rows
  const opts = [...doc.querySelectorAll("#pPartition option")].map((o) => o.value);
  assert.ok(opts.includes("h200") && opts.includes("h200_3g.71gb"));
  // the meta line reports the selected type's pending count
  assert.match(doc.getElementById("pQueueMeta").textContent, /1 pending job/);
  // the selector label says GPU type, not partition
  assert.equal(
    doc.querySelector('label[for="pPartition"], #pPartition').closest("label")
      ?.textContent.trim().startsWith("GPU type"), true);
  await mod.applyPartitionSelection("");
  rows = doc.querySelectorAll("#pendingJobsTable tbody tr");
  assert.equal(rows.length, 2); // back to every physical job once
  assert.match(doc.getElementById("pQueueMeta").textContent, /2 pending jobs/);
});

test("unavailable squeue shows the warning, never 'No pending jobs'", async (t) => {
  const { dom } = await boot({ queueAvailable: false, queue: {} }, 4);
  t.after(() => dom.window.close());
  const doc = dom.window.document;
  assert.equal(doc.getElementById("pQueueHint").hidden, false);
  assert.match(doc.querySelector("#partQueueTable tbody").textContent,
    /unavailable/i);
  // the waiting list carries the same unavailable state, not an empty queue
  assert.match(doc.querySelector("#pendingJobsTable tbody").textContent,
    /unavailable/i);
});
test("unavailable wait history shows its own hint, pending counts intact", async (t) => {
  const { dom } = await boot({
    queueAvailable: true,
    waitHistoryAvailable: false,
    queue: {
      "h200": { jobs: 1, gpus: 4, gpus_min: 4, started_jobs: 0, avg_wait_s: null },
      "__total__": { jobs: 1, gpus: 4, gpus_min: 4, started_jobs: 0, avg_wait_s: null },
    },
    waitingJobs: WAITING,
  }, 5);
  t.after(() => dom.window.close());
  const doc = dom.window.document;
  assert.equal(doc.getElementById("pQueueHint").hidden, true);
  assert.equal(doc.getElementById("pWaitHistoryHint").hidden, false);
  // the live queue itself is unaffected
  assert.match(doc.querySelector("#partQueueTable tbody").textContent, /h200/);
});

test("reachable-but-empty queue reads 'No pending jobs', no warning", async (t) => {
  const { dom } = await boot({ queueAvailable: true, queue: {} }, 6);
  t.after(() => dom.window.close());
  const doc = dom.window.document;
  assert.equal(doc.getElementById("pQueueHint").hidden, true);
  assert.match(doc.querySelector("#partQueueTable tbody").textContent,
    /No pending jobs\./);
});
