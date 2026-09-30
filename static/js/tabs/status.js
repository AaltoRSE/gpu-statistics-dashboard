/* Status tab: the cluster right now (core and GPU occupancy, running and
 * pending jobs, partitions) and its job throughput over the past day and
 * week — every job figure split into CPU jobs and GPU jobs.
 *
 * Two independent panels, fetched concurrently and rendered as each one
 * arrives: /api/status/live (scontrol + squeue, a few seconds) and
 * /api/status/history (seven days of sacct; slower when cold, so its chip
 * shows the day-chunk batch progress). A source that could not be read
 * renders as "unavailable" — never as a zero, an idle cluster or an empty
 * queue. The jobs-per-hour class toggle re-renders client-side, no fetch.
 *
 * See tabs/jobs.js for why this module and core/router.js import each
 * other. */
"use strict";

import { $ } from "../core/dom.js";
import {
  fmt, fmtInt, fmtDuration, html, raw, stateBadge, tsToDate,
} from "../core/format.js";
import {
  setResultsLoading, setResultsLoadingMessage, resetResultsLoadingMessage,
  showPanelError, panelOk,
} from "../core/panel.js";
import { batchText, pollProgress } from "../core/progress.js";
import { plotTheme, renderPlot } from "../core/plot.js";
import { currentTheme } from "../core/theme.js";
import { api } from "../core/api.js";
import { loaded } from "../core/router.js";
import { createTable } from "../core/table.js";

// Job-class identity colors, one pair per theme, each validated against
// that theme's panel surface (lightness band, chroma, CVD separation
// ΔE ≥ 8, contrast ≥ 3:1). The status colors (ok/warn/bad) stay reserved
// for outcomes, so a class never reads as good or bad.
const CLASS_COLORS = {
  dark: { cpu: "#2a66c8", gpu: "#9c7cf0" },
  light: { cpu: "#2a78d6", gpu: "#4a3aa7" },
};
const CLASS_LABEL = { cpu: "CPU jobs", gpu: "GPU jobs" };

// The backend's outcome groups (domain/status.py FAILED_STATES), for the
// final-state table's "counted as" column.
const FAILED_STATES = new Set([
  "FAILED", "OUT_OF_MEMORY", "TIMEOUT", "NODE_FAIL", "BOOT_FAIL", "DEADLINE",
]);

let liveData = null;   // the last /api/status/live payload
let histData = null;   // the last /api/status/history payload
let liveToken = 0;
let histToken = 0;
let inflight = 0;
let jobClass = "all";  // the jobs-per-hour toggle: all | cpu | gpu

function classColor(cls) {
  return CLASS_COLORS[currentTheme() === "light" ? "light" : "dark"][cls];
}

// The panel surface, for the 2px gaps between stacked fills.
function surface() {
  return currentTheme() === "light" ? "#ffffff" : "#171e2e";
}

function withAlpha(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  return "rgba(" + (n >> 16) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + alpha + ")";
}

// ---- loading ------------------------------------------------------------

function busy(delta) {
  inflight += delta;
  $("sRefresh").disabled = inflight > 0;
}

export function loadStatus(force = false) {
  return Promise.all([loadStatusLive(force), loadStatusHistory(force)]);
}

export async function loadStatusLive(force = false) {
  const token = ++liveToken;
  busy(1);
  setResultsLoading("statusLiveResults", true, "Loading cluster status…");
  try {
    const data = await api("/api/status/live" + (force ? "?refresh=true" : ""));
    if (token !== liveToken) return;
    panelOk("statusLiveResults");
    liveData = data;
    renderLive();
    loaded.status = true;
  } catch (e) {
    if (token === liveToken)
      showPanelError("statusLiveResults", e, () => loadStatusLive(), "the cluster status");
  } finally {
    busy(-1);
    if (token === liveToken) setResultsLoading("statusLiveResults", false);
  }
}

export async function loadStatusHistory(force = false) {
  const token = ++histToken;
  busy(1);
  setResultsLoading("statusHistoryResults", true, "Loading job history…");
  // A cold history reads a week of accounting as day chunks; the chip
  // shows which one it is on instead of an opaque spinner.
  const stopPolling = pollProgress("/api/status/history/progress",
    () => token === histToken,
    (prog) => setResultsLoadingMessage("statusHistoryResults", batchText(
      "Reading accounting records", prog.done, prog.total, prog.failed_batches)),
    () => resetResultsLoadingMessage("statusHistoryResults"));
  try {
    const data = await api("/api/status/history" + (force ? "?refresh=true" : ""));
    if (token !== histToken) return;
    panelOk("statusHistoryResults");
    histData = data;
    renderHistory();
  } catch (e) {
    if (token === histToken)
      showPanelError("statusHistoryResults", e, () => loadStatusHistory(), "the job history");
  } finally {
    stopPolling();
    busy(-1);
    if (token === histToken) setResultsLoading("statusHistoryResults", false);
  }
}

// ---- shared pieces ------------------------------------------------------

function classKey(cls) {
  return html`<span class="legend-key" style="background:${classColor(cls)}"></span>`;
}

// "■ CPU 708 · ■ GPU 303" — the class split under a tile's headline.
function splitLine(cpu, gpu, format = fmtInt, unit = "") {
  const tail = unit ? " " + unit : "";
  return html`${raw(classKey("cpu"))}CPU ${format(cpu)}${tail} &nbsp;·&nbsp; ${raw(classKey("gpu"))}GPU ${format(gpu)}${tail}`;
}

function tile(label, value, subs = [], title = "") {
  const lines = subs.filter(Boolean).map((s) => '<div class="stat-sub">' + s + "</div>").join("");
  return html`<div class="stat kpi" title="${title}"><div class="stat-label">${label}</div><div class="stat-value">${value}</div>${raw(lines)}</div>`;
}

function baseLayout(th) {
  return {
    margin: { l: 58, r: 16, t: 34, b: 40 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: th.font,
    // Plotly reverses stacked traces' legend by default; keep the trace
    // order so CPU always precedes GPU and segments read left to right.
    // Anchored to the container's top-left, not the plot area's (whose
    // left edge moves with the category labels), so a narrow card never
    // wraps the legend down onto the bars.
    legend: { orientation: "h", xref: "container", yref: "container",
              x: 0, y: 1, yanchor: "top", traceorder: "normal" },
  };
}

function emptyPlot(id, message) {
  const th = plotTheme();
  renderPlot(id, [], Object.assign(baseLayout(th), {
    xaxis: { visible: false }, yaxis: { visible: false },
    annotations: [{ text: message, xref: "paper", yref: "paper", x: 0.5, y: 0.5,
                    showarrow: false, font: { color: th.font.color, size: 12 } }],
  }));
}

function timeAxis(th) {
  return { type: "date", gridcolor: th.grid, hoverformat: "%a %d %b %H:%M" };
}

// ---- live panel -----------------------------------------------------------

function renderLive() {
  const d = liveData;
  const when = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/Helsinki", hour: "2-digit", minute: "2-digit",
    hour12: false, timeZoneName: "short",
  }).format(d.time * 1000);
  $("sMeta").textContent = "snapshot " + when;
  renderLiveTiles(d);
  const down = [];
  if (!d.nodes_available) down.push("node states (scontrol)");
  if (!d.jobs_available) down.push("running jobs (scontrol)");
  if (!d.queue_available) down.push("pending jobs (squeue)");
  const banner = $("sLiveUnavailable");
  banner.hidden = !down.length;
  banner.textContent = down.length
    ? "Unavailable right now: " + down.join(", ") + ". Those figures show as — "
      + "(unknown), not as zero; everything else is current."
    : "";
  renderResourcePlot();
  renderReasonPlot();
  const rows = (d.partitions || []).map((p) => ({
    ...p, cpu_pct: p.cpus_total ? (p.cpus_alloc / p.cpus_total) * 100 : null,
  }));
  partTable.setRows(rows);
  $("sPartCount").textContent = d.partitions ? String(rows.length) : "";
}

function renderLiveTiles(d) {
  const tiles = [];
  const run = d.running;
  if (d.cpu) {
    tiles.push(tile("Core occupancy", fmt(d.cpu.occupancy_pct) + "%", [
      html`${fmtInt(d.cpu.alloc)} of ${fmtInt(d.cpu.total)} cores allocated`,
      run ? splitLine(run.cpu.cpus, run.gpu.cpus, fmtInt, "cores") : "",
    ], "allocated / total cores over every node"));
    tiles.push(tile("GPU occupancy", fmt(d.gpu.occupancy_pct) + "%", [
      html`${fmtInt(d.gpu.alloc)} of ${fmtInt(d.gpu.total)} GPUs allocated`,
      d.gpu.unavailable ? html`${fmtInt(d.gpu.unavailable)} unavailable` : "",
    ], "allocated / total GPUs over every node"));
  } else {
    tiles.push(tile("Core occupancy", "—", ["unavailable (scontrol)"]));
    tiles.push(tile("GPU occupancy", "—", ["unavailable (scontrol)"]));
  }
  if (run) {
    tiles.push(tile("Running jobs", fmtInt(run.cpu.jobs + run.gpu.jobs),
      [splitLine(run.cpu.jobs, run.gpu.jobs)]));
  } else {
    tiles.push(tile("Running jobs", "—", ["unavailable (scontrol)"]));
  }
  const pend = d.pending;
  if (pend) {
    const eligible = pend.cpu.eligible_jobs + pend.gpu.eligible_jobs;
    const blocked = pend.cpu.blocked_jobs + pend.gpu.blocked_jobs;
    tiles.push(tile("Pending jobs", fmtInt(pend.cpu.jobs + pend.gpu.jobs), [
      splitLine(pend.cpu.jobs, pend.gpu.jobs),
      html`${fmtInt(eligible)} eligible · ${fmtInt(blocked)} blocked`,
      html`${fmtInt(pend.cpu.tasks + pend.gpu.tasks)} array tasks`,
    ], "a pending array counts as one job; array tasks is how many tasks those jobs hold"));
    const ages = ["cpu", "gpu"].filter((c) => pend[c].eligible_age_avg_s !== null);
    const weight = ages.reduce((n, c) => n + pend[c].eligible_jobs, 0);
    const mean = weight
      ? ages.reduce((n, c) => n + pend[c].eligible_age_avg_s * pend[c].eligible_jobs, 0) / weight
      : null;
    tiles.push(tile("Eligible jobs waiting for", fmtDuration(mean), [
      splitLine(pend.cpu.eligible_age_avg_s, pend.gpu.eligible_age_avg_s, fmtDuration),
      html`longest ${fmtDuration(Math.max(pend.cpu.eligible_age_max_s || 0,
                                          pend.gpu.eligible_age_max_s || 0) || null)}`,
    ], "mean time the eligible pending jobs have been queued so far (now − eligible time)"));
  } else {
    tiles.push(tile("Pending jobs", "—", ["unavailable (squeue) — not an empty queue"]));
  }
  if (d.nodes) {
    tiles.push(tile("Nodes unavailable", fmtInt(d.nodes.unavailable),
      [html`of ${fmtInt(d.nodes.total)} nodes`],
      "down, draining, failing or not responding"));
  }
  $("sLiveTiles").innerHTML = tiles.join("");
}

function renderResourcePlot() {
  const d = liveData;
  if (!d || !d.cpu) {
    emptyPlot("sResourcePlot", "Node snapshot unavailable (scontrol)");
    return;
  }
  const th = plotTheme();
  const rows = [["GPUs", d.gpu, "GPUs"], ["Cores", d.cpu, "cores"]];
  const segments = [
    ["Allocated", "alloc", th.acc],
    ["Idle", "idle", th.idle],
    ["Unavailable", "unavailable", th.warn],
  ];
  const traces = segments.map(([name, k, color]) => ({
    type: "bar", orientation: "h", name,
    y: rows.map((r) => r[0]),
    x: rows.map((r) => (r[1].total ? (r[1][k] / r[1].total) * 100 : 0)),
    customdata: rows.map((r) => [fmtInt(r[1][k]), r[2]]),
    marker: { color, line: { color: surface(), width: 2 } },
    text: k === "alloc" ? rows.map((r) => fmt(r[1].occupancy_pct) + "%") : undefined,
    textposition: "inside", insidetextanchor: "start",
    hovertemplate: "<b>%{y}</b> · " + name
      + ": %{customdata[0]} %{customdata[1]} (%{x:.1f}%)<extra></extra>",
  }));
  renderPlot("sResourcePlot", traces, Object.assign(baseLayout(th), {
    barmode: "stack", bargap: 0.35,
    margin: { l: 58, r: 16, t: 34, b: 30 },
    xaxis: { range: [0, 100], ticksuffix: "%", gridcolor: th.grid },
    yaxis: { gridcolor: th.grid },
  }));
}

function renderReasonPlot() {
  const d = liveData;
  if (!d || !d.pending_reasons) {
    emptyPlot("sReasonPlot",
      "Queue unavailable — squeue could not be read (not an empty queue)");
    return;
  }
  if (!d.pending_reasons.length) {
    emptyPlot("sReasonPlot", "No pending jobs");
    return;
  }
  const th = plotTheme();
  const top = d.pending_reasons.slice(0, 8);
  const rest = d.pending_reasons.slice(8);
  if (rest.length) {
    top.push({
      reason: "Other reasons",
      cpu_jobs: rest.reduce((n, r) => n + r.cpu_jobs, 0),
      gpu_jobs: rest.reduce((n, r) => n + r.gpu_jobs, 0),
    });
  }
  const rows = top.slice().reverse(); // largest on top
  const traces = ["cpu", "gpu"].map((cls) => ({
    type: "bar", orientation: "h", name: CLASS_LABEL[cls],
    y: rows.map((r) => r.reason), x: rows.map((r) => r[cls + "_jobs"]),
    marker: { color: classColor(cls), line: { color: surface(), width: 2 } },
    hovertemplate: "<b>%{y}</b> · " + CLASS_LABEL[cls] + ": %{x}<extra></extra>",
  }));
  const maxTotal = Math.max(...rows.map((r) => r.cpu_jobs + r.gpu_jobs));
  renderPlot("sReasonPlot", traces, Object.assign(baseLayout(th), {
    barmode: "stack", bargap: 0.3,
    margin: { l: 160, r: 16, t: 34, b: 30 },
    xaxis: { gridcolor: th.grid, range: [0, maxTotal * 1.15] },
    yaxis: { automargin: true },
    // Totals at the bar ends: the one number per row worth reading.
    annotations: rows.map((r) => ({
      x: r.cpu_jobs + r.gpu_jobs, y: r.reason, text: fmtInt(r.cpu_jobs + r.gpu_jobs),
      xanchor: "left", xshift: 4, showarrow: false,
      font: { size: 11, color: th.font.color },
    })),
  }));
}

// An occupancy share in the table's bar-cell shape, but neutral: pctBar's
// red/amber/green bands grade GPU utilization, and a lightly used
// partition is not a failure — status colors stay reserved for outcomes.
function shareBar(v) {
  if (v === null || v === undefined) return "";
  const pct = Math.max(0, Math.min(100, v)).toFixed(0);
  return html`<span class="pct-cell"><span class="bar-track"><span class="bar" style="width:${pct}%"></span></span><span class="pct-value">${pct}%</span></span>`;
}

function partRowHtml(p) {
  const gpus = p.gpus_total ? fmtInt(p.gpus_alloc) + "/" + fmtInt(p.gpus_total) : "—";
  return html`
    <tr>
      <td>${p.name}</td>
      <td>${p.kind === "gpu" ? "GPU" : "CPU"}</td>
      <td class="num">${fmtInt(p.nodes)}</td>
      <td class="num">${fmtInt(p.cpus_alloc)}/${fmtInt(p.cpus_total)}</td>
      <td>${raw(shareBar(p.cpu_pct))}</td>
      <td class="num">${gpus}</td>
      <td class="num">${fmtInt(p.running_jobs)}</td>
      <td class="num">${fmtInt(p.pending_jobs)}</td>
    </tr>`;
}

const partTable = createTable({
  el: $("statusPartTable"),
  columns: [
    { key: "name", type: "text" }, { key: "kind", type: "text" },
    { key: "nodes", type: "number" }, { key: "cpus_alloc", type: "number" },
    { key: "cpu_pct", type: "number" }, { key: "gpus_alloc", type: "number" },
    { key: "running_jobs", type: "number" }, { key: "pending_jobs", type: "number" },
  ],
  defaultSort: { key: "name", dir: "asc" },
  renderRow: partRowHtml,
  onRowClick: () => {},
  emptyMessage: () => ({
    text: liveData && !liveData.partitions
      ? "Node snapshot unavailable — scontrol could not be read."
      : "No partitions in this snapshot.",
    resetLabel: null,
  }),
});

// ---- history panel ----------------------------------------------------------

function renderHistory() {
  const d = histData;
  const banner = $("sHistCoverage");
  if (!d.available) {
    $("sHistBody").hidden = true;
    banner.hidden = false;
    banner.textContent = "Job history unavailable — sacct could not be read. "
      + "This is not an idle week; the live status above is unaffected.";
    return;
  }
  $("sHistBody").hidden = false;
  const cov = d.coverage;
  const notes = [];
  if (!cov.complete) {
    notes.push(cov.failed_chunks + " of " + cov.chunks + " days of accounting "
      + "data could not be read: their hours show as gaps and are missing "
      + "from the totals.");
  }
  if (!cov.running_verified) {
    notes.push("Running jobs could not be checked against the controller "
      + "(scontrol unavailable), so the held-resources charts may include "
      + fmtInt(cov.unverified_running_rows) + " stale accounting rows.");
  }
  if (!cov.blocked_pending_counted) {
    notes.push("The pending queue could not be read, so jobs submitted but "
      + "still blocked by a dependency or hold are missing from "
      + "\u201csubmitted\u201d.");
  }
  banner.hidden = !notes.length;
  banner.textContent = notes.join(" ");
  const w = d.windows;
  $("sHistMeta").textContent = "24 h from " + tsToDate(w.day.start)
    + " · 7 days from " + tsToDate(w.week.start) + " · to " + tsToDate(w.week.end);
  renderHistoryTiles(d.summary);
  renderSummaryTable(d.summary);
  renderStateTable(d.summary);
  renderHistoryPlots();
  renderHourlyTable();
}

function renderHistoryTiles(sum) {
  const day = sum.day, week = sum.week;
  const count = (label, win, k, title) => tile(label, fmtInt(win.all[k]),
    [splitLine(win.cpu[k], win.gpu[k])], title);
  $("sHistTiles").innerHTML = [
    count("Submitted, past 24 h", day, "submitted", "array tasks count individually"),
    count("Submitted, past 7 days", week, "submitted", "array tasks count individually"),
    count("Completed, past 24 h", day, "completed"),
    count("Failed, past 24 h", day, "failed",
      "failed, out of memory, timed out, node or boot failure, deadline"),
    count("Cancelled, past 24 h", day, "cancelled"),
    tile("Mean queue wait, past 24 h", fmtDuration(day.all.wait_avg_s), [
      splitLine(day.cpu.wait_avg_s, day.gpu.wait_avg_s, fmtDuration),
      html`median ${fmtDuration(day.all.wait_p50_s)}`,
    ], "Eligible → Start of the jobs that started in the past 24 h"),
  ].join("");
}

const SUMMARY_ROWS = [
  ["Submitted", "submitted", fmtInt],
  ["Started", "started", fmtInt],
  ["Completed", "completed", fmtInt],
  ["Failed", "failed", fmtInt],
  ["Cancelled", "cancelled", fmtInt],
  ["Other ended", "other", fmtInt],
  ["Mean queue wait", "wait_avg_s", fmtDuration],
  ["Median queue wait", "wait_p50_s", fmtDuration],
  ["P90 queue wait", "wait_p90_s", fmtDuration],
];

function renderSummaryTable(sum) {
  const cells = (win, k, format) => ["cpu", "gpu", "all"]
    .map((c) => html`<td class="num">${format(win[c][k])}</td>`).join("");
  $("statusSummaryTable").querySelector("tbody").innerHTML = SUMMARY_ROWS
    .map(([label, k, format]) => html`<tr><td class="metric">${label}</td>${raw(
      cells(sum.day, k, format) + cells(sum.week, k, format))}</tr>`)
    .join("");
}

function outcomeOf(state) {
  if (state === "COMPLETED") return "completed";
  if (state === "CANCELLED") return "cancelled";
  return FAILED_STATES.has(state) ? "failed" : "other";
}

function renderStateTable(sum) {
  const states = new Set();
  for (const win of [sum.day, sum.week])
    for (const c of ["cpu", "gpu"]) Object.keys(win[c].states).forEach((s) => states.add(s));
  const n = (win, c, s) => win[c].states[s] || 0;
  const rows = [...states].sort((a, b) =>
    (n(sum.week, "cpu", b) + n(sum.week, "gpu", b))
    - (n(sum.week, "cpu", a) + n(sum.week, "gpu", a)));
  const tbody = $("statusStateTable").querySelector("tbody");
  if (!rows.length) {
    tbody.innerHTML = '<tr class="empty-state-row"><td colspan="6"><div class="empty-state">'
      + "No jobs ended in the past 7 days.</div></td></tr>";
    return;
  }
  tbody.innerHTML = rows.map((s) => html`<tr>
      <td>${raw(stateBadge(s))}</td><td>${outcomeOf(s)}</td>
      <td class="num">${fmtInt(n(sum.day, "cpu", s))}</td>
      <td class="num">${fmtInt(n(sum.day, "gpu", s))}</td>
      <td class="num">${fmtInt(n(sum.week, "cpu", s))}</td>
      <td class="num">${fmtInt(n(sum.week, "gpu", s))}</td>
    </tr>`).join("");
}

// One series for the jobs-per-hour toggle: a class's own, or both summed
// (an hour no chunk covered stays null — a gap, never a zero).
function classSeries(k) {
  const s = histData.series;
  if (jobClass !== "all") return s[jobClass][k];
  return s.cpu[k].map((v, i) => (v === null ? null : v + s.gpu[k][i]));
}

function renderHistoryPlots() {
  if (!histData || !histData.available) return;
  renderJobsPlot();
  renderCoresPlot();
  renderGpusPlot();
  renderWaitPlot();
}

function renderJobsPlot() {
  const th = plotTheme();
  const x = histData.series.t.map(tsToDate);
  const outcomes = [
    ["completed", "Completed", th.ok],
    ["failed", "Failed", th.bad],
    ["cancelled", "Cancelled", th.warn],
    ["other", "Other", th.colors[10]],
  ];
  const traces = outcomes
    .map(([k, name, color]) => ({ k, name, color, y: classSeries(k) }))
    .filter((t) => t.k !== "other" || t.y.some((v) => v))
    .map(({ name, color, y }) => ({
      type: "bar", name, x, y, marker: { color },
      hovertemplate: name + ": %{y}<extra></extra>",
    }));
  traces.push({
    type: "scatter", mode: "lines", name: "Submitted", x, y: classSeries("submitted"),
    line: { color: th.font.color, width: 2 },
    hovertemplate: "Submitted: %{y}<extra></extra>",
  });
  renderPlot("sJobsPlot", traces, Object.assign(baseLayout(th), {
    barmode: "stack", bargap: 0.15, hovermode: "x unified",
    xaxis: timeAxis(th),
    yaxis: { title: "jobs per hour", gridcolor: th.grid, rangemode: "tozero" },
  }));
}

function capacityLine(th, value, label) {
  return {
    shapes: [{ type: "line", xref: "paper", x0: 0, x1: 1, y0: value, y1: value,
               line: { color: th.font.color, width: 1, dash: "dash" } }],
    annotations: [{ xref: "paper", x: 1, y: value, xanchor: "right", yanchor: "bottom",
                    text: label + " " + fmtInt(value), showarrow: false,
                    font: { size: 10, color: th.font.color } }],
  };
}

function renderCoresPlot() {
  const th = plotTheme();
  const s = histData.series;
  const x = s.t.map(tsToDate);
  const traces = ["cpu", "gpu"].map((cls) => ({
    type: "scatter", mode: "lines", name: CLASS_LABEL[cls], x, y: s[cls].cpus_alloc,
    stackgroup: "cores", line: { color: classColor(cls), width: 2 },
    fillcolor: withAlpha(classColor(cls), 0.35),
    hovertemplate: CLASS_LABEL[cls] + ": %{y:,.0f} cores<extra></extra>",
  }));
  const cap = histData.capacity;
  renderPlot("sCoresPlot", traces, Object.assign(baseLayout(th), {
    hovermode: "x unified",
    xaxis: timeAxis(th),
    yaxis: { title: "cores", gridcolor: th.grid, rangemode: "tozero",
             range: cap ? [0, cap.cpus * 1.08] : undefined },
  }, cap ? capacityLine(th, cap.cpus, "all cores") : {}));
}

function renderGpusPlot() {
  const th = plotTheme();
  const s = histData.series;
  const x = s.t.map(tsToDate);
  const color = classColor("gpu");
  const traces = [{
    type: "scatter", mode: "lines", name: "GPUs held", x, y: s.gpu.gpus_alloc,
    fill: "tozeroy", line: { color, width: 2 }, fillcolor: withAlpha(color, 0.35),
    hovertemplate: "GPUs held: %{y:,.1f}<extra></extra>",
  }];
  const cap = histData.capacity;
  renderPlot("sGpusPlot", traces, Object.assign(baseLayout(th), {
    hovermode: "x unified", showlegend: false,
    xaxis: timeAxis(th),
    yaxis: { title: "GPUs", gridcolor: th.grid, rangemode: "tozero",
             range: cap ? [0, cap.gpus * 1.08] : undefined },
  }, cap ? capacityLine(th, cap.gpus, "all GPUs") : {}));
}

function renderWaitPlot() {
  const th = plotTheme();
  const s = histData.series;
  const x = s.t.map(tsToDate);
  const traces = [];
  for (const cls of ["cpu", "gpu"]) {
    for (const [k, stat, dash] of [["wait_p50_s", "median", "solid"],
                                   ["wait_avg_s", "mean", "dot"]]) {
      const name = CLASS_LABEL[cls] + " · " + stat;
      traces.push({
        type: "scatter", mode: "lines", name, x,
        // A log axis has no zero: a 0 s median sits on the 1 s floor, and
        // the hover still reads the real value.
        y: s[cls][k].map((v) => (v === null ? null : Math.max(v, 1))),
        customdata: s[cls][k].map((v) => fmtDuration(v)),
        line: { color: classColor(cls), width: 2, dash },
        hovertemplate: name + ": %{customdata}<extra></extra>",
        // Four hourly lines over a week are noise; the means are one
        // legend click away (the tiles and table carry the mean anyway).
        visible: stat === "mean" ? "legendonly" : true,
      });
    }
  }
  renderPlot("sWaitPlot", traces, Object.assign(baseLayout(th), {
    hovermode: "x unified",
    xaxis: timeAxis(th),
    yaxis: {
      type: "log", gridcolor: th.grid, title: "queue wait",
      tickvals: [1, 60, 600, 3600, 4 * 3600, 86400, 7 * 86400],
      ticktext: ["1s", "1m", "10m", "1h", "4h", "1d", "7d"],
    },
  }));
}

function renderHourlyTable() {
  const s = histData.series;
  const ended = (c, i) => (s[c].completed[i] === null ? null
    : s[c].completed[i] + s[c].failed[i] + s[c].cancelled[i] + s[c].other[i]);
  const rows = s.t.map((ts, i) => html`<tr>
      <td>${tsToDate(ts)}</td>
      <td class="num">${fmtInt(s.cpu.submitted[i])}</td>
      <td class="num">${fmtInt(s.gpu.submitted[i])}</td>
      <td class="num">${fmtInt(ended("cpu", i))}</td>
      <td class="num">${fmtInt(ended("gpu", i))}</td>
      <td class="num">${fmtInt(s.cpu.failed[i])}</td>
      <td class="num">${fmtInt(s.gpu.failed[i])}</td>
      <td class="num">${fmtDuration(s.cpu.wait_p50_s[i])}</td>
      <td class="num">${fmtDuration(s.gpu.wait_p50_s[i])}</td>
      <td class="num">${fmtInt(s.cpu.cpus_alloc[i])}</td>
      <td class="num">${fmtInt(s.gpu.cpus_alloc[i])}</td>
      <td class="num">${fmt(s.gpu.gpus_alloc[i])}</td>
    </tr>`);
  $("statusHourlyTable").querySelector("tbody").innerHTML = rows.reverse().join("");
}

// Theme toggle: every chart and the class-colored tile keys re-render
// from the stored payloads (router.js rerenderAllPlots).
export function renderStatusPlots() {
  if (liveData) renderLive();
  if (histData) renderHistory();
}

// ---- controls ---------------------------------------------------------------

$("sRefresh").addEventListener("click", () => { loadStatus(true); });

$("sJobsClass").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-cls]");
  if (!btn) return;
  jobClass = btn.dataset.cls;
  $("sJobsClass").querySelectorAll("button").forEach((b) =>
    b.setAttribute("aria-pressed", String(b === btn)));
  if (histData && histData.available) renderJobsPlot();
});

$("sHourlyToggle").addEventListener("click", () => {
  const ex = $("sHourlyExplorer");
  const collapse = !ex.classList.contains("collapsed");
  ex.classList.toggle("collapsed", collapse);
  $("sHourlyToggle").setAttribute("aria-expanded", String(!collapse));
  $("sHourlyToggle").innerHTML = (collapse ? "&#9656; " : "&#9662; ") + "Hourly data";
});
