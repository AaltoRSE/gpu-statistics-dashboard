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
  fmt, fmtInt, pctBar, escapeHtml, html, raw, tsToDate, fmtSacctTime, fmtDuration,
  stateBadge, userLink, nodeLinks, partitionLink,
} from "../core/format.js";
import { setResultsLoading, showPanelError, panelOk } from "../core/panel.js";
import { renderPlot, plotTheme, partBarColor } from "../core/plot.js";
import { api } from "../core/api.js";
import { loaded, setUrl, openUser, openNode, openPartition } from "../core/router.js";
import { createTable } from "../core/table.js";

let jobRows = [];
let jobEffHistogram = []; // server-computed GPU-hours-by-utilization-bucket, scoped by search
let jobVisibleRows = []; // searched rows after the client-side partition filter
let jobTotalCandidates = 0; // candidates before the "show top N" limit cut
let jobsToken = 0;

const JOB_LIMIT_DEFAULT = 100; // matches jLimit's value="100" in index.html

// PLAN-1 3.5: "show top N" persists across tab navigation with nothing on
// screen marking it as non-default — a badge on the tab itself (not just
// the controls row) surfaces that regardless of which tab is active.
function updateLimitBadge() {
  const badge = $("jobsLimitBadge");
  const active = !$("jRunning").checked && Number($("jLimit").value) !== JOB_LIMIT_DEFAULT;
  badge.hidden = !active;
  if (active) badge.textContent = $("jLimit").value;
}

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
  try {
    const data = await api("/api/jobs?" + params);
    if (token !== jobsToken) return; // a newer request supersedes this one
    panelOk("jobsResults");
    panelOk("jobEfficiencyResults");
    jobRows = data.jobs;
    jobTotalCandidates = data.total_candidates || 0;
    // The server computes the histogram from the searched rows.
    jobEffHistogram = data.efficiency_histogram || [];
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
      : tsToDate(w.start) + " → " + tsToDate(w.end);
    updateLimitBadge();
    renderJobEfficiency();
    renderJobsView();
    loaded.jobs = true;
  } catch (e) {
    if (token === jobsToken)
      showPanelError("jobsResults", e, () => loadJobs(), "the job list");
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
  const jobid = j.jobid;
  const rawName = j.name || "";
  const start = fmtSacctTime(j.start);
  const gpus = j.gpus !== undefined ? j.gpus : "—";
  return html`
    <tr class="row" data-job="${jobid}">
      <td>${jobid}</td><td class="name-cell" title="${rawName}">${rawName}</td>
      <td>${raw(userLink(j.user))}</td><td>${raw(partitionLink(j.gpu_group || j.partition))}</td>
      <td>${raw(nodeLinks(j.nodes))}</td>
      <td>${raw(stateBadge(j.state))}</td><td>${start}</td>
      <td class="num">${gpus}</td>
      <td class="num">${raw(pctBar(j.mean_util))}</td>
      <td class="num">${fmt(j.vram_avg)}</td>
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
  const search = $("jSearch").value.trim();
  const hasFilters = search !== "" || $("jPartition").value !== "";
  // Search is server-side and runs after the "Show top N by GPU-hours"
  // enrichment cap, so a search can miss a real job that simply falls
  // outside that cap — the UI must not let that look like "no such job in
  // this window." total_candidates (jobs matching the window/partition/user
  // filters before the cap) exceeding what was actually fetched is the
  // signal that happened; running-only ignores the limit entirely, so it
  // never gets this message even if search still finds nothing.
  if (search && jobRows.length === 0 && jobTotalCandidates > jobRows.length &&
      !$("jRunning").checked) {
    return {
      text: "No match in the top " + $("jLimit").value +
        " by GPU-hours — increase the limit to search further back.",
      resetLabel: "increase limit",
      onReset: () => {
        $("jLimit").value = String(Math.min(1000, jobTotalCandidates));
        updateLimitBadge();
        loadJobs();
      },
    };
  }
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

// One histogram of GPU-hours by utilization bucket (Phase 1.3): replaces
// the old twin "highest/lowest average efficiency" ranked-job charts, which
// degenerated on a small candidate set (a handful of jobs all near 100%
// left "lowest" empty) and couldn't answer "where is capacity being
// wasted" directly — only "which jobs", which said nothing about scale.
// A bucket is never a single job, so there is nothing sensible to click
// through to a job detail with.
export function renderJobEfficiency() {
  const buckets = jobEffHistogram;
  const totalHours = buckets.reduce((s, b) => s + b.gpu_hours, 0);
  // Collapse to one line of text at the panel's natural height instead of
  // a full-height chart with an invented axis (PLAN-1 2.3): an empty
  // Plotly trace still autoscales both axes to something like 0-4 with no
  // data behind it, which reads as a real (if oddly-scaled) chart, not as
  // "no data" — the table below this panel already says the same thing in
  // words, so this only needs to match that, not draw around it.
  const empty = !buckets.length || totalHours === 0;
  $("jobEffHistPlot").hidden = empty;
  $("jobEffHistEmpty").hidden = !empty;
  if (empty) return;
  const th = plotTheme();
  const layout = {
    margin: { l: 60, r: 20, t: 10, b: 40 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: th.font,
    xaxis: {
      title: "average efficiency %", range: [0, 100], gridcolor: th.grid,
      tickvals: buckets.map((b) => (b.bucket_start + b.bucket_end) / 2),
      ticktext: buckets.map((b) => b.bucket_start + "-" + b.bucket_end),
    },
    yaxis: { title: "GPU-hours", gridcolor: th.grid, rangemode: "tozero" },
    // Threshold markers (carried over from T-27): the 40/75% bands are
    // otherwise only legible from bar color, which is exactly the
    // red-vs-green distinction a colorblind operator can't rely on.
    shapes: [40, 75].map((x) => ({
      type: "line", x0: x, x1: x, xref: "x", yref: "paper", y0: 0, y1: 1,
      line: { color: th.grid, width: 1, dash: "dot" },
    })),
  };
  const trace = {
    type: "bar",
    x: buckets.map((b) => (b.bucket_start + b.bucket_end) / 2),
    y: buckets.map((b) => b.gpu_hours),
    width: buckets.map((b) => (b.bucket_end - b.bucket_start) * 0.9),
    customdata: buckets.map((b) => [b.bucket_start, b.bucket_end]),
    marker: {
      color: buckets.map((b) => partBarColor((b.bucket_start + b.bucket_end) / 2)),
      line: { width: 0 },
    },
    hovertemplate: "%{customdata[0]}-%{customdata[1]}%<br>" +
      "GPU-hours: %{y:.1f}<extra></extra>",
  };
  renderPlot("jobEffHistPlot", [trace], layout);
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
  try {
    const data = await api("/api/jobs/" + jobid + "?since_hours=" + $("jWindow").value);
    if (token !== jobDetailToken) return;
    panelOk("jobDetailResults");
    setUrl("/job/" + jobid);
    jobDetailData = data;
    renderJobDetail(data);
    if (jobDetailOpenedFromTable) highlightJobRow(jobid);
    // Land on the detail only once its real content (stats row, chart)
    // has rendered: scrolling before the fetch targets the loading
    // skeleton's height, and the panel's own size change on render then
    // pushes the view back down to the jobs table (PLAN-1 2.2's fix for
    // the Nodes tab, applied here). The row highlight is a plain marker
    // now — the single scroll is the panel's.
    detail.scrollIntoView({ behavior: "smooth", block: "start" });
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
  $("jobDetailStats").innerHTML = "";
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
  // No scrollIntoView here: the detail panel's own scroll (in
  // loadJobDetail, after its content renders) is the navigation target —
  // this is just a visual marker for whoever scrolls back up to the
  // table, not a second place to land (same as highlightNodeRow).
  if (tr) tr.classList.add("sel");
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
  if (m.start) metaBits.push("start " + fmtSacctTime(m.start));
  if (m.end) metaBits.push("end " + fmtSacctTime(m.end));
  $("jobDetailMeta").textContent = metaBits.join(" · ");
  // Summary row (PLAN-2): mean_util/gpu_hours_eff come straight from the
  // series for this job, always present; gpu_hours_alloc/elapsed_s are
  // null when metadata never resolved, in which case they're skipped
  // rather than shown as a misleading zero.
  const stats = [
    { label: "mean utilization", value: fmt(data.mean_util, 1) + "%" },
    { label: "effective GPU-hours", value: fmt(data.gpu_hours_eff, 1) },
  ];
  if (data.gpu_hours_alloc !== null && data.gpu_hours_alloc !== undefined) {
    stats.push({ label: "allocated GPU-hours", value: fmt(data.gpu_hours_alloc, 1) });
  }
  if (data.elapsed_s !== null && data.elapsed_s !== undefined) {
    stats.push({ label: "elapsed", value: fmtDuration(data.elapsed_s) });
  }
  $("jobDetailStats").innerHTML = stats.map((s) =>
    html`<div class="stat"><div class="stat-label">${s.label}</div><div class="stat-value">${s.value}</div></div>`
  ).join("");
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
  updateLimitBadge();
  loadJobs();
});
$("jLimit").addEventListener("change", loadJobs);
$("jLimit").addEventListener("input", updateLimitBadge);
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
