// Functional tests for the Partitions tab's pending-jobs (queue status)
// tables: the unavailable state must never read as "No pending jobs" or
// as zeros, per-type rows classify exclusive/flexible/eligible demand,
// completed-job wait statistics render with null-vs-zero distinction,
// and a reachable-but-empty queue must read "No pending jobs" with no
// warning. Node's own test runner + jsdom against the app's real
// index.html; global.setInterval is stubbed because core/panel.js starts
// its freshness timer at import time and node --test would never exit.
//
// The queue lives on its own endpoint (/api/partitions/queue) so the
// Prometheus-backed charts render without waiting on squeue/sacct: these
// tests also pin that progressive behavior (independent overlays, token
// supersession, fresh per-window queue fetches) and the compacted queue
// tables (no Partition/Nodes columns; the waiting list behind a
// "Waiting jobs" disclosure).
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
    groups: ["h200"], wait_s: 5400, gpu_total: 4 },
  { jobid: "501", user: "bob", partition: "gpu-h200-mig,batch",
    state: "PENDING", submit: "2026-09-17T09:30:00",
    start: "2026-09-17T18:00:00", reason: "(Priority)",
    nodes: 1, gpus: 1, gpu_type: "",
    groups: ["h200", "h200_3g.71gb"], wait_s: 7200, gpu_total: 1 },
];

const TOTALS = { unique_pending_jobs: 4, unique_gpus_requested: 6 };

const CORE_BODY = {
  window: { start: 1000, end: 2000 },
  partitions: [{ name: "h200", mean_util: 50, max_util: 60,
                 job_count: 1, gpus_alloc: 1, gpus_total: 20,
                 mean_occupancy: 7.5 }],
  trend: { h200: [[1000, 50]] },
  step: 120,
};

const QUEUE = {
  "h200": {
    exclusive_jobs: 1, flexible_jobs: 1, eligible_jobs: 2,
    exclusive_gpus: 4, flexible_gpus: 1, eligible_gpus: 5,
    wait_p50_s: 3600, wait_p90_s: 3600, wait_avg_s: 3600,
    wait_samples: 2,
    wait_per_gpu_hour_weighted: 0.5,
  },
  "h200_3g.71gb": {
    exclusive_jobs: 0, flexible_jobs: 1, eligible_jobs: 1,
    exclusive_gpus: 0, flexible_gpus: 1, eligible_gpus: 1,
    wait_p50_s: null, wait_p90_s: null, wait_avg_s: null,
    wait_samples: 0,
    wait_per_gpu_hour_weighted: null,
  },
};

// boot: build the DOM, route the fetch stub by URL, import a fresh
// partitions module against it. Options:
//   core          body for /api/partitions (default CORE_BODY)
//   queue         body for /api/partitions/queue
//   totals        totals object inside that body
//   queueAvailable / waitHistoryAvailable  flags inside that body
//   waitingJobs   waiting_jobs array inside that body
//   gateQueue     unresolved promise placeholder for /api/partitions/queue
//                 (the returned handle resolves it via releaseQueue())
//   queueError    make /api/partitions/queue reject
//   coreError     make /api/partitions reject
async function boot(opts, importCacheBust) {
  const dom = new JSDOM(html, { url: "http://localhost/partitions" });
  global.document = dom.window.document;
  global.window = dom.window;
  global.localStorage = dom.window.localStorage;
  global.location = dom.window.location;
  // A minimal stub: setUrl only calls pushState, and binding jsdom's own
  // History object here keeps the runner's event loop alive after every
  // booted test (the leak that hung the suite).
  global.history = { pushState() {}, replaceState() {} };
  // Capture interval callbacks for opts.progress404 tests so they can
  // drive ticks manually (jsdom timers are stubbed to keep the runner
  // from hanging). Fresh arrays per boot: shared arrays would let a
  // later test inherit a prior boot's captured clear and pass the
  // circuit-breaker assertion without its own poller clearing anything.
  const intervals = [];
  const clearedIds = [];
  global.setInterval = opts.progress404
    ? (fn) => { intervals.push(fn); return intervals.length; }
    : () => 0;
  global.clearInterval = opts.progress404
    ? (id) => { clearedIds.push(id); }
    : () => {};
  global.Plotly = { newPlot: () => {}, react: () => {} };

  const queueBody = opts.queueError ? null : {
    queue: opts.queue || {},
    totals: opts.totals !== undefined ? opts.totals
      : (opts.queueAvailable === false
        ? { unique_pending_jobs: null, unique_gpus_requested: null }
        : TOTALS),
    queue_available: opts.queueAvailable !== false,
    waiting_jobs: opts.waitingJobs || [],
    wait_history_available: opts.waitHistoryAvailable !== false,
  };
  // The live snapshot: same pending shapes, wait statistics absent —
  // exactly what /api/partitions/queue/live returns. opt-driven:
  // liveQueue/liveTotals override the defaults derived from the full
  // queue body when a test wants the two responses to differ.
  const liveBody = opts.liveError ? null : {
    queue: opts.liveQueue !== undefined ? opts.liveQueue
      : (opts.queue || {}),
    totals: opts.liveTotals !== undefined ? opts.liveTotals
      : (opts.queueAvailable === false
        ? { unique_pending_jobs: null, unique_gpus_requested: null }
        : TOTALS),
    queue_available: opts.queueAvailable !== false,
    waiting_jobs: opts.waitingJobs || [],
  };
  const coreBody = opts.coreError ? null : { ...CORE_BODY };
  const urls = [];
  let releaseQueue = () => {};
  let releaseLive = () => {};

  global.fetch = (url) => {
    urls.push(String(url));
    if (String(url).startsWith("/api/partitions/queue/progress")) {
      // A stale backend without the route: 404, recorded for the
      // circuit-breaker test.
      return Promise.resolve({ ok: false, status: 404,
                               json: () => Promise.resolve({}) });
    }
    if (String(url).startsWith("/api/partitions/queue/live")) {
      // The live snapshot: pending figures WITHOUT wait statistics.
      if (opts.queueError) return Promise.reject(new Error("squeue down"));
      if (opts.gateLive) {
        return new Promise((resolve) => {
          releaseLive = () => resolve({
            ok: true, json: () => Promise.resolve(liveBody),
          });
        });
      }
      return Promise.resolve({
        ok: true, json: () => Promise.resolve(liveBody) });
    }
    if (String(url).startsWith("/api/partitions/queue")) {
      if (opts.queueError) return Promise.reject(new Error("squeue down"));
      if (opts.gateQueue) {
        return new Promise((resolve) => {
          releaseQueue = () => resolve({
            ok: true, json: () => Promise.resolve(queueBody),
          });
        });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve(queueBody) });
    }
    if (String(url).startsWith("/api/partitions")) {
      if (opts.coreError) return Promise.reject(new Error("prometheus down"));
      return Promise.resolve({ ok: true, json: () => Promise.resolve(coreBody) });
    }
    // VRAM endpoint: empty distribution.
    return Promise.resolve({ ok: true, json: () => Promise.resolve({
      window: { start: 1000, end: 2000 }, step: 120, total: 0, jobs: [],
    }) });
  };

  const mod = await import("../static/js/tabs/partitions.js?cb=" + importCacheBust);
  // Indirection: the gated fetch above REASSIGNS the closure-scoped
  // releaseQueue when the queue request is created — after this return
  // object was built. Expose a live trampoline, not a by-value snapshot,
  // or the gated-queue test would invoke the initial no-op forever.
  return { dom, mod, urls,
           releaseQueue: (...args) => releaseQueue(...args),
           releaseLive: (...args) => releaseLive(...args),
           intervals, clearedIds };
}

function bootAndWait(opts, bust) {
  return boot(opts, bust).then(async (ctx) => {
    await ctx.mod.loadPartitions();
    return ctx;
  });
}

test("queue table renders classification, wait stats, and unique headline", async (t) => {
  const { dom } = await bootAndWait({
    queue: QUEUE,
    waitingJobs: WAITING,
  }, 1);
  t.after(() => dom.window.close());
  const doc = dom.window.document;
  const rows = doc.querySelectorAll("#partQueueTable tbody tr");
  assert.equal(rows.length, 2);
  const text = doc.querySelector("#partQueueTable tbody").textContent;
  // per-type classification renders all six demand figures
  assert.match(text, /h200/);
  assert.match(text, /h200_3g\.71gb/);
  // wait statistics: real durations, null as —, zero samples as 0
  assert.match(text, /1h/);      // 3600s P50/P90/avg for h200
  assert.match(text, /—/);       // null percentiles for the MIG profile
  assert.match(text, /2/);       // h200 sample count
  // the size-normalized median renders with its unit; null stays an em dash
  assert.match(text, /0\.50 h\/GPU-h/);
  // the unique headline is the cluster-wide totals, prominent, above the table
  const unique = doc.getElementById("pQueueUnique");
  assert.match(unique.textContent, /4 unique pending jobs/);
  assert.match(unique.textContent, /requesting 6 GPUs/);
  assert.equal(doc.getElementById("pQueueHint").hidden, true);
  assert.equal(doc.getElementById("pWaitHistoryHint").hidden, true);
  // the exact Eligible jobs tooltip is present on the header
  const eligibleTh = [...doc.querySelectorAll("#partQueueTable th")]
    .find((h) => h.textContent.trim() === "Eligible jobs");
  assert.ok(eligibleTh, "Eligible jobs header exists");
  assert.equal(
    eligibleTh.getAttribute("title"),
    "Jobs that could run on this GPU type. Flexible jobs appear in "
    + "multiple GPU-type rows, so this column must not be summed.");
});

test("null wait stats (sacct failed) read as —, never as zeros", async (t) => {
  const { dom } = await bootAndWait({
    queue: {
      "h200": {
        exclusive_jobs: 1, flexible_jobs: 0, eligible_jobs: 1,
        exclusive_gpus: 4, flexible_gpus: 0, eligible_gpus: 4,
        wait_p50_s: null, wait_p90_s: null, wait_avg_s: null,
        wait_samples: null, wait_buckets: null,
      },
    },
    waitHistoryAvailable: false,
  }, 2);
  t.after(() => dom.window.close());
  const doc = dom.window.document;
  assert.equal(doc.getElementById("pWaitHistoryHint").hidden, false);
  const text = doc.querySelector("#partQueueTable tbody").textContent;
  // samples and buckets render the unavailable marker, not fabricated 0s
  assert.ok(!/<5m 0/.test(text), "buckets must not render as zero counts");
  const cells = [...doc.querySelectorAll("#partQueueTable tbody td")]
    .map((c) => c.textContent.trim());
  assert.ok(cells.includes("—"), cells);
});

test("null totals (squeue failed) read as unavailable in the headline", async (t) => {
  const { dom } = await bootAndWait({
    queueAvailable: false, queue: {}, totals: { unique_pending_jobs: null, unique_gpus_requested: null },
  }, 3);
  t.after(() => dom.window.close());
  const doc = dom.window.document;
  assert.equal(doc.getElementById("pQueueHint").hidden, false);
  assert.match(doc.querySelector("#partQueueTable tbody").textContent,
    /unavailable/i);
  assert.match(doc.getElementById("pQueueUnique").textContent,
    /unavailable/i);
});

test("waiting jobs list de-duplicates rows behind its disclosure", async (t) => {
  const { dom } = await bootAndWait({
    queue: QUEUE,
    waitingJobs: WAITING,
  }, 4);
  t.after(() => dom.window.close());
  const doc = dom.window.document;
  // collapsed by default: body hidden until the toggle is clicked
  const explorer = doc.getElementById("pWaitingExplorer");
  assert.ok(explorer.classList.contains("collapsed"));
  assert.equal(doc.getElementById("pWaitingExplorerToggle")
    .getAttribute("aria-expanded"), "false");
  // meta stays visible while collapsed
  assert.match(doc.getElementById("pWaitingMeta").textContent, /2 waiting/);

  doc.getElementById("pWaitingExplorerToggle").click();
  assert.ok(!explorer.classList.contains("collapsed"));
  assert.equal(doc.getElementById("pWaitingExplorerToggle")
    .getAttribute("aria-expanded"), "true");

  const rows = doc.querySelectorAll("#pendingJobsTable tbody tr");
  // job 501 can run on h200 AND the h200_3g.71gb profile but is listed
  // exactly once
  assert.equal(rows.length, 2);
  const text = doc.querySelector("#pendingJobsTable tbody").textContent;
  assert.match(text, /500/);
  assert.match(text, /501/);
  assert.match(text, /1h 30m/);   // wait_s 5400 (current queue age)
  assert.match(text, /2h/);       // wait_s 7200
  assert.match(text, /\(Resources\)/);
  assert.match(text, /\(Priority\)/);
  // estimated start renders as a reformatted sacct time; empty stays —
  assert.match(text, /2026-09-17 18:00/);
  // the GPU-type cell shows the job's eligible types
  const row501 = [...rows].find((r) => r.textContent.includes("501"));
  assert.match(row501.textContent, /h200, h200_3g\.71gb/);
  // Partition and Nodes columns were dropped from the waiting table
  const heads = [...doc.querySelectorAll("#pendingJobsTable th")]
    .map((h) => h.textContent.trim());
  assert.ok(!heads.includes("Partition"), heads);
  assert.ok(!heads.includes("Nodes"), heads);
  assert.equal(row501.querySelectorAll("a.partitionlink").length, 0);
  assert.ok(!row501.textContent.includes("gpu-h200-mig"));
  // clicking again re-collapses
  doc.getElementById("pWaitingExplorerToggle").click();
  assert.ok(explorer.classList.contains("collapsed"));
});

test("metrics render while the wait history is still pending", async (t) => {
  // The live snapshot (one squeue call) is NOT gated here — only the
  // FULL queue response (sacct wait history) hangs. The metrics panel
  // and the live pending rows both render before history resolves.
  const ctx = await boot({
    gateQueue: true,
    queue: QUEUE,
    waitingJobs: WAITING,
  }, 7);
  const dom = ctx.dom;
  const releaseQueue = ctx.releaseQueue;
  const urls = ctx.urls;
  t.after(() => dom.window.close());
  await ctx.mod.loadPartitions(); // core + live resolve; history gated
  // all three companions were requested together
  assert.ok(urls.some((u) => u.startsWith("/api/partitions?") ||
                          u === "/api/partitions"), urls);
  assert.ok(urls.some((u) => u.startsWith("/api/partitions/queue/live")), urls);
  assert.ok(urls.some((u) => u.startsWith("/api/partitions/queue?")), urls);
  const doc = dom.window.document;
  // metrics panel unblocks and renders as soon as its response lands
  const metricsPanel = doc.getElementById("partitionsResults");
  assert.equal(metricsPanel.classList.contains("loading"), false);
  assert.equal(metricsPanel.getAttribute("aria-busy"), "false");
  assert.ok(doc.querySelectorAll("#partTable tbody tr").length > 0);
  // the live queue rows are already rendered and NOT blurred: only the
  // history phase (the hint) is still in flight.
  const queuePanel = doc.getElementById("queueResults");
  assert.equal(queuePanel.classList.contains("loading"), false);
  assert.equal(queuePanel.getAttribute("aria-busy"), "false");
  assert.equal(doc.querySelectorAll("#partQueueTable tbody tr").length, 2);
  // release the full queue: the wait statistics merge into the rows
  releaseQueue();
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(doc.getElementById("pWaitHistoryHint").hidden, true);
  assert.equal(doc.querySelectorAll("#partQueueTable tbody tr").length, 2);
});

test("queue failures leave the metrics panel usable", async (t) => {
  const { dom } = await bootAndWait({ queueError: true }, 8);
  t.after(() => dom.window.close());
  // BOTH queue phases reject with queueError: live (awaited in
  // loadPartitionQueue's try) and history (held in historyPromise).
  // The history rejection must be consumed before the test ends, or
  // node flags an unhandled rejection after the test completed.
  await new Promise((r) => setTimeout(r, 20));
  const doc = dom.window.document;
  assert.equal(doc.getElementById("partitionsResults").classList.contains("loading"), false);
  assert.equal(doc.getElementById("queueResults").classList.contains("loading"), false);
  assert.match(doc.querySelector("#queueResults .panel-error").textContent,
    /Could not load the pending-jobs queue/i);
  // metrics content unaffected
  assert.ok(doc.querySelectorAll("#partTable tbody tr").length > 0);
});

test("a window change refetches both endpoints with the new window", async (t) => {
  const { dom, mod, urls } = await bootAndWait({
    queue: QUEUE,
    waitingJobs: WAITING,
  }, 9);
  t.after(() => dom.window.close());
  const doc = dom.window.document;
  urls.length = 0;
  doc.getElementById("pWindow").value = "72";
  doc.getElementById("pWindow").dispatchEvent(new dom.window.Event("change"));
  await new Promise((r) => setTimeout(r, 20));
  // Three companions fire per window change: the live snapshot (which
  // deliberately carries NO window — the snapshot is live by
  // construction), the full queue with the new window, and the core
  // metrics request with the new window.
  const liveUrls = urls.filter((u) => u.startsWith("/api/partitions/queue/live"));
  const fullQueueUrls = urls.filter((u) => u.startsWith("/api/partitions/queue?"));
  const coreUrls = urls.filter((u) => u.startsWith("/api/partitions?"));
  assert.equal(liveUrls.length, 1, urls);
  assert.equal(fullQueueUrls.length, 1, urls);
  assert.equal(coreUrls.length, 1, urls);
  assert.match(liveUrls[0], /^\/api\/partitions\/queue\/live$/);
  assert.match(fullQueueUrls[0], /since_hours=72/);
  assert.match(coreUrls[0], /since_hours=72/);
  // the replacement payload's rows render under the fresh fetch
  assert.ok(doc.querySelectorAll("#partQueueTable tbody tr").length >= 0);
  assert.ok(mod.selectedPartition !== undefined);
});

test("live rows render first, then wait-history progress lands on the hint", async (t) => {
  // The live snapshot resolves immediately (pending figures render,
  // overlay clears); only the sacct wait history stays in flight. Its
  // batch progress is non-blocking: it rewrites the history HINT, not
  // a blurred loading chip, because the table above it is already live.
  const ctx = await boot({
    gateQueue: true,
    queue: QUEUE,
    waitingJobs: WAITING,
  }, 12);
  const releaseQueue = ctx.releaseQueue;
  t.after(() => ctx.dom.window.close());
  await ctx.mod.loadPartitions(); // live renders; full queue stays gated
  const doc = ctx.dom.window.document;
  const panel = doc.getElementById("queueResults");
  // live rows are NOT blurred: the overlay is already cleared
  assert.equal(panel.classList.contains("loading"), false);
  assert.ok(doc.querySelectorAll("#partQueueTable tbody tr").length > 0);
  // the history hint is visible while the enrichment runs
  const hint = doc.getElementById("pWaitHistoryHint");
  assert.equal(hint.hidden, false);
  assert.match(hint.textContent, /Wait statistics loading/);
  // progress lands on the hint as plain text
  ctx.mod.setQueueProgress(3, 7, 1);
  assert.equal(hint.textContent,
    "Loading wait history: batch 3 of 7 (1 failed)…");
  assert.ok(!hint.textContent.includes("hellip"));
  // releasing the full queue resolves the history phase
  releaseQueue();
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(hint.hidden, true);
});

test("progress polling stops after a 404 from a stale backend", async (t) => {
  // A backend older than the progress route answers every poll with 404.
  // The poller must clear its own interval on the first 404 tick instead
  // of fetching once per second until the queue request lands.
  const ctx = await boot(
    { gateQueue: true, queue: QUEUE, progress404: true }, 14);
  const releaseQueue = ctx.releaseQueue;
  t.after(() => ctx.dom.window.close());
  await ctx.mod.loadPartitions(); // queue stays gated; poller registered
  // Drive one tick: the poller fetches, gets 404, clears itself.
  // Drive the LAST registered interval: the poller (the freshness timer
  // also registers an interval at import time).
  await ctx.intervals.at(-1)();
  assert.ok(ctx.clearedIds.length >= 1,
    "the 404 cleared the poll interval (no per-second request storm)");
  releaseQueue();
});

test("partial VRAM enrichment renders a visible coverage hint", async (t) => {
  const dom = new JSDOM(html, { url: "http://localhost/partitions" });
  global.document = dom.window.document;
  global.window = dom.window;
  global.localStorage = dom.window.localStorage;
  global.location = dom.window.location;
  global.history = { pushState() {}, replaceState() {} };
  global.setInterval = () => 0;
  global.Plotly = { newPlot: () => {}, react: () => {} };
  global.fetch = (url) => {
    if (String(url).startsWith("/api/partitions/vram")) {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({
        window: { start: 1000, end: 2000 }, step: 120, total: 4,
        enriched_frac: 0.5, failed_batches: 2,
        jobs: [{ jobid: "1", user: "ann", partition: "h200",
                 gpu_type: "h200", mean_util: 50, vram_gb: 1,
                 gpu_hours: 1, gpu_hours_eff: 0.5 }],
      }) });
    }
    if (String(url).startsWith("/api/partitions/queue")) {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({
        queue: {}, totals: TOTALS, queue_available: true, waiting_jobs: [],
        wait_history_available: true,
      }) });
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ ...CORE_BODY }) });
  };
  const mod = await import("../static/js/tabs/partitions.js?cb=16");
  t.after(() => dom.window.close());
  await mod.loadVram();
  const meta = dom.window.document.getElementById("vramMeta").textContent;
  assert.match(meta, /GPU-hour enrichment partial: 50% resolved/);
  assert.match(meta, /2 sacct batches failed/);
  assert.match(meta, /dashes mean accounting data is missing, not zero/);
});

test("selecting a GPU type filters the waiting list but keeps the unique headline", async (t) => {
  const { dom, mod } = await bootAndWait({
    queue: QUEUE,
    waitingJobs: WAITING,
  }, 5);
  t.after(() => dom.window.close());
  const doc = dom.window.document;
  doc.getElementById("pWaitingExplorerToggle").click(); // open the disclosure
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
  // the unique headline stays cluster-wide under a type filter — it must
  // NOT switch to the selected type's eligible count
  assert.match(doc.getElementById("pQueueUnique").textContent,
    /4 unique pending jobs/);
  assert.match(doc.getElementById("pQueueUnique").textContent,
    /requesting 6 GPUs/);
  // the selector label says GPU type, not partition
  assert.equal(
    doc.querySelector('label[for="pPartition"], #pPartition').closest("label")
      ?.textContent.trim().startsWith("GPU type"), true);
  await mod.applyPartitionSelection("");
  rows = doc.querySelectorAll("#pendingJobsTable tbody tr");
  assert.equal(rows.length, 2); // back to every physical job once
  // headline unchanged after clearing the filter
  assert.match(doc.getElementById("pQueueUnique").textContent,
    /4 unique pending jobs/);
  // the disclosure stayed open across the selection change
  assert.ok(!doc.getElementById("pWaitingExplorer").classList.contains("collapsed"));
});

test("unavailable squeue shows the warning, never 'No pending jobs'", async (t) => {
  const { dom } = await bootAndWait({
    queueAvailable: false, queue: {}, waitingJobs: [],
  }, 10);
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
  const { dom } = await bootAndWait({
    queueAvailable: true,
    waitHistoryAvailable: false,
    queue: {
      "h200": {
        exclusive_jobs: 1, flexible_jobs: 0, eligible_jobs: 1,
        exclusive_gpus: 4, flexible_gpus: 0, eligible_gpus: 4,
        wait_p50_s: null, wait_p90_s: null, wait_avg_s: null,
        wait_samples: null, wait_buckets: null,
      },
    },
    waitingJobs: WAITING,
  }, 13);
  t.after(() => dom.window.close());
  const doc = dom.window.document;
  assert.equal(doc.getElementById("pQueueHint").hidden, true);
  assert.equal(doc.getElementById("pWaitHistoryHint").hidden, false);
  // the live queue itself is unaffected
  assert.match(doc.querySelector("#partQueueTable tbody").textContent, /h200/);
  assert.match(doc.getElementById("pQueueUnique").textContent,
    /4 unique pending jobs/);
});

test("reachable-but-empty queue reads 'No pending jobs', no warning", async (t) => {
  const { dom } = await bootAndWait({ queueAvailable: true, queue: {} }, 15);
  t.after(() => dom.window.close());
  const doc = dom.window.document;
  assert.equal(doc.getElementById("pQueueHint").hidden, true);
  assert.match(doc.querySelector("#partQueueTable tbody").textContent,
    /No pending jobs\./);
});

test("gated VRAM fetch shows enrichment batch progress then completes", async (t) => {
  // VRAM polls its own progress identity with the SAME window/running/
  // partition parameters as its data request, rewrites the chip only
  // once a real batch total arrives, and stops polling when the data
  // response lands.
  const dom = new JSDOM(html, { url: "http://localhost/partitions" });
  global.document = dom.window.document;
  global.window = dom.window;
  global.localStorage = dom.window.localStorage;
  global.location = dom.window.location;
  global.history = { pushState() {}, replaceState() {} };
  const intervals = [];
  global.setInterval = (fn) => { intervals.push(fn); return intervals.length; };
  const cleared = [];
  global.clearInterval = (id) => { cleared.push(id); };
  global.Plotly = { newPlot: () => {}, react: () => {} };
  const urls = [];
  let releaseVram = () => {};
  global.fetch = (url) => {
    urls.push(String(url));
    if (String(url).startsWith("/api/partitions/vram/progress")) {
      return Promise.resolve({ ok: true, status: 200,
        json: () => Promise.resolve({ done: 2, total: 5,
                                      failed_batches: 0 }) });
    }
    if (String(url).startsWith("/api/partitions/vram")) {
      return new Promise((resolve) => {
        releaseVram = () => resolve({ ok: true,
          json: () => Promise.resolve({
            window: { start: 1000, end: 2000 }, step: 120,
            total: 0, enriched_frac: 1.0, failed_batches: 0, jobs: [],
          }) });
      });
    }
    if (String(url).startsWith("/api/partitions/queue/progress")) {
      return Promise.resolve({ ok: false, status: 404,
                               json: () => Promise.resolve({}) });
    }
    if (String(url).startsWith("/api/partitions/queue")) {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({
        queue: {}, totals: TOTALS, queue_available: true,
        waiting_jobs: [], wait_history_available: true }) });
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve({
      ...CORE_BODY }) });
  };
  const mod = await import("../static/js/tabs/partitions.js?cb=vram");
  t.after(() => dom.window.close());
  const doc = dom.window.document;
  // Start the gated fetch; its promise is released from inside the
  // assertions below, so await it through a driven sequence.
  const vramPromise = mod.loadVram();
  await new Promise((r) => setTimeout(r, 20));
  const chip = doc.querySelector("#vramResults .results-loading");
  assert.ok(chip, "the VRAM panel has its loading chip");
  assert.equal(chip.textContent, "Loading VRAM distribution…");
  // Drive one poll tick: batch state rewrites the chip as plain text.
  await intervals.at(-1)();
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(chip.textContent,
    "Loading VRAM distribution: batch 2 of 5…");
  assert.ok(!chip.textContent.includes("hellip"));
  // The poll carries the data request's exact identity.
  const vramData = urls.find((u) => u.startsWith("/api/partitions/vram?"));
  const vramProg = urls.find((u) => u.startsWith("/api/partitions/vram/progress?"));
  assert.ok(vramData && vramProg);
  assert.equal(vramProg.replace("/api/partitions/vram/progress", ""),
    vramData.replace("/api/partitions/vram", ""));
  // Completion stops the poller and clears the overlay.
  releaseVram();
  await vramPromise;
  assert.equal(doc.getElementById("vramResults").classList.contains("loading"),
    false);
  assert.ok(cleared.length >= 1, "the poll interval was cleared");
});

test("a late poll from a superseded queue request cannot overwrite the new label", async (t) => {
  // pollProgress gates every update on the request's token: after a
  // window change starts a NEW queue request (which resets the chip to
  // the honest initial label), an in-flight poll from the OLD request
  // must not paint its batch text over the reset.
  const dom = new JSDOM(html, { url: "http://localhost/partitions" });
  global.document = dom.window.document;
  global.window = dom.window;
  global.localStorage = dom.window.localStorage;
  global.location = dom.window.location;
  global.history = { pushState() {}, replaceState() {} };
  const intervals = [];
  global.clearInterval = () => {};
  global.Plotly = { newPlot: () => {}, react: () => {} };
  let queueGeneration = 0; // 0: gated old request, 1: new request answers
  // The core /api/partitions response stays GATED until after the old
  // queue poller is captured: an immediately resolved core request would
  // race ahead to loadVram() and register the VRAM poller, so a naive
  // "last interval" pick would grab the wrong poller (that exact bug is
  // why this test exists in this shape).
  let releaseCore;
  const coreGated = new Promise((resolve) => { releaseCore = resolve; });
  // Tag every poller with the URLs it fetches, by wrapping its body.
  const tagPoller = (fn) => async (...args) => {
    const seen = [];
    const taggedFetch = (url) => {
      seen.push(String(url));
      return fetch(url);
    };
    const realFetch = global.fetch;
    global.fetch = taggedFetch;
    try { return await fn(...args); } finally { global.fetch = realFetch; }
  };
  const registerPoller = (fn) => {
    const tagged = async (...args) => {
      const seen = [];
      const realFetch = global.fetch;
      global.fetch = (url) => {
        seen.push(String(url));
        return realFetch(url);
      };
      try { return await fn(...args); } finally { global.fetch = realFetch; }
      fn.seenUrls = seen;
    };
    intervals.push(tagged);
    return intervals.length;
  };
  global.setInterval = registerPoller;
  global.fetch = (url) => {
    const u = String(url);
    if (u.startsWith("/api/partitions/queue/progress")) {
      // The progress endpoint's state is keyed by since_hours, not by
      // request token, so EVERY poll sees the batch state — including
      // the old request's late poll after supersession. Only the token
      // predicate may stop it from painting over the new label; a stub
      // that went quiet here could hide a broken gate.
      return Promise.resolve({ ok: true, status: 200,
        json: () => Promise.resolve({ done: 4, total: 30,
                                      failed_batches: 0 }) });
    }
    if (u.startsWith("/api/partitions/queue")) {
      if (queueGeneration === 0) {
        return new Promise(() => {}); // the OLD requests never land
      }
      if (u.startsWith("/api/partitions/queue/live")) {
        // generation 1: the live snapshot answers immediately, so the
        // new request's rows render and its history phase is pending.
        return Promise.resolve({ ok: true, json: () => Promise.resolve({
          queue: {}, totals: TOTALS, queue_available: true,
          waiting_jobs: [] }) });
      }
      // The FULL queue response stays gated for the whole test: the
      // hint must still read "loading" when the stale poller fires.
      return new Promise(() => {});
    }
    if (u.startsWith("/api/partitions")) {
      return coreGated.then(() => ({ ok: true,
        json: () => Promise.resolve({ ...CORE_BODY }) }));
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve({
      window: { start: 1000, end: 2000 }, step: 120, total: 0,
      enriched_frac: 1.0, failed_batches: 0, jobs: [] }) });
  };
  const mod = await import("../static/js/tabs/partitions.js?cb=supersede");
  t.after(() => dom.window.close());
  const doc = dom.window.document;
  // Old request starts; its queue fetch hangs gated forever, and the
  // core request is held back so no VRAM poller can register yet.
  mod.loadPartitions();
  await new Promise((r) => setTimeout(r, 20));
  // Capture the OLD queue poller: it was registered by the gated
  // generation-0 request's progress polling.
  const oldPoller = intervals.at(-1);
  assert.ok(oldPoller, "the old queue request registered a progress poller");
  releaseCore();
  // A window change supersedes it: token bumps, the NEW requests
  // answer (generation 1). The new request's live rows render, its
  // history phase shows the loading hint.
  queueGeneration = 1;
  doc.getElementById("pWindow").value = "72";
  doc.getElementById("pWindow").dispatchEvent(
    new dom.window.Event("change"));
  await new Promise((r) => setTimeout(r, 20));
  const hint = doc.getElementById("pWaitHistoryHint");
  // the new request's live rows already rendered; the hint shows the
  // loading state of ITS history phase
  assert.match(hint.textContent, /Wait statistics loading/);
  // The old poller fires late: without the token gate it would paint
  // "batch 4 of 30" over the new request's hint.
  await oldPoller();
  await new Promise((r) => setTimeout(r, 20));
  assert.match(hint.textContent, /Wait statistics loading/);
});
