/* Jobs tab: job table, efficiency charts and the detail-first job view.
 *
 * Imports router.js for cross-tab navigation (openUser/openNode/
 * openPartition, setUrl) and the shared `loaded` flags; router.js imports
 * this module back for loadJobs/loadJobDetail/rendering (deep links, the
 * theme-toggle re-render sweep). This import cycle is intentional — every
 * use site is inside an event handler or async function, called only after
 * every module has finished evaluating, so the cycle never runs during
 * module initialization. */
"use strict";

import { $, debounce, isPlainClick } from "../core/dom.js";
import {
  fmt, fmtInt, pctBar, escapeHtml, tsToDate, stateBadge, compareStrings,
  userLink, nodeLinks, partitionLink,
} from "../core/format.js";
import { setResultsLoading, showPanelError, panelOk } from "../core/panel.js";
import { renderPlot, plotTheme, partBarColor } from "../core/plot.js";
import { api, status } from "../core/api.js";
import { loaded, setUrl, openUser, openNode, openPartition } from "../core/router.js";
import { createTable } from "../core/table.js";

let jobRows = [];
let jobBaseHigh = []; // server-calculated highest efficiency set, scoped by search
let jobBaseLow = [];
let jobVisibleRows = []; // searched rows after the client-side partition filter
let jobsToken = 0;

function jobLimit() {
  // The box is the only source of the historical fetch cap: validate it
  // strictly so the backend never sees an out-of-range or fractional value.
  const el = $("jLimit");
  const v = el.value.trim();
  const ok = v !== "" && Number.isInteger(Number(v)) &&
    Number(v) >= 1 && Number(v) <= 1000;
  if (!ok) {
    el.reportValidity();
    return null;
  }
  return Number(v);
}

export async function loadJobs(force = false) {
  const token = ++jobsToken;
  // A name/ID search is server-backed because it determines the highest
  // efficiency chart. The partition selector filters the returned rows locally.
  const params = new URLSearchParams({ since_hours: $("jWindow").value });
  if (force) params.set("refresh", "true");
  const search = $("jSearch").value.trim();
  if (search) params.set("search", search);
  const btn = $("jRefresh");
  if ($("jRunning").checked) {
    // Running mode returns every live GPU job: the limit box is disabled
    // and must not be sent.
    params.set("running_only", "true");
  } else {
    const limit = jobLimit();
    if (limit === null) {
      jobsToken = token - 1;
      return;
    }
    params.set("limit", String(limit));
  }
  if (btn) btn.disabled = true;
  setResultsLoading("jobsResults", true);
  setResultsLoading("jobEfficiencyResults", true);
  status("loading jobs…");
  try {
    const data = await api("/api/jobs?" + params);
    if (token !== jobsToken) return; // a newer request supersedes this one
    panelOk("jobsResults");
    panelOk("jobEfficiencyResults");
    jobRows = data.jobs;
    // The server computes highest/lowest efficiency from the searched rows.
    jobBaseHigh = data.efficiency_high || [];
    jobBaseLow = data.efficiency_low || [];
    // Preserve a compatible local partition selection across a search refresh.
    const selectedPartition = $("jPartition").value;
    $("jPartition").innerHTML =
      '<option value="">all</option>' +
      [...new Set(jobRows.map((j) => j.gpu_group || j.partition).filter(Boolean))].sort()
        .map((p) => '<option value="' + escapeHtml(p) + '">' + escapeHtml(p) + "</option>").join("");
    $("jPartition").value = [...$("jPartition").options]
      .some((option) => option.value === selectedPartition) ? selectedPartition : "";
    const w = data.window;
    $("jMetaCount").textContent = data.count + " jobs";
    $("jMeta").textContent = $("jRunning").checked
      ? "live jobs · instantaneous"
      : tsToDate(w.start) + " → " + tsToDate(w.end) + " UTC";
    renderJobEfficiency();
    renderJobsView();
    loaded.jobs = true;
  } catch (e) {
    if (token === jobsToken)
      showPanelError("jobsResults", e, () => loadJobs(), "the job list");
    throw e;
  } finally {
    if (token === jobsToken) {
      if (btn) btn.disabled = false;
      setResultsLoading("jobsResults", false);
      setResultsLoading("jobEfficiencyResults", false);
    }
  }
}

// Name/ID search is server-backed: it is sent to /api/jobs because it
// determines the efficiency chart's rows, so a search change reloads and
// refreshes the server-calculated extremes. The partition selector is a
// client-side filter that re-renders the table only and leaves the charts
// (the full base set's extremes) untouched.
export function renderJobsView() {
  const search = $("jSearch").value.trim().toLowerCase();
  const partition = $("jPartition").value;
  let rows = jobRows;
  if (partition) rows = rows.filter((j) => (j.gpu_group || j.partition) === partition);
  if (search) rows = rows.filter((j) =>
    j.jobid.includes(search) || (j.name || "").toLowerCase().includes(search));
  jobVisibleRows = rows;
  jobTable.setRows(jobVisibleRows);
  $("jCount").textContent = (partition || search)
    ? rows.length + " / " + jobRows.length + " shown"
    : rows.length + " shown";
}

function jobRowHtml(j) {
  const jobid = escapeHtml(j.jobid);
  const rawName = j.name || "";
  const start = escapeHtml((j.start || "").slice(0, 16));
  const gpus = escapeHtml(j.gpus !== undefined ? j.gpus : "—");
  return `
    <tr class="row" data-job="${jobid}">
      <td>${jobid}</td><td title="${escapeHtml(rawName)}">${escapeHtml(rawName.slice(0, 40))}</td>
      <td>${userLink(j.user)}</td><td>${partitionLink(j.gpu_group || j.partition)}</td>
      <td>${nodeLinks(j.nodes)}</td>
      <td>${stateBadge(j.state)}</td><td>${start}</td>
      <td class="num">${gpus}</td>
      <td class="num">${pctBar(j.mean_util)}</td>
      <td class="num">${escapeHtml(fmt(j.vram_avg))}</td>
    </tr>`;
}

function jobRowClick(e, tr) {
  const link = e.target.closest("a.userlink");
  if (link) {
    e.stopPropagation();
    if (!isPlainClick(e)) return;
    e.preventDefault();
    openUser(link.dataset.user);
    return;
  }
  const nlink = e.target.closest("a.nodelink");
  if (nlink) {
    e.stopPropagation();
    if (!isPlainClick(e)) return;
    e.preventDefault();
    openNode(nlink.dataset.node);
    return;
  }
  const plink = e.target.closest("a.partitionlink");
  if (plink) {
    e.stopPropagation();
    if (!isPlainClick(e)) return;
    e.preventDefault();
    openPartition(plink.dataset.partition);
    return;
  }
  loadJobDetail(tr.dataset.job, { kind: "jobs" });
}

function jobsEmptyMessage() {
  const hasFilters = $("jSearch").value.trim() !== "" || $("jPartition").value !== "";
  return {
    text: hasFilters
      ? "No jobs match the current search / partition filters."
      : "No jobs in this window.",
    resetLabel: hasFilters ? "reset filters" : null,
    onReset: () => {
      $("jSearch").value = "";
      $("jPartition").value = "";
      loadJobs();
    },
  };
}

const jobTable = createTable({
  el: $("jobTable"),
  columns: [
    { key: "jobid", type: "text" }, { key: "name", type: "text" },
    { key: "user", type: "text" }, { key: "partition", type: "text" },
    { key: "nodes", type: "text" }, { key: "state", type: "text" },
    { key: "start", type: "text" }, { key: "gpus", type: "number" },
    { key: "mean_util", type: "number" }, { key: "vram_avg", type: "number" },
  ],
  defaultSort: { key: "mean_util", dir: "desc" },
  renderRow: jobRowHtml,
  onRowClick: jobRowClick,
  emptyMessage: jobsEmptyMessage,
});

let effImpact = false;   // rank by effective GPU-hours instead of average efficiency
let effShowAll = false;  // show the full top-30 set instead of the default 10

// One horizontal efficiency bar chart. ``sortKey`` ranks the rows (and
// ``dir`` chooses which end is "first"); ``barKey`` sets the bar length so
// color and length encode the same quantity. Efficiency mode keeps them
// equal (efficiency). Impact mode makes the high chart rank and measure by
// effective GPU-hours, and reorders the low chart by GPU-hours while its
// bars still show average efficiency — the cheap-to-fix-inefficiency list
// then surfaces the jobs that burned the most capacity (no waste metric is
// available, so we never claim one).
function renderJobEffChart(elId, jobs, sortKey, barKey, dir) {
  const val = (j) => j[sortKey] || 0;
  let rows = jobs.slice().sort((a, b) => {
    const va = val(a), vb = val(b);
    return dir === "high" ? (vb - va) || compareStrings(a.jobid, b.jobid)
                          : (va - vb) || compareStrings(a.jobid, b.jobid);
  });
  if (!effShowAll) rows = rows.slice(0, 10);
  const th = plotTheme();
  const isGpuHours = barKey === "gpu_hours_eff";
  const layout = {
    margin: { l: 130, r: 20, t: 10, b: 30 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: th.font,
    xaxis: { title: isGpuHours ? "effective GPU-hours" : "average efficiency %",
      range: isGpuHours ? undefined : [0, 105], gridcolor: th.grid },
    yaxis: { autorange: "reversed", gridcolor: th.grid },
  };
  if (!rows.length) {
    layout.xaxis.showaxis = false;
    layout.yaxis.showaxis = false;
    layout.annotations = [{
      text: "No jobs match the current filters", showarrow: false,
      xref: "paper", yref: "paper", x: 0.5, y: 0.5,
      font: { color: th.font.color, size: 12 },
    }];
    renderPlot(elId, [{ type: "bar" }], layout);
    return;
  }
  const trace = {
    type: "bar", orientation: "h",
    y: rows.map((j) => j.jobid + " · " + j.user),
    x: rows.map((j) => j[barKey] || 0),
    customdata: rows.map((j) => [j.jobid, j.efficiency, j.gpu_hours_eff || 0,
      j.gpu_group || j.partition || "", j.gpu_type || ""]),
    marker: {
      color: rows.map((j) => partBarColor(j.efficiency)),
      line: { width: 0 },
    },
    hovertemplate: (isGpuHours
      ? "<b>%{y}</b><br>effective GPU-hours: %{x:.1f}<br>average efficiency: %{customdata[1]:.1f}%"
      : "<b>%{y}</b><br>average efficiency: %{x:.1f}%<br>effective GPU-hours: %{customdata[2]:.1f}") +
      "<br>partition: %{customdata[3]} · %{customdata[4]}<extra></extra>",
  };
  renderPlot(elId, [trace], layout).then((g) => {
    // Plotly.react keeps the graph div across renders: clear stale
    // handlers before rebinding or one click fires N detail loads.
    g.removeAllListeners("plotly_click");
    g.on("plotly_click", (ev) => {
      const id = ev.points[0].customdata[0];
      if (id) loadJobDetail(id, { kind: "jobs" });
    });
  });
}

export function renderJobEfficiency() {
  // High chart: efficiency mode ranks+measures by average efficiency;
  // impact mode ranks+measures by effective GPU-hours consumed.
  const highSort = effImpact ? "gpu_hours_eff" : "efficiency";
  const highBar = effImpact ? "gpu_hours_eff" : "efficiency";
  // Low chart: bars always show average efficiency; impact mode only
  // reorders it by effective GPU-hours (descending) so the top rows are the
  // inefficient jobs that consumed the most capacity.
  const lowSort = effImpact ? "gpu_hours_eff" : "efficiency";
  const lowDir = effImpact ? "high" : "low";
  renderJobEffChart("jobHighBarPlot", jobBaseHigh, highSort, highBar, "high");
  renderJobEffChart("jobLowBarPlot", jobBaseLow, lowSort, "efficiency", lowDir);
  $("jobHighTitle").textContent = effImpact
    ? "Highest effective GPU-hours" : "Highest average efficiency";
  $("jobLowTitle").textContent = effImpact
    ? "Lowest efficiency · most GPU-hours consumed" : "Lowest average efficiency";
  $("effShowAll").textContent = effShowAll
    ? "show top 10" : "show all " + (jobBaseHigh.length || 30);
  $("effLowNote").textContent = effImpact
    ? "reordered by effective GPU-hours consumed (bars = average efficiency)" : "";
}

let jobDetailFrom = null; // { node } when the job was opened from a node's Active jobs
let jobDetailOpenedFromTable = false;
let jobDetailWasCollapsed = false; // explorer visibility before the detail opened
let jobDetailToken = 0;
export let jobDetailData = null; // raw API payload; traces rebuild per theme

export async function loadJobDetail(jobid, from) {
  jobDetailFrom = from || null;
  jobDetailOpenedFromTable = !!(from && from.kind === "jobs");
  const token = ++jobDetailToken;
  const detail = $("jobDetailResults");
  const explorer = $("jobExplorer");
  if (detail.style.display === "none") {
    // Remember the explorer's visibility only on first open so a later
    // job switch restores the operator's prior layout.
    jobDetailWasCollapsed = explorer.classList.contains("collapsed");
  }
  setJobDetailHead(jobid);
  detail.style.display = "block";
  setResultsLoading("jobDetailResults", true);
  clearJobTableHighlight();
  // Detail-first: the requested job is the subject of this view, so tuck
  // the broad explorer behind its "Browse jobs" control unless the click
  // came from the table itself (where it is already the context).
  if (!jobDetailOpenedFromTable) setJobExplorerCollapsed(true);
  detail.scrollIntoView({ behavior: "smooth", block: "start" });
  try {
    const data = await api("/api/jobs/" + jobid + "?since_hours=" + $("jWindow").value);
    if (token !== jobDetailToken) return;
    panelOk("jobDetailResults");
    setUrl("/job/" + jobid);
    jobDetailData = data;
    renderJobDetail(data);
    if (jobDetailOpenedFromTable) highlightJobRow(jobid);
  } catch (e) {
    if (token === jobDetailToken)
      showPanelError("jobDetailResults", e, () => loadJobDetail(jobid, jobDetailFrom), "job " + jobid);
  } finally {
    if (token === jobDetailToken) setResultsLoading("jobDetailResults", false);
  }
}

function setJobDetailHead(jobid) {
  $("jobDetailTitle").textContent = "Job " + jobid;
  $("jobDetailMeta").textContent = "";
  const back = $("jobDetailBack");
  // Clear any stale origin from a previous job; the label depends on how
  // this job was opened.
  back.dataset.kind = "";
  back.dataset.node = "";
  back.dataset.user = "";
  back.dataset.partition = "";
  if (jobDetailFrom && jobDetailFrom.kind === "node") {
    back.dataset.kind = "node";
    back.dataset.node = jobDetailFrom.node;
    back.textContent = "← from node " + jobDetailFrom.node;
    back.title = "Return to node " + jobDetailFrom.node;
    back.style.display = "";
  } else if (jobDetailFrom && jobDetailFrom.kind === "user") {
    back.dataset.kind = "user";
    back.dataset.user = jobDetailFrom.user;
    back.textContent = "← from user " + jobDetailFrom.user;
    back.title = "Return to " + jobDetailFrom.user + "’s jobs";
    back.style.display = "";
  } else if (jobDetailFrom && jobDetailFrom.kind === "jobs") {
    back.dataset.kind = "jobs";
    back.textContent = "← back to jobs";
    back.title = "Return to job explorer";
    back.style.display = "";
  } else {
    // Unknown origin (deep link / direct). Left hidden until the metadata
    // loads; renderJobDetail offers the job's partition as a next step.
    back.style.display = "none";
  }
}

function closeJobDetail() {
  jobDetailData = null;
  jobDetailFrom = null;
  $("jobDetailResults").style.display = "none";
  clearJobTableHighlight();
  // Return the explorer to exactly how it was before the detail opened.
  setJobExplorerCollapsed(jobDetailWasCollapsed);
  setUrl(loaded.jobs && location.pathname.startsWith("/job/") ? "/jobs" : location.pathname);
}

function setJobExplorerCollapsed(collapse) {
  const ex = $("jobExplorer");
  ex.classList.toggle("collapsed", collapse);
  $("jobExplorerToggle").setAttribute("aria-expanded", String(!collapse));
  $("jobExplorerToggle").innerHTML = (collapse ? "&#9656; " : "&#9662; ") + "Browse jobs";
  $("jobExplorerNote").textContent = collapse
    ? "hidden while a job detail is open — expand to search and sort all jobs"
    : "";
}

function toggleJobExplorer() {
  setJobExplorerCollapsed(!$("jobExplorer").classList.contains("collapsed"));
}

function highlightJobRow(jobid) {
  clearJobTableHighlight();
  const tr = $("jobTable").querySelector('tr.row[data-job="' + jobid + '"]');
  if (tr) { tr.classList.add("sel"); tr.scrollIntoView({ block: "center" }); }
}

function clearJobTableHighlight() {
  const tb = $("jobTable").querySelector("tbody");
  if (tb) tb.querySelectorAll("tr.row.sel").forEach((t) => t.classList.remove("sel"));
}

export function renderJobDetail(data) {
  const m = data.metadata || {};
  const jobid = data.jobid;
  $("jobDetailTitle").textContent =
    "Job " + jobid + " — " + (m.name || "?") + " (" + (m.user || "?") + ") · " + (m.state || "?");
  const metaBits = [];
  if (m.partition) metaBits.push("partition " + m.partition);
  if (m.node_list) metaBits.push("nodes " + m.node_list);
  if (m.gpus) metaBits.push(m.gpus + "× " + (m.gpu_type || "gpu"));
  if (m.start) metaBits.push("start " + m.start);
  if (m.end) metaBits.push("end " + m.end);
  $("jobDetailMeta").textContent = metaBits.join(" · ");
  // No known origin (deep link / direct open): offer the job's partition as
  // a useful next investigation step, but only once metadata has loaded.
  const back = $("jobDetailBack");
  if (!back.dataset.kind && m.partition) {
    back.dataset.kind = "partition";
    back.dataset.partition = m.partition;
    back.textContent = "View partition " + m.partition;
    back.title = "Open the " + m.partition + " partition";
    back.style.display = "";
  }
  const th = plotTheme();
  const traces = [];
  // util and VRAM are both percentages, so they share one 0-100 axis. Each
  // GPU gets a solid util line and a dotted VRAM line in the same color, so
  // the pairing reads directly; unified hover reports every series at the
  // cursor's timestamp, and the legend toggles each line independently.
  const nUtil = data.series.utilization.length;
  const nVram = data.series.vram.length;
  data.series.utilization.forEach((s, i) => {
    const dev = s.metric.gpu !== undefined ? "GPU " + s.metric.gpu : "GPU " + (s.metric.instance || i);
    traces.push({
      type: "scatter", mode: "lines", name: dev + " util",
      x: s.values.map((v) => v[0] * 1000), y: s.values.map((v) => v[1]),
      line: { width: 2, color: th.colors[i % th.colors.length] },
    });
  });
  data.series.vram.forEach((s, i) => {
    const dev = s.metric.gpu !== undefined ? "GPU " + s.metric.gpu : "GPU " + (s.metric.instance || i);
    traces.push({
      type: "scatter", mode: "lines", name: dev + " vram",
      x: s.values.map((v) => v[0] * 1000), y: s.values.map((v) => v[1]),
      line: { width: 2, dash: "dot", color: th.colors[i % th.colors.length] },
    });
  });
  const detailLayout = {
    margin: { l: 46, r: 20, t: 10, b: 34 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: th.font,
    showlegend: nUtil + nVram > 0,
    legend: { orientation: "h", y: -0.15 },
    hovermode: "x unified",
    yaxis: { title: "%", range: [0, 105], gridcolor: th.grid },
    xaxis: { type: "date", gridcolor: th.grid },
  };
  renderPlot("jobDetailPlot", traces, detailLayout);
}

$("jWindow").addEventListener("change", () => {
  loadJobs();
  // A window change re-bases the detail's series; re-fetch an open job so it
  // never shows a stale window.
  if (jobDetailData && $("jobDetailResults").style.display !== "none") {
    loadJobDetail(jobDetailData.jobid, jobDetailFrom);
  }
});
$("jRunning").addEventListener("change", (e) => {
  $("jWindow").disabled = e.target.checked;
  $("jLimit").disabled = e.target.checked;
  loadJobs();
});
$("jLimit").addEventListener("change", loadJobs);
// Search refreshes the server-calculated highest-efficiency chart. Debounced
// so typing doesn't fire a request per keystroke; partition filtering is
// local because it only changes the table.
$("jSearch").addEventListener("input", debounce(loadJobs, 250));
$("jRefresh").addEventListener("click", () => { loadJobs(true); });
$("jPartition").addEventListener("change", renderJobsView);
$("jobDetailClose").addEventListener("click", closeJobDetail);
$("jobDetailBack").addEventListener("click", (e) => {
  e.preventDefault();
  const btn = e.currentTarget;
  const kind = btn.dataset.kind;
  closeJobDetail();
  if (kind === "node") openNode(btn.dataset.node);
  else if (kind === "user") openUser(btn.dataset.user);
  else if (kind === "partition") openPartition(btn.dataset.partition);
  // kind "jobs" (or empty): closing the detail returns to the explorer,
  // which is the source context.
});
$("jobExplorerToggle").addEventListener("click", () => toggleJobExplorer());
$("effImpact").addEventListener("change", (e) => {
  effImpact = e.target.checked;
  renderJobEfficiency();
});
$("effShowAll").addEventListener("click", (e) => {
  e.preventDefault();
  effShowAll = !effShowAll;
  renderJobEfficiency();
});
