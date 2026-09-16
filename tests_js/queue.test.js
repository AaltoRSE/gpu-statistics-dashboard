// Functional tests for the Partitions tab's pending-jobs (queue status)
// table: the unavailable state must never read as "No pending jobs", the
// unknown-GPU disclosure must say "unknown" rather than a fabricated 0,
// and a reachable-but-empty queue must read "No pending jobs" with no
// warning. Node's own test runner + jsdom against the app's real
// index.html; global.setInterval is stubbed because core/panel.js starts
// its freshness timer at import time and node --test would never exit.
import assert from "node:assert/strict";
import { test, mock } from "node:test";
import { JSDOM } from "jsdom";
import { readFileSync } from "fs";
import { pathToFileURL } from "url";

const html = readFileSync(new URL("../static/index.html", import.meta.url), "utf8");

function boot({ queue, queueAvailable }, importCacheBust) {
  const dom = new JSDOM(html, { url: "http://localhost/partitions" });
  global.document = dom.window.document;
  global.window = dom.window;
  global.localStorage = dom.window.localStorage;
  global.location = dom.window.location;
  global.setInterval = mock.fn(() => 0); // panel.js's freshness timer
  global.Plotly = { newPlot: () => {}, react: () => {} };
  global.fetch = () => Promise.resolve({
    ok: true,
    json: () => Promise.resolve({
      window: { start: 1000, end: 2000 },
      step: 300,
      partitions: [{ name: "gpu-x", mean_util: 50, max_util: 60,
                     job_count: 1, gpus_alloc: 1, gpus_total: 8,
                     mean_occupancy: 10 }],
      trend: { "gpu-x": [[1000, 50]] },
      queue,
      queue_available: queueAvailable,
    }),
  });
  // cache-bust: each scenario gets a fresh module instance against its
  // own DOM (module-level table bindings would otherwise be frozen to
  // the first scenario's document).
  return import("../static/js/tabs/partitions.js?cb=" + importCacheBust)
    .then(async (mod) => {
      await mod.loadPartitions();
      return { dom };
    });
}

test("queue table renders grouped pending jobs with an unknown-GPU state", async () => {
  const { dom } = await boot({
    queueAvailable: true,
    queue: {
      "gpu-a": { jobs: 3, gpus: 12, gpus_min: 12 },
      "h200_3g.71gb": { jobs: 1, gpus: null, gpus_min: 4 },
    },
  }, 1);
  const doc = dom.window.document;
  const rows = doc.querySelectorAll("#partQueueTable tbody tr");
  assert.equal(rows.length, 2);
  const text = doc.querySelector("#partQueueTable tbody").textContent;
  assert.match(text, /gpu-a/);
  assert.match(text, /unknown/); // a null exact total is disclosed, not a 0
  assert.match(text, /12/);
  assert.equal(doc.getElementById("pQueueHint").hidden, true);
  assert.match(doc.getElementById("pQueueMeta").textContent, /4 pending jobs/);
});

test("unavailable squeue shows the warning, never 'No pending jobs'", async () => {
  const { dom } = await boot({ queueAvailable: false, queue: {} }, 2);
  const doc = dom.window.document;
  assert.equal(doc.getElementById("pQueueHint").hidden, false);
  assert.match(doc.querySelector("#partQueueTable tbody").textContent,
    /unavailable/i);
});

test("reachable-but-empty queue reads 'No pending jobs', no warning", async () => {
  const { dom } = await boot({ queueAvailable: true, queue: {} }, 3);
  const doc = dom.window.document;
  assert.equal(doc.getElementById("pQueueHint").hidden, true);
  assert.match(doc.querySelector("#partQueueTable tbody").textContent,
    /No pending jobs\./);
});
