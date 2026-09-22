/* Partitions tab: utilization/occupancy bar charts, the trend chart, the
 * GPU-type table, the pending-job queue table and the VRAM-distribution
 * chart. The VRAM panel loads independently under its own results-panel
 * so a slow VRAM query never blocks the rest of the tab.
 *
 * See tabs/jobs.js for why this module and core/router.js import each
 * other. */
"use strict";

import { $, isPlainClick } from "../core/dom.js";
import { escapeHtml, fmt, fmtInt, pctBar, html, raw, compareStrings, tsToDate, partitionLink, fmtDuration, fmtSacctTime, jobLink, userLink } from "../core/format.js";
import { setResultsLoading, showPanelError, panelOk } from "../core/panel.js";
import { renderPlot, plotTheme, partBarColor } from "../core/plot.js";
import { api } from "../core/api.js";
import { loaded, setUrl, openPartition, openJob, openUser } from "../core/router.js";
import { createTable } from "../core/table.js";

let partRows = [];
let queueRows = [];
let queueTotals = null;  // the backend's unique cluster-wide pending figures
let queueAvailable = true;
let waitingJobs = [];    // raw waiting_jobs records from the API
let waitHistoryAvailable = true;
export let partTrendData = {};
let partTrendStep = 300; // seconds; set from the API response, used to size the smoothing window
export let selectedPartition = ""; // deep-linked or chosen GPU type; "" = all
let partitionsToken = 0;

export function setSelectedPartition(name) {
  selectedPartition = name || "";
}

function refreshSelectorOptions() {
  const sel = $("pPartition");
  // Union of utilization rows and queue group names: a pending-only GPU
  // type (no utilization series in this window) stays selectable.
  const names = [...new Set([
    ...partRows.map((p) => p.name),
    ...queueRows.map((q) => q.name),
  ].filter(Boolean))].sort(compareStrings);
  const options = ['<option value="">all GPU types</option>'];
  const listed = names.includes(selectedPartition);
  names.forEach((n) =>
    options.push('<option value="' + escapeHtml(n) + '">' + escapeHtml(n) + "</option>"));
  if (selectedPartition && !listed) {
    // A deep-linked type absent from the current window stays selected
    // so the URL keeps yielding the scoped (possibly empty) result.
    options.push('<option value="' + escapeHtml(selectedPartition) + '">' +
      escapeHtml(selectedPartition) + " (no data in window)</option>");
  }
  sel.innerHTML = options.join("");
  sel.value = selectedPartition;
  if (!sel.value) selectedPartition = "";
}
export function applyPartitionSelection(name) {
  selectedPartition = name || "";
  refreshSelectorOptions();
  const sel = $("pPartition");
  renderPartTrend(partTrendData);
  // Re-outline the selected type's bar in the two charts above the
  // trend (T-27) — previously only the trend/VRAM charts showed which
  // type was scoped.
  renderPartBar();
  renderPartOccupancy();
  renderWaitingJobs();
  renderPartQueue();
  setUrl(sel.value ? "/partition/" + encodeURIComponent(sel.value) : "/partitions");
}

export async function loadPartitions() {
  const token = ++partitionsToken;
  setResultsLoading("partitionsResults", true);
  // The queue is a separate, slower endpoint (squeue + sacct): start it
  // immediately so both requests are in flight together, and never let
  // its completion gate the Prometheus-backed charts below.
  loadPartitionQueue();
  let data;
  try {
    const params = new URLSearchParams({ since_hours: $("pWindow").value });
    if ($("pRunning").checked) params.set("running_only", "true");
    data = await api("/api/partitions?" + params);
  } catch (e) {
    if (token === partitionsToken) {
      setResultsLoading("partitionsResults", false);
      showPanelError("partitionsResults", e, loadPartitions, "the GPU type data");
    }
    return;
  }
  if (token !== partitionsToken) return; // a newer request supersedes this one
  panelOk("partitionsResults");
  partRows = data.partitions;
  const w = data.window;
  $("pCount").textContent = data.partitions.length + " GPU types · " +
    ($("pRunning").checked
      ? "live jobs · instantaneous"
      : tsToDate(w.start) + " → " + tsToDate(w.end));
  renderPartBar();
  renderPartOccupancy();
  partTrendData = data.trend;
  partTrendStep = data.step;
  applyPartitionSelection(selectedPartition);
  renderPartTable();
  loaded.partitions = true;
  // The summary panel is unblocked as soon as its response renders; the
  // VRAM distribution then fetches independently under its own panel.
  setResultsLoading("partitionsResults", false);
  if (token !== partitionsToken) return;
  await loadVram();
}

/* ---------------- Live queue (independent endpoint) ----------------
 * /api/partitions/queue carries the squeue snapshot plus the sacct
 * wait history. It is slower than the metrics endpoint, so it renders
 * under its own loading overlay whenever its response lands. */
let queueToken = 0;

async function loadPartitionQueue() {
  const token = ++queueToken;
  setResultsLoading("queueResults", true);
  const params = new URLSearchParams({ since_hours: $("pWindow").value });
  if ($("pRunning").checked) params.set("running_only", "true");
  // Poll the accounting progress endpoint while the queue request runs, so
  // the seven-day wait-history fetch shows real batch progress instead of
  // an opaque spinner. The poll stops when the queue response lands.
  const pollTimer = setInterval(async () => {
    try {
      const resp = await fetch("/api/partitions/queue/progress?" + params);
      if (resp.status === 404) {
        // A stale backend without the progress route: stop hammering it
        // every second — the queue request itself still decides the
        // panel's outcome.
        clearInterval(pollTimer);
        return;
      }
      if (!resp.ok) return;
      const prog = await resp.json();
      if (token === queueToken && prog && prog.total) {
        setQueueProgress(prog.done, prog.total, prog.failed_batches);
      }
    } catch (_) { /* progress is best-effort; the queue result decides */ }
  }, 1000);
  let data;
  try {
    data = await api("/api/partitions/queue?" + params);
  } catch (e) {
    clearInterval(pollTimer);
    if (token === queueToken) {
      setResultsLoading("queueResults", false);
      showPanelError("queueResults", e, loadPartitionQueue, "the pending-jobs queue");
    }
    return;
  }
  clearInterval(pollTimer);
  if (token !== queueToken) return; // a newer request supersedes this one
  panelOk("queueResults");
  const q = data.queue || {};
  queueTotals = data.totals || null;
  queueRows = Object.entries(q)
    .map(([name, g]) => Object.assign({ name }, g));
  queueAvailable = data.queue_available !== false;
  waitingJobs = data.waiting_jobs || [];
  waitHistoryAvailable = data.wait_history_available !== false;
  // Queue-only GPU types join the selector's union; the two queue
  // tables re-render under the current type filter. The metrics charts
  // are already rendered and are NOT redrawn here.
  refreshSelectorOptions();
  renderPartQueue();
  renderWaitingJobs();
  setResultsLoading("queueResults", false);
}

export function setQueueProgress(done, total, failed) {
  const panel = $("queueResults");
  const chip = panel.querySelector(".results-loading");
  if (!chip) return;
  chip.innerHTML = escapeHtml("Loading wait history: batch " + done + " of "
    + total + (failed ? " (" + failed + " failed)" : "") + "&hellip;");
}

function renderPartBar() {
  const th = plotTheme();
  const rows = partRows.slice().sort((a, b) => compareStrings(a.name, b.name));
  renderPlot("partBarPlot", [{
    type: "bar",
    x: rows.map((p) => p.name),
    y: rows.map((p) => p.mean_util),
    marker: {
      color: rows.map((p) => p.mean_util === null || p.mean_util === undefined
        ? th.idle : partBarColor(p.mean_util)),
      // Selecting a GPU type already scopes the trend and VRAM charts
      // below; outlining its bar here too (T-27) is the only place these
      // two charts show which type that is.
      line: {
        width: rows.map((p) => p.name === selectedPartition ? 2 : 0),
        color: th.font.color,
      },
    },
    hovertemplate: rows.map((p) => p.mean_util === null || p.mean_util === undefined
      ? "<b>%{x}</b><br>no utilization data<extra></extra>"
      : "<b>%{x}</b><br>mean %{y:.1f}%<extra></extra>"),
  }], {
    margin: { l: 46, r: 20, t: 10, b: 60 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: th.font,
    // Bar charts are read-only: no rectangle drag, pan, or axis zoom.
    yaxis: { title: "mean utilization %", range: [0, 105], gridcolor: th.grid, fixedrange: true },
    xaxis: { tickangle: -30, automargin: true, gridcolor: th.grid, fixedrange: true },
    dragmode: false,
  });
}

function renderPartOccupancy() {
  // Occupancy is a single neutral series: the bar HEIGHT is the signal, so
  // color must not double-encode a different metric (utilization). The
  // utilization value is surfaced in the tooltip instead.
  const th = plotTheme();
  const rows = partRows.slice().sort((a, b) => compareStrings(a.name, b.name));
  renderPlot("partOccupancyPlot", [{
    type: "bar",
    x: rows.map((p) => p.name),
    y: rows.map((p) => p.mean_occupancy),
    marker: {
      color: rows.map((p) => p.mean_occupancy === null
        || p.mean_occupancy === undefined ? th.idle : th.acc),
      line: {
        width: rows.map((p) => p.name === selectedPartition ? 2 : 0),
        color: th.font.color,
      },
    },
    customdata: rows.map((p) => p.mean_util),
    hovertemplate: rows.map((p) => p.mean_occupancy === null
      || p.mean_occupancy === undefined
      ? "<b>%{x}</b><br>no occupancy data<extra></extra>"
      : "<b>%{x}</b><br>occupancy %{y:.1f}%<br>mean util %{customdata:.1f}%<extra></extra>"),
  }], {
    margin: { l: 46, r: 20, t: 10, b: 60 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: th.font,
    yaxis: { title: "mean occupancy %", range: [0, 105], gridcolor: th.grid, fixedrange: true },
    xaxis: { tickangle: -30, automargin: true, gridcolor: th.grid, fixedrange: true },
    dragmode: false,
  });
}

function queueRowHtml(q) {
  return html`
    <tr class="row" data-partition="${q.name}">
      <td>${raw(partitionLink(q.name))}</td>
      <td class="num">${fmtInt(q.exclusive_jobs)}</td>
      <td class="num">${fmtInt(q.flexible_jobs)}</td>
      <td class="num">${fmtInt(q.eligible_jobs)}</td>
      <td class="num">${fmtInt(q.exclusive_gpus)}</td>
      <td class="num">${fmtInt(q.flexible_gpus)}</td>
      <td class="num">${fmtInt(q.eligible_gpus)}</td>
      <td class="num">${q.wait_p50_s === null || q.wait_p50_s === undefined
        ? "—" : fmtDuration(q.wait_p50_s)}</td>
      <td class="num">${q.wait_p90_s === null || q.wait_p90_s === undefined
        ? "—" : fmtDuration(q.wait_p90_s)}</td>
      <td class="num">${q.wait_avg_s === null || q.wait_avg_s === undefined
        ? "—" : fmtDuration(q.wait_avg_s)}</td>
      <td class="num">${q.wait_samples === null || q.wait_samples === undefined
        ? "—" : fmtInt(q.wait_samples)}</td>
      <td class="num">${q.wait_per_gpu_hour_weighted === null
        || q.wait_per_gpu_hour_weighted === undefined
        ? "—" : fmt(q.wait_per_gpu_hour_weighted, 2) + " h/GPU-h"}</td>
    </tr>`;
}
// Centered rolling mean over a fixed WALL-CLOCK window (not a fixed point
// count): the trend query's own step varies with the selected time range
// (120s at 24h, up to 900s at 7d — domain/common.py's step_for_range), so
// a fixed point count would smooth a 7-day view far more aggressively
// than a 24h one. Targeting ~1h of averaging either way keeps the amount
// of smoothing consistent across window choices (PLAN-1 3.8) — seven raw
// per-scrape series over a week were noisy enough to hide the shape of
// the week itself.
const TREND_SMOOTH_SECONDS = 3600;

function rollingMean(values, windowPoints) {
  if (windowPoints <= 1 || values.length <= windowPoints) return values;
  const half = Math.floor(windowPoints / 2);
  const out = new Array(values.length);
  for (let i = 0; i < values.length; i++) {
    const lo = Math.max(0, i - half);
    const hi = Math.min(values.length - 1, i + half);
    let sum = 0;
    for (let j = lo; j <= hi; j++) sum += values[j][1];
    out[i] = [values[i][0], sum / (hi - lo + 1)];
  }
  return out;
}

function renderPartTrend(trend) {
  const th = plotTheme();
  const entries = selectedPartition
    ? Object.entries(trend).filter(([name]) => name === selectedPartition)
    : Object.entries(trend);
  const emptyNames = entries
    .filter(([, values]) => !values || !values.length)
    .map(([name]) => name)
    .sort(compareStrings);
  const withData = entries.filter(([, values]) => values && values.length);
  // Rank by each GPU type's mean utilization (computed from the series
  // actually plotted) so the busiest types sit first and get the
  // stable leading colors; a plain insertion order would be arbitrary.
  const ranked = withData
    .map(([name, values]) => ({ name, values,
      mean: values.reduce((a, v) => a + v[1], 0) / values.length }))
    .sort((a, b) => b.mean - a.mean || compareStrings(a.name, b.name));
  const windowPoints = Math.max(1, Math.round(TREND_SMOOTH_SECONDS / (partTrendStep || 300)));
  const traces = ranked.map((r, i) => {
    const smoothed = rollingMean(r.values, windowPoints);
    return {
      type: "scatter", mode: "lines", name: r.name,
      x: smoothed.map((v) => v[0] * 1000), y: smoothed.map((v) => v[1]),
      line: { width: 1.5, color: th.colors[i % th.colors.length] },
    };
  });
  const layout = {
    margin: { l: 40, r: 20, t: 10, b: 30 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: th.font,
    showlegend: traces.length > 1,
    legend: { orientation: "h", y: -0.18 },
    yaxis: { title: "avg util %", range: [0, 105], gridcolor: th.grid },
    xaxis: { type: "date", gridcolor: th.grid },
  };
  if (!traces.length) {
    layout.xaxis.showaxis = false;
    layout.yaxis.showaxis = false;
    layout.annotations = [{
      text: selectedPartition
        ? "No trend data for " + selectedPartition + " in this window"
        : emptyNames.length
          ? "No trend data: " + emptyNames.join(", ")
          : "No GPU type trend data in this window",
      showarrow: false, xref: "paper", yref: "paper", x: 0.5, y: 0.5,
      font: { color: th.font.color, size: 12 },
    }];
  } else if (emptyNames.length) {
    layout.annotations = [{
      text: "No trend data: " + emptyNames.join(", "),
      showarrow: false, xref: "paper", yref: "paper", x: 0.5, y: 1.08,
      font: { color: th.font.color, size: 12 },
    }];
  }
  renderPlot("partTrendPlot", traces, layout);
}

function partRowHtml(p) {
  return html`
    <tr class="row" data-partition="${p.name}">
      <td>${raw(partitionLink(p.name))}</td>
      <td class="num" title="allocated / total GPUs">${p.gpus_alloc}/${p.gpus_total}</td>
      <td class="num">${fmtInt(p.job_count)}</td>
      <td class="num">${p.mean_util === null || p.mean_util === undefined
        ? "—" : raw(pctBar(p.mean_util))}</td>
    </tr>`;
}

function partRowClick(e, tr) {
  openPartition(tr.dataset.partition);
}

function partTableEmptyMessage() {
  return { text: "No GPU types in this window.", resetLabel: null };
}


const partTable = createTable({
  el: $("partTable"),
  columns: [
    { key: "name", type: "text" }, { key: "gpus_total", type: "number" },
    { key: "job_count", type: "number" }, { key: "mean_util", type: "number" },
  ],
  defaultSort: { key: "mean_util", dir: "desc" },
  renderRow: partRowHtml,
  onRowClick: partRowClick,
  emptyMessage: partTableEmptyMessage,
});

function renderPartTable() {
  partTable.setRows(partRows);
}



function queueRowClick(e, tr) {
  openPartition(tr.dataset.partition);
}

const partQueueTable = createTable({
  el: $("partQueueTable"),
  columns: [
    { key: "name", type: "text" },
    { key: "exclusive_jobs", type: "number" },
    { key: "flexible_jobs", type: "number" },
    { key: "eligible_jobs", type: "number" },
    { key: "exclusive_gpus", type: "number" },
    { key: "flexible_gpus", type: "number" },
    { key: "eligible_gpus", type: "number" },
    { key: "wait_p50_s", type: "number" },
    { key: "wait_p90_s", type: "number" },
    { key: "wait_avg_s", type: "number" },
    { key: "wait_samples", type: "number" },
    { key: "wait_per_gpu_hour_weighted", type: "number" },
  ],
  defaultSort: { key: "eligible_jobs", dir: "desc" },
  renderRow: queueRowHtml,
  onRowClick: queueRowClick,
  emptyMessage: () => ({ text: queueAvailable
    ? "No pending jobs."
    : "Queue status unavailable — squeue could not be reached (this is not an empty queue).",
    resetLabel: null }),
});

function renderPartQueue() {
  partQueueTable.setRows(queueRows);
  $("pQueueHint").hidden = queueAvailable;
  $("pWaitHistoryHint").hidden = waitHistoryAvailable;
  // The unique cluster-wide figure (totals from the backend) always
  // stays cluster-wide: per-type rows overlap (flexible jobs count in
  // each eligible row), so a type filter rewrites the waiting list
  // below — never this headline.
  const t = queueTotals || {};
  const jobs = t.unique_pending_jobs;
  const gpus = t.unique_gpus_requested;
  $("pQueueUnique").textContent =
    jobs === null || jobs === undefined
      ? "Unique pending jobs unavailable — squeue could not be reached."
      : fmtInt(jobs) + " unique pending job" + (jobs === 1 ? "" : "s") +
        (gpus === null || gpus === undefined
          ? "" : " requesting " + fmtInt(gpus) + " GPU" + (gpus === 1 ? "" : "s"));
}


/* -------------- Waiting jobs (the individual queue rows) --------------
 * Each GPU-eligible physical pending job once, filtered client-side by
 * the selected GPU type through its groups. Row/link opens the job
 * detail; user links keep their higher-priority navigation. The table
 * sits behind a "Waiting jobs" disclosure (the Browse-jobs pattern):
 * collapsed by default, toggled open, state kept across refreshes. */

function waitingRowHtml(j) {
  return html`
    <tr class="row" data-job="${j.jobid}">
      <td>${raw(jobLink(j.jobid))}</td>
      <td>${raw(userLink(j.user))}</td>
      <td>${(j.groups || []).join(", ")}</td>
      <td>${fmtSacctTime(j.submit)}</td>
      <td class="num">${j.wait_s === null || j.wait_s === undefined
        ? "—" : fmtDuration(j.wait_s)}</td>
      <td>${j.start ? fmtSacctTime(j.start) : "—"}</td>
      <td>${j.reason}</td>
      <td class="num">${j.gpu_total === null || j.gpu_total === undefined
        ? "—" : fmtInt(j.gpu_total)}</td>
    </tr>`;
}

function waitingRowClick(e, tr) {
  const jlink = e.target.closest("a.joblink");
  if (jlink) {
    e.stopPropagation();
    if (!isPlainClick(e)) return;
    e.preventDefault();
    openJob(jlink.dataset.job, { kind: "partitions" });
    return;
  }
  const ulink = e.target.closest("a.userlink");
  if (ulink) {
    e.stopPropagation();
    if (!isPlainClick(e)) return;
    e.preventDefault();
    openUser(ulink.dataset.user);
    return;
  }
  openJob(tr.dataset.job, { kind: "partitions" });
}

const pendingJobsTable = createTable({
  el: $("pendingJobsTable"),
  columns: [
    { key: "jobid", type: "text" }, { key: "user", type: "text" },
    { key: "groups", type: "text" },
    { key: "submit", type: "text" },
    { key: "wait_s", type: "number" }, { key: "start", type: "text" },
    { key: "reason", type: "text" },
    { key: "gpu_total", type: "number" },
  ],
  defaultSort: { key: "wait_s", dir: "desc" },
  renderRow: waitingRowHtml,
  onRowClick: waitingRowClick,
  emptyMessage: () => {
    if (!queueAvailable) {
      return { text: "Queue status unavailable — squeue could not be reached (this is not an empty queue).", resetLabel: null };
    }
    return selectedPartition
      ? { text: "No pending jobs for " + selectedPartition + ".", resetLabel: null }
      : { text: "No pending jobs.", resetLabel: null };
  },
});


function renderWaitingJobs() {
  const rows = selectedPartition
    ? waitingJobs.filter((j) => Array.isArray(j.groups) && j.groups.includes(selectedPartition))
    : waitingJobs;
  pendingJobsTable.setRows(rows);
  $("pWaitingMeta").textContent = selectedPartition
    ? rows.length + " waiting for " + selectedPartition
    : (rows.length ? rows.length + " waiting" : "");
}

function partControlsChanged() { loadPartitions(); }

/* ---- "Waiting jobs" disclosure (Browse-jobs pattern) ----
 * Collapsed by default; the toggle only flips a class + ARIA state, so
 * refreshes and GPU-type re-filters never lose the operator's choice. */

function setWaitingJobsCollapsed(collapse) {
  const ex = $("pWaitingExplorer");
  ex.classList.toggle("collapsed", collapse);
  $("pWaitingExplorerToggle").setAttribute("aria-expanded", String(!collapse));
  $("pWaitingExplorerToggle").innerHTML =
    (collapse ? "&#9656; " : "&#9662; ") + "Waiting jobs";
}

$("pWaitingExplorerToggle").addEventListener("click", () => {
  setWaitingJobsCollapsed(!$("pWaitingExplorer").classList.contains("collapsed"));
});
$("pWindow").addEventListener("change", partControlsChanged);
$("pPartition").addEventListener("change", () => {
  applyPartitionSelection($("pPartition").value);
  loadVram();
});
$("pRunning").addEventListener("change", (e) => {
  $("pWindow").disabled = e.target.checked;
  loadPartitions();
});

/* ---------------- VRAM distribution ----------------
 * VRAM usage of jobs in the window, binned by per-job peak VRAM and
 * weighted by allocated GPU-hours. The dual utilization-range slider
 * refilters client-side (no refetch); window / running-only refetch. */

export let vramJobs = [];
let vramTotal = 0; // candidates in the window, before the backend cap
let vramToken = 0;
let vramGpuType = "";
let vramEnrichedFrac = 1.0; // sacct enrichment coverage of the returned records
let vramFailedBatches = 0; // sacct batches that failed after retrying

export async function loadVram() {
  const token = ++vramToken;
  // The VRAM fetch blurs only the VRAM panel (vramResults), never the whole
  // partitions tab: window / running-only / GPU-type changes here must not
  // freeze the other graphs.
  const origin = partitionsToken;
  setResultsLoading("vramResults", true);
  try {
    const params = new URLSearchParams({ since_hours: $("pWindow").value });
    if ($("pRunning").checked) params.set("running_only", "true");
    if (selectedPartition) params.set("partition", selectedPartition);
    // The chart shows allocated vs effective directly; the backend weight
    // param (cap ordering) keeps its default.
    const data = await api("/api/partitions/vram?" + params);
    if (token !== vramToken) return; // a newer VRAM request supersedes this one
    panelOk("vramResults");
    vramJobs = data.jobs;
    vramTotal = data.total || data.jobs.length;
    vramEnrichedFrac = data.enriched_frac;
    vramFailedBatches = data.failed_batches || 0;
    fillVramGpuTypes();
    renderVram();
  } catch (e) {
    if (token === vramToken && origin === partitionsToken)
      showPanelError("vramResults", e, loadVram, "the VRAM distribution");
  } finally {
    if (token === vramToken && origin === partitionsToken)
      setResultsLoading("vramResults", false);
  }
}

function fillVramGpuTypes() {
  const sel = $("vGpuType");
  const prev = sel.value;
  const types = [...new Set(vramJobs.map((j) => j.gpu_type).filter(Boolean))]
    .sort(compareStrings);
  sel.innerHTML = '<option value="">all</option>' +
    types.map((t) => '<option value="' + escapeHtml(t) + '">' + escapeHtml(t) + '</option>').join("");
  if (types.includes(prev)) sel.value = prev;
  vramGpuType = sel.value;
}

function renderVram() {
  const lo = Math.min(+$("vUtilMin").value, +$("vUtilMax").value);
  const hi = Math.max(+$("vUtilMin").value, +$("vUtilMax").value);
  const matched = vramJobs.filter((j) =>
    j.mean_util >= lo && j.mean_util <= hi &&
    (!vramGpuType || j.gpu_type === vramGpuType));
  const binW = 16;
  const maxG = matched.length ? Math.max(...matched.map((j) => j.vram_gb)) : binW;
  const nBins = Math.max(1, Math.ceil(maxG / binW) || 1);
  // Each bar's total height is the bin's ALLOCATED GPU-hours, split into an
  // effective (green) baseline and an allocated-but-ineffective (blue) cap.
  // A record with no allocation row cannot contribute to an allocated-total
  // bar, so it is excluded from the chart and reported in the meta line.
  const allocOf = (j) => (j.gpu_hours == null ? null : j.gpu_hours);
  const effBins = new Array(nBins).fill(0);
  const remBins = new Array(nBins).fill(0);
  const perBin = new Array(nBins).fill(0);
  let excludedJobs = 0, excludedEff = 0, clamped = 0;
  matched.forEach((j) => {
    const a = allocOf(j);
    if (a == null) { excludedJobs++; excludedEff += j.gpu_hours_eff || 0; return; }
    const eff = Math.min(j.gpu_hours_eff || 0, a); // clamp: remainder stays >= 0
    if ((j.gpu_hours_eff || 0) > a) clamped++;
    const i = Math.min(Math.floor(j.vram_gb / binW), nBins - 1);
    effBins[i] += eff;
    remBins[i] += a - eff;
    perBin[i] += 1;
  });
  const totalAlloc = matched
    .filter((j) => allocOf(j) != null)
    .reduce((s, j) => s + j.gpu_hours, 0);
  const normalize = $("vNormalize").checked && totalAlloc > 0;
  const scale = (v) => (normalize ? (v / totalAlloc) * 100 : v);
  const labels = Array.from({ length: nBins }, (_, i) =>
    i * binW + "–" + (i + 1) * binW + " GB");
  const th = plotTheme();
  // One hover payload per bin, shared by both segments so either reports the
  // whole bin: label, allocated, effective, effective/allocated ratio, jobs.
  const customdata = labels.map((lab, i) => {
    const a = effBins[i] + remBins[i];
    return [lab, a, effBins[i], a > 0 ? effBins[i] / a : 0, perBin[i]];
  });
  const hover = "<b>%{customdata[0]}</b><br>Allocated: %{customdata[1]:,.1f} GPU-hours" +
    "<br>Effective: %{customdata[2]:,.1f} GPU-hours" +
    "<br>Effective / allocated: %{customdata[3]:.1%}<br>Jobs: %{customdata[4]}<extra></extra>";
  const traces = [
    { type: "bar", name: "Effective GPU-hours", x: labels, y: effBins.map(scale),
      marker: { color: th.colors[1] }, customdata, hovertemplate: hover },
    { type: "bar", name: "Allocated but ineffective GPU-hours", x: labels,
      y: remBins.map(scale), marker: { color: th.colors[0] }, customdata,
      hovertemplate: hover },
  ];
  const layout = {
    margin: { l: 60, r: 20, t: 30, b: 40 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: th.font,
    barmode: "stack",
    showlegend: true,
    legend: { orientation: "h", x: 0, y: 1.06, xanchor: "left", yanchor: "bottom" },
    // Bar distribution is read-only: no rectangle drag, pan, or axis zoom
    // (same treatment as the GPU-type bar charts).
    xaxis: { title: "VRAM usage (GB per GPU, peak over window)",
             gridcolor: th.grid, fixedrange: true },
    yaxis: { title: normalize ? "Share of matched allocated GPU-hours (%)"
                              : "Allocated GPU-hours",
             gridcolor: th.grid, fixedrange: true },
    dragmode: false,
  };
  const noAlloc = matched.length > 0 && perBin.every((c) => c === 0);
  if (!matched.length || noAlloc) {
    layout.xaxis.showaxis = false;
    layout.yaxis.showaxis = false;
    layout.annotations = [{
      text: noAlloc ? "No allocation data for the current filters"
                    : "No jobs match the current filters",
      showarrow: false, xref: "paper", yref: "paper", x: 0.5, y: 0.5,
      font: { color: th.font.color, size: 12 },
    }];
  }
  renderPlot("partVramPlot", traces, layout);
  const totalEff = matched
    .filter((j) => allocOf(j) != null)
    .reduce((s, j) => s + Math.min(j.gpu_hours_eff || 0, j.gpu_hours), 0);
  const truncated = vramTotal > vramJobs.length;
  const scopeBits = [
    truncated
      ? matched.length + " / " + vramJobs.length + " (top of " + vramTotal + ")"
      : matched.length + " jobs",
    selectedPartition,
    vramGpuType,
  ].filter(Boolean);
  const metaBits = [
    scopeBits.join(" · "),
    totalAlloc.toFixed(0) + " allocated GPU-hours",
    totalEff.toFixed(0) + " effective",
  ];
  if (normalize) metaBits.push(totalAlloc.toFixed(0) + " allocated in scope");
  if (vramEnrichedFrac < 1 || vramFailedBatches > 0) {
    const pct = Math.round((vramEnrichedFrac || 0) * 100);
    metaBits.push("GPU-hour enrichment partial: " + pct + "% resolved" +
      (vramFailedBatches ? "; " + vramFailedBatches + " sacct batch"
        + (vramFailedBatches === 1 ? "" : "es") + " failed" : "") +
      " (dashes mean accounting data is missing, not zero)");
  }
  if (excludedJobs)
    metaBits.push(excludedJobs + " jobs / " + excludedEff.toFixed(0) +
      " effective GPU-hours excluded — allocation unavailable");
  if (clamped)
    metaBits.push(clamped + " jobs have effective hours above allocated hours; effective share capped at allocated hours");
  metaBits.push("utilization " + lo + "–" + hi + "%");
  $("vramMeta").textContent = metaBits.join(" · ");
}

function vramSliderInput() {
  const min = $("vUtilMin"), max = $("vUtilMax");
  const lo = Math.min(+min.value, +max.value);
  const hi = Math.max(+min.value, +max.value);
  $("vUtilMinVal").textContent = lo;
  $("vUtilMaxVal").textContent = hi;
  const track = $("vTrack");
  track.style.left = lo + "%";
  track.style.width = (hi - lo) + "%";
  renderVram();
}

$("vNormalize").addEventListener("change", renderVram);
$("vGpuType").addEventListener("change", (e) => {
  vramGpuType = e.target.value;
  renderVram();
});
$("vUtilMin").addEventListener("input", vramSliderInput);
$("vUtilMax").addEventListener("input", vramSliderInput);

export function clearPartitionSelection() {
  if (!selectedPartition) return;
  selectedPartition = "";
  $("pPartition").value = "";
  renderPartTrend(partTrendData);
  renderPartBar();
  renderPartOccupancy();
  renderWaitingJobs();
  if (loaded.partitions) loadVram();
}

export { renderPartBar, renderPartOccupancy, renderPartTrend, renderVram };
