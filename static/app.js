/* Triton GPU Efficiency Dashboard — frontend logic.
 * All data is fetched on demand from the FastAPI backend (which queries
 * sacct / scontrol / Prometheus on demand, behind short TTL caches). No
 * client-side caching of data.
 */
"use strict";

const $ = (id) => document.getElementById(id);
const PLOT_CFG = { displayModeBar: false, responsive: true };
const COLORS = [
  "#4fc3f7", "#81c784", "#ffb74d", "#ba68c8", "#4dd0e1", "#f06292",
  "#aed581", "#7986cb", "#ffd54f", "#a1887f", "#90a4ae", "#e57373",
];

/* ---------------- theme ----------------
 * Dark is the default; light is a saved preference (localStorage) or the
 * OS-level preference. Toggling recolors the document and re-renders the
 * plots of every tab visited so far (plot colors are baked in at draw). */

const THEME_KEY = "gpu-dash-theme";
const THEME_LIGHT = {
  text: "#1c2436", dim: "#5a6a8a", grid: "#c9d3e3",
  colors: ["#005a9c", "#1b6e1b", "#a64900", "#6a1b9a", "#006064",
           "#9c1458", "#3d6518", "#283593", "#8d5b00", "#5d4037",
           "#37474f", "#a31515"],
};

function currentTheme() {
  const t = localStorage.getItem(THEME_KEY);
  return t === "light" || t === "dark"
    ? t
    : (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches)
      ? "light" : "dark";
}

function applyTheme(t) {
  if (t === "light") document.documentElement.dataset.theme = "light";
  else delete document.documentElement.dataset.theme;
  const btn = $("themeBtn");
  if (btn) btn.innerHTML =
    (t === "light" ? "&#9790;&#xFE0E; dark" : "&#9728;&#xFE0E; light");
}

function plotTheme() {
  const light = currentTheme() === "light";
  return {
    font: { color: light ? THEME_LIGHT.text : "#dce3f2", size: 11 },
    grid: light ? THEME_LIGHT.grid : "#2a3552",
    colors: light ? THEME_LIGHT.colors : COLORS,
    ok: light ? "#2e7d32" : "#66bb6a",
    warn: light ? "#ef6c00" : "#ffa726",
    bad: light ? "#d32f2f" : "#ef5350",
    acc: light ? "#0288d1" : "#4fc3f7",
    idle: light ? "#c9d3e3" : "#2a3552",
  };
}

function renderPlot(elId, traces, layout) {
  // Plotly.react diffs traces and layout, so the same call serves fresh
  // filter data and theme-only palette updates.
  return Plotly.react($(elId), traces, layout, PLOT_CFG);
}

function status(msg) { $("status").textContent = msg || ""; }

function errBox(show, msg) {
  const box = $("errBox");
  box.style.display = show ? "block" : "none";
  if (show) box.textContent = msg;
}

// Panel-local failure state: names the failed data source and offers a retry,
// keeping the last data visible (flagged stale) without depending on the
// global banner. The <div class="panel-error"> is appended to the panel's
// results-content so it sits with the data it belongs to.
const panelLoadedAt = {}; // resultsId -> ms timestamp of last successful load
function fmtClock(ts) {
  return new Date(ts).toLocaleTimeString("en-GB", { hour12: false });
}
function showPanelError(resultsId, err, reload, source) {
  const panel = $(resultsId);
  if (!panel) return;
  clearPanelError(resultsId);
  const box = document.createElement("div");
  box.className = "panel-error";
  box.innerHTML = "&#9888; " + escapeHtml(
    "Could not load " + (source || "data") + ": " +
    (err && err.message ? err.message : err));
  if (typeof reload === "function") {
    const btn = document.createElement("button");
    btn.textContent = "retry";
    btn.addEventListener("click", () => { clearPanelError(resultsId); reload(); });
    box.appendChild(btn);
  }
  const content = panel.querySelector(".results-content") || panel;
  content.appendChild(box);
  markStale(resultsId, true);
}

function clearPanelError(resultsId) {
  const panel = $(resultsId);
  if (!panel) return;
  panel.querySelectorAll(".panel-error").forEach((el) => el.remove());
  markStale(resultsId, false);
}

// A successful load: remember when it happened (for the stale timestamp) and
// drop any error/stale state. Call at each loader's success point.
function panelOk(resultsId) {
  panelLoadedAt[resultsId] = Date.now();
  clearPanelError(resultsId);
}

// Flag (or clear) the panel's data as stale. Only meaningful once a load has
// actually succeeded; a first-load failure shows just the error box (there is
// no prior data to be "stale"). The note carries the last-load timestamp so
// the operator knows how old the visible numbers are.
function markStale(resultsId, stale) {
  const panel = $(resultsId);
  if (!panel) return;
  panel.classList.toggle("stale", !!stale);
  const at = panelLoadedAt[resultsId];
  if (!stale || !at) return;
  let note = panel.querySelector(".stale-note");
  if (!note) {
    note = document.createElement("div");
    note.className = "stale-note";
    (panel.querySelector(".results-content") || panel).appendChild(note);
  }
  note.textContent = "Out of date \u2014 last loaded " + fmtClock(at) +
    ". The refresh failed, so this may not reflect the current state.";
}

async function api(path) {
  const t0 = performance.now();
  let resp;
  try {
    resp = await fetch(path);
  } catch (e) {
    errBox(true, "Backend unreachable: " + e);
    throw e;
  }
  if (!resp.ok) {
    let detail = resp.status + " " + resp.statusText;
    try { detail += " — " + JSON.stringify((await resp.json()).detail || ""); } catch (_) {}
    errBox(true, "API error: " + detail);
    throw new Error(detail);
  }
  errBox(false);
  const data = await resp.json();
  status("loaded in " + Math.round(performance.now() - t0) + " ms");
  return data;
}

function fmt(v, digits) {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  if (Number.isNaN(n)) return String(v);
  return n.toFixed(digits === undefined ? 1 : digits);
}

function fmtInt(v) {
  if (v === null || v === undefined) return "—";
  return Math.round(Number(v)).toLocaleString();
}

function pctBar(v) {
  if (v === null || v === undefined) return "";
  const p = Math.max(0, Math.min(100, v));
  const cls = p < 40 ? "lo" : p < 75 ? "mid" : "hi";
  return '<span class="pct-cell"><span class="bar-track">' +
    '<span class="bar ' + cls + '" style="width:' + p.toFixed(0) + '%"></span></span>' +
    '<span class="pct-value">' + p.toFixed(0) + "%</span></span>";
}

function stateBadge(state, label = state) {
  if (!state) return "";
  const known = ["RUNNING", "COMPLETED", "PENDING", "IDLE", "FAILED", "CANCELLED",
                 "TIMEOUT", "DRAIN", "DRAINED", "DOWN", "DRAINING", "RESERVED",
                 "MIXED", "ALLOCATED", "PLANNED", "NOT_RESPONDING"];
  const cls = known.includes(state) ? state : "PENDING";
  return '<span class="badge ' + cls + '">' + escapeHtml(label) + "</span>";
}

// scontrol node states carry qualifiers after ``+`` (``IDLE+DRAIN``) and a
// trailing ``*`` marks a node that is presently not responding; the star is
// stripped. Every qualifier is rendered as a badge (neutral allocation
// states included, per the column contract); the parsed drain reason is
// exposed as the cell title by the caller.
function nodeStateBadges(n) {
  const raw = (n.state_full || n.state || "").replace(/\*$/, "");
  const parts = raw.split("+")
    .map((p) => p.split(":")[0])
    .filter(Boolean);
  if (!parts.length) return "";
  return parts.map((p) => stateBadge(p, p.replace(/_/g, " "))).join(" ");
}

function tsToDate(ts) {
  return new Date(ts * 1000).toISOString().slice(0, 16).replace("T", " ");
}

// Trailing-edge debounce for high-frequency input events; the returned
// wrapper exposes .cancel() so a load can be cancelled mid-flight.
function debounce(fn, wait) {
  let t = null;
  const wrapped = (...args) => {
    clearTimeout(t);
    t = setTimeout(() => { t = null; fn(...args); }, wait);
  };
  wrapped.cancel = () => { clearTimeout(t); t = null; };
  return wrapped;
}

/* ---------------- health ---------------- */

async function checkHealth() {
  try {
    const h = await api("/api/health");
    $("health").innerHTML = '<b>&#9679;</b> prometheus: ' +
      escapeHtml(h.prometheus.replace(/^https?:\/\//, ""));
  } catch (_) {
    $("health").innerHTML = '<span style="color:var(--bad)">&#9679; backend down</span>';
  }
}

/* ---------------- tabs ---------------- */

const loaded = { jobs: false, partitions: false, users: false, nodes: false };
const nodeFilters = { search: "", gputype: "", busy: false, gpuOnly: true };

function showTab(name) {
  document.querySelectorAll("nav.tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tabpage").forEach((p) =>
    p.classList.toggle("active", p.id === "tab-" + name));
  let p = Promise.resolve();
  if (name === "jobs" && !loaded.jobs) p = loadJobs();
  if (name === "partitions" && !loaded.partitions) p = loadPartitions();
  if (name === "users" && !loaded.users) p = loadUsers();
  if (name === "nodes" && !loaded.nodes) p = loadNodes();
  window.dispatchEvent(new Event("resize")); // refit hidden plots
  return p;
}


/* ---------------- sort indicators ---------------- */

function markSort(tableEl, key, dir) {
  tableEl.querySelectorAll("th").forEach((th) => {
    th.classList.toggle("sorted-asc", th.dataset.k === key && dir === "asc");
    th.classList.toggle("sorted-desc", th.dataset.k === key && dir === "desc");
  });
}

// Natural (numeric-aware) string comparison: "gpu2" < "gpu3" < "gpu15" <
// "gpu28", unlike localeCompare's lexicographic "gpu2" < "gpu15" < "gpu3".
// Used by every column sort so node/partition names order the same way in
// all tabs.
function compareStrings(a, b) {
  a = String(a || "");
  b = String(b || "");
  const ra = a.split(/(\d+)/g);
  const rb = b.split(/(\d+)/g);
  const len = Math.max(ra.length, rb.length);
  for (let i = 0; i < len; i++) {
    const x = ra[i] || "", y = rb[i] || "";
    const nx = /^\d+$/.test(x) ? Number(x) : null;
    const ny = /^\d+$/.test(y) ? Number(y) : null;
    if (nx !== null && ny !== null) {
      if (nx !== ny) return nx - ny;
    } else if (nx !== null || ny !== null) {
      // Digit run vs non-digit run: shorter (prefix) side comes first.
      return x < y ? -1 : 1;
    } else if (x !== y) {
      return x.localeCompare(y);
    }
  }
  return a.length - b.length;
}

/* ---------------- jobs ---------------- */

let jobRows = [];
let jobBaseHigh = []; // server-calculated highest efficiency set, scoped by search
let jobBaseLow = [];
let jobVisibleRows = []; // searched rows after the client-side partition filter
let jobSort = { key: "mean_util", dir: "desc" };
let jobsToken = 0;

function setResultsLoading(resultsId, loading) {
  const el = $(resultsId);
  el.classList.toggle("loading", loading);
  el.setAttribute("aria-busy", loading ? "true" : "false");
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

async function loadJobs(force = false) {
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
    $("jMeta").textContent =
      tsToDate(w.start) + " → " + tsToDate(w.end) + " UTC";
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

function sortJobRows() {
  const rows = jobVisibleRows.slice();
  const k = jobSort.key, s = jobSort.dir === "asc" ? 1 : -1;
  const key = (v) => Array.isArray(v) ? v.join(",") : v;
  rows.sort((a, b) => {
    const va = key(a[k]), vb = key(b[k]);
    if (typeof va === "number" && typeof vb === "number") return (va - vb) * s;
    return compareStrings(va, vb) * s;
  });
  return rows;
}

// Name/ID search is server-backed: it is sent to /api/jobs because it
// determines the efficiency chart's rows, so a search change reloads and
// refreshes the server-calculated extremes. The partition selector is a
// client-side filter that re-renders the table only and leaves the charts
// (the full base set's extremes) untouched.
function renderJobsView() {
  const search = $("jSearch").value.trim().toLowerCase();
  const partition = $("jPartition").value;
  let rows = jobRows;
  if (partition) rows = rows.filter((j) => (j.gpu_group || j.partition) === partition);
  if (search) rows = rows.filter((j) =>
    j.jobid.includes(search) || (j.name || "").toLowerCase().includes(search));
  jobVisibleRows = rows;
  renderJobTable(sortJobRows());
  markSort($("jobTable"), jobSort.key, jobSort.dir);
  $("jCount").textContent = (partition || search)
    ? rows.length + " / " + jobRows.length + " shown"
    : rows.length + " shown";
}

// A filtered table with zero rows says what was filtered out and offers a
// reset, instead of a blank box that reads as "no data".
function emptyRow(cols, msg, resetLabel) {
  const btn = resetLabel ? ' <button data-empty-reset="1">' + escapeHtml(resetLabel) + '</button>' : '';
  return '<tr class="empty-state-row"><td colspan="' + cols + '">' +
    '<div class="empty-state">' + escapeHtml(msg) + btn + '</div></td></tr>';
}
function renderJobTable(rows) {
  const tb = $("jobTable").querySelector("tbody");
  const hasFilters = $("jSearch").value.trim() !== "" || $("jPartition").value !== "";
  if (!rows.length) {
    const msg = hasFilters
      ? "No jobs match the current search / partition filters."
      : "No jobs in this window.";
    tb.innerHTML = emptyRow(10, msg, hasFilters ? "reset filters" : null);
    const reset = tb.querySelector("button[data-empty-reset]");
    if (reset) reset.addEventListener("click", () => {
      $("jSearch").value = "";
      $("jPartition").value = "";
      loadJobs();
    });
    return;
  }
  tb.innerHTML = rows.map((j) => {
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
      <td class="num">${escapeHtml(j.efficiency !== undefined ? fmt(j.efficiency) : "—")}</td>
      <td class="num">${escapeHtml(fmt(j.vram_avg))}</td>
    </tr>`;
  }).join("");
  tb.querySelectorAll("tr.row").forEach((tr) =>
    tr.addEventListener("click", (e) => {
      const link = e.target.closest("a.userlink");
      if (link) { e.stopPropagation(); openUser(link.dataset.user); return; }
      const nlink = e.target.closest("a.nodelink");
      if (nlink) { e.stopPropagation(); openNode(nlink.dataset.node); return; }
      const plink = e.target.closest("a.partitionlink");
      if (plink) { e.stopPropagation(); openPartition(plink.dataset.partition); return; }
      loadJobDetail(tr.dataset.job, { table: true });
    }));
}

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
      if (id) loadJobDetail(id, { table: true });
    });
  });
}

function renderJobEfficiency() {
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
let jobDetailData = null; // raw API payload; traces rebuild per theme
async function loadJobDetail(jobid, from) {
  jobDetailFrom = from || null;
  jobDetailOpenedFromTable = !!(from && from.table);
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
  if (jobDetailFrom && jobDetailFrom.node) {
    back.style.display = "";
    back.textContent = "\u2190 from node " + jobDetailFrom.node;
    back.dataset.node = jobDetailFrom.node;
  } else {
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
    ? "hidden while a job detail is open \u2014 expand to search and sort all jobs"
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

function renderJobDetail(data) {
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
  const node = e.currentTarget.dataset.node;
  closeJobDetail();
  if (node) openNode(node);
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
$("jobTable").querySelectorAll("th[data-k]").forEach((th) =>
  th.addEventListener("click", () => {
    const k = th.dataset.k;
    jobSort = (k === jobSort.key)
      ? { key: k, dir: jobSort.dir === "desc" ? "asc" : "desc" }
      : { key: k, dir: "desc" };
    renderJobTable(sortJobRows());
    markSort($("jobTable"), jobSort.key, jobSort.dir);
  }));

/* ---------------- users ----------------
 * The user list is fetched once per window; the text box filters that
 * list locally as you type (no network). A selection is "finalized"
 * only on Enter or a table-row click — and only then is the selected
 * user's job list fetched (server-side user-scoped query). */

let userRows = [];
let userSelected = null;   // finalized user name or null
let userJobs = [];
let userSort = { key: "util_gpu_hours", dir: "desc" };
let usersToken = 0;
let userJobsToken = 0;

async function loadUsers() {
  const token = ++usersToken;
  setResultsLoading("usersResults", true);
  status("loading users…");
  try {
    const data = await api("/api/users?since_hours=" + $("uWindow").value);
    if (token !== usersToken) return;
    panelOk("usersResults");
    userRows = data.users;
    const w = data.window;
    $("uMetaCount").textContent = data.count + " users";
    $("uMeta").textContent =
      tsToDate(w.start) + " → " + tsToDate(w.end) + " UTC";
    renderUserTable();
    loaded.users = true;
  } catch (e) {
    if (token === usersToken)
      showPanelError("usersResults", e, loadUsers, "the user list");
    throw e;
  } finally {
    if (token === usersToken) setResultsLoading("usersResults", false);
  }
}

function filteredUsers() {
  const q = $("uSearch").value.trim().toLowerCase();
  return userRows.filter((u) => {
    if ($("uRunning").checked && !u.running_jobs) return false;
    if (q && !u.user.toLowerCase().includes(q)) return false;
    return true;
  });
}

function sortUserRows(rows) {
  const k = userSort.key, s = userSort.dir === "asc" ? 1 : -1;
  const key = (v) => Array.isArray(v) ? v.join(",") : (v === null ? "" : v);
  return rows.slice().sort((a, b) => {
    const va = key(a[k]), vb = key(b[k]);
    if (typeof va === "number" && typeof vb === "number") return (va - vb) * s;
    return compareStrings(va, vb) * s;
  });
}

function renderUserTable() {
  const tb = $("userTable").querySelector("tbody");
  const rows = sortUserRows(filteredUsers());
  if (!rows.length) {
    const searched = $("uSearch").value.trim();
    tb.innerHTML = emptyRow(7, searched
      ? "No users match that search." : "No users with GPU activity in this window.",
      searched ? "clear search" : null);
    const reset = tb.querySelector("button[data-empty-reset]");
    if (reset) reset.addEventListener("click", () => { $("uSearch").value = ""; renderUserTable(); });
    $("uCount").textContent = "0 shown";
    return;
  }
  tb.innerHTML = rows.map((u) => {
    const user = escapeHtml(u.user);
    return `
    <tr class="row" data-user="${user}" style="${u.user === userSelected ? "background:var(--panel2)" : ""}">
      <td><b>${user}</b></td>
      <td class="num">${escapeHtml(fmtInt(u.jobs))}</td>
      <td class="num">${escapeHtml(fmtInt(u.running_jobs))}</td>
      <td class="num">${pctBar(u.mean_util)}</td>
      <td class="num">${escapeHtml(fmtInt(u.util_gpu_hours))}</td>
      <td class="num">${escapeHtml(fmt(u.vram_avg))}</td>
      <td class="small">${escapeList(u.gpu_types)}</td>
    </tr>`;
  }).join("");
  tb.querySelectorAll("tr.row").forEach((tr) =>
    tr.addEventListener("click", () => {
      $("uSearch").value = tr.dataset.user;
      finalizeUser(tr.dataset.user);
    }));
  $("uCount").textContent = rows.length + " shown";
  markSort($("userTable"), userSort.key, userSort.dir);
}

// Make the finalized selection unmissable: a banner names the selected user
// and clears it, so "which user's jobs are below" never depends on row tint.
function renderUserSelectedBanner() {
  const banner = $("userSelectedBanner");
  if (!banner) return;
  banner.classList.toggle("on", !!userSelected);
  if (userSelected) $("userSelectedName").textContent = userSelected;
}

/* Finalize a selection: only this path fetches the user's jobs. An
 * empty finalized value deselects (hides the jobs panel). */
function finalizeUser(name) {
  name = (name || "").trim();
  if (!name) {
    userSelected = null;
    $("userJobsResults").style.display = "none";
    renderUserSelectedBanner();
    renderUserTable();
    setUrl("/users");
    return;
  }
  // Exact list match wins (case-insensitive); otherwise the raw text is
  // sent as-is — admins may type a user with no GPU activity in window.
  const hit = userRows.find((u) => u.user.toLowerCase() === name.toLowerCase());
  const finalName = hit ? hit.user : name;
  userSelected = finalName;
  $("uSearch").value = finalName;
  renderUserSelectedBanner();
  renderUserTable();
  loadUserJobs(finalName);
  setUrl("/user/" + encodeURIComponent(finalName));
}

async function loadUserJobs(user) {
  const token = ++userJobsToken;
  $("userJobsResults").style.display = "block";
  $("userJobsTitle").textContent =
    "Jobs · " + user + " · last " + $("uWindow").value / 24 + " d";
  setResultsLoading("userJobsResults", true);
  status("loading " + user + "’s jobs…");
  const params = new URLSearchParams({
    since_hours: $("uWindow").value, user, limit: "500",
  });
  if ($("uRunning").checked) params.set("running_only", "true");
  try {
    const data = await api("/api/jobs?" + params);
    if (token !== userJobsToken) return;
    panelOk("userJobsResults");
    userJobs = data.jobs;
    renderUserJobsTable();
  } catch (e) {
    if (token === userJobsToken)
      showPanelError("userJobsResults", e, () => loadUserJobs(user), "the job list");
  } finally {
    if (token === userJobsToken) setResultsLoading("userJobsResults", false);
  }
}

function renderUserJobsTable() {
  const tb = $("userJobsTable").querySelector("tbody");
  if (!userJobs.length) {
    tb.innerHTML = emptyRow(10,
      $("uRunning").checked
        ? "No running jobs for " + userSelected + " in this window."
        : "No jobs for " + userSelected + " in this window.", null);
    return;
  }
  tb.innerHTML = userJobs.map((j) => {
    const jobid = escapeHtml(j.jobid);
    const rawName = j.name || "";
    const start = escapeHtml((j.start || "").slice(0, 16));
    const gpus = escapeHtml(j.gpus !== undefined ? j.gpus : "—");
    return `
    <tr class="row" data-job="${jobid}">
      <td>${jobLink(j.jobid)}</td>
      <td title="${escapeHtml(rawName)}">${escapeHtml(rawName.slice(0, 40))}</td>
      <td>${partitionLink(j.gpu_group || j.partition)}</td>
      <td>${nodeLinks(j.nodes)}</td>
      <td>${stateBadge(j.state)}</td><td>${start}</td>
      <td class="num">${gpus}</td>
      <td class="num">${pctBar(j.mean_util)}</td>
      <td class="num">${escapeHtml(j.efficiency !== undefined ? fmt(j.efficiency) : "—")}</td>
      <td class="num">${escapeHtml(fmtInt(j.gpu_hours_eff))}</td>
    </tr>`;
  }).join("");
  tb.querySelectorAll("tr.row").forEach((tr) =>
    tr.addEventListener("click", (e) => {
      const link = e.target.closest("a.joblink");
      if (link) { e.stopPropagation(); openJob(link.dataset.job); return; }
      const nlink = e.target.closest("a.nodelink");
      if (nlink) { e.stopPropagation(); openNode(nlink.dataset.node); return; }
      const plink = e.target.closest("a.partitionlink");
      if (plink) { e.stopPropagation(); openPartition(plink.dataset.partition); return; }
      openJob(tr.dataset.job);
    }));
}

$("uWindow").addEventListener("change", () => {
  userSelected = null;
  $("userJobsResults").style.display = "none";
  loadUsers();
});
$("uRunning").addEventListener("change", () => {
  renderUserTable();
  if (userSelected) loadUserJobs(userSelected);
});
$("uRefresh").addEventListener("click", loadUsers);
$("userSelectedClear").addEventListener("click", () => finalizeUser(""));
$("uSearch").addEventListener("input", renderUserTable);
$("uSearch").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    finalizeUser($("uSearch").value);
  } else if (e.key === "Escape") {
    $("uSearch").value = "";
    finalizeUser("");
  }
});
$("userTable").querySelectorAll("th[data-k]").forEach((th) =>
  th.addEventListener("click", () => {
    const k = th.dataset.k;
    userSort = (k === userSort.key)
      ? { key: k, dir: userSort.dir === "desc" ? "asc" : "desc" }
      : { key: k, dir: "desc" };
    renderUserTable();
  }));

/* ---------------- partitions ---------------- */

let partRows = [];
let partTrendData = {};
let selectedPartition = ""; // deep-linked or chosen partition; "" = all
let partSort = { key: "mean_util", dir: "desc" };
let partitionsToken = 0;

function applyPartitionSelection(name) {
  selectedPartition = name || "";
  const sel = $("pPartition");
  const names = [...new Set(partRows.map((p) => p.name).filter(Boolean))].sort();
  const options = ['<option value="">all partitions</option>'];
  const listed = names.includes(selectedPartition);
  names.forEach((n) =>
    options.push('<option value="' + escapeHtml(n) + '">' + escapeHtml(n) + "</option>"));
  if (selectedPartition && !listed) {
    // A deep-linked partition absent from the current window stays selected
    // so the URL keeps yielding the scoped (possibly empty) result.
    options.push('<option value="' + escapeHtml(selectedPartition) + '">' +
      escapeHtml(selectedPartition) + " (no data in window)</option>");
  }
  sel.innerHTML = options.join("");
  sel.value = selectedPartition;
  if (!sel.value) selectedPartition = "";
  updatePartitionScope();
  renderPartTrend(partTrendData);
  setUrl(sel.value ? "/partition/" + encodeURIComponent(sel.value) : "/partitions");
}

// The partition selector scopes the trend and the VRAM distribution; say so
// in place so a scoped view is never mistaken for the whole cluster.
function updatePartitionScope() {
  const el = $("pScope");
  if (!el) return;
  el.innerHTML = selectedPartition
    ? "Scoped to \u201C" + escapeHtml(selectedPartition) + "\u201D: the trend and VRAM distribution below show only this partition (the table still lists all). " +
      '<button type="button" id="pScopeClear">clear scope</button>'
    : "";
  const b = el.querySelector("#pScopeClear");
  if (b) b.addEventListener("click", clearPartitionSelection);
}

async function loadPartitions() {
  const token = ++partitionsToken;
  setResultsLoading("partitionsResults", true);
  status("loading partitions…");
  let data;
  try {
    const params = new URLSearchParams({ since_hours: $("pWindow").value });
    if ($("pRunning").checked) params.set("running_only", "true");
    data = await api("/api/partitions?" + params);
  } catch (e) {
    if (token === partitionsToken) {
      setResultsLoading("partitionsResults", false);
      showPanelError("partitionsResults", e, loadPartitions, "the partition data");
    }
    throw e;
  }
  if (token !== partitionsToken) return; // a newer request supersedes this one
  panelOk("partitionsResults");
  partRows = data.partitions;
  const w = data.window;
  $("pCount").textContent = data.partitions.length + " partitions · " +
    tsToDate(w.start) + " → " + tsToDate(w.end) + " UTC";
  renderPartBar();
  renderPartOccupancy();
  partTrendData = data.trend;
  applyPartitionSelection(selectedPartition);
  renderPartTable();
  markSort($("partTable"), partSort.key, partSort.dir);
  loaded.partitions = true;
  // The summary panel is unblocked as soon as its response renders; the
  // VRAM distribution then fetches independently under its own panel.
  setResultsLoading("partitionsResults", false);
  if (token !== partitionsToken) return;
  await loadVram().catch(() => {});
}


function partBarColor(v) {
  const th = plotTheme();
  return v < 40 ? th.bad : v < 75 ? th.warn : th.ok;
}

function renderPartBar() {
  const th = plotTheme();
  const rows = partRows.slice().sort((a, b) => compareStrings(a.name, b.name));
  renderPlot("partBarPlot", [{
    type: "bar",
    x: rows.map((p) => p.name),
    y: rows.map((p) => p.mean_util),
    marker: { color: rows.map((p) => partBarColor(p.mean_util)) },
    hovertemplate: "<b>%{x}</b><br>mean %{y:.1f}%<extra></extra>",
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
    y: rows.map((p) => (p.mean_occupancy === null ? 0 : p.mean_occupancy)),
    marker: { color: th.acc },
    customdata: rows.map((p) => p.mean_util),
    hovertemplate: rows.map((p) => p.mean_occupancy === null
      ? "<b>%{x}</b><br>no occupancy data<br>mean util %{customdata:.1f}%<extra></extra>"
      : "<b>%{x}</b><br>mean occupancy %{y:.1f}%<br>mean util %{customdata:.1f}%<extra></extra>"),
  }], {
    margin: { l: 46, r: 20, t: 10, b: 60 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: th.font,
    yaxis: { title: "mean occupancy %", range: [0, 105], gridcolor: th.grid, fixedrange: true },
    xaxis: { tickangle: -30, automargin: true, gridcolor: th.grid, fixedrange: true },
    dragmode: false,
  });
}

function renderPartTrend(trend) {
  const th = plotTheme();
  const entries = selectedPartition
    ? Object.entries(trend).filter(([name]) => name === selectedPartition)
    : Object.entries(trend);
  const withData = entries.filter(([, values]) => values && values.length);
  // Rank by each partition's mean utilization (computed from the series
  // actually plotted) so the busiest partitions sit first and get the
  // stable leading colors; a plain insertion order would be arbitrary.
  const ranked = withData
    .map(([name, values]) => ({ name, values,
      mean: values.reduce((a, v) => a + v[1], 0) / values.length }))
    .sort((a, b) => b.mean - a.mean || compareStrings(a.name, b.name));
  const traces = ranked.map((r, i) => ({
    type: "scatter", mode: "lines", name: r.name,
    x: r.values.map((v) => v[0] * 1000), y: r.values.map((v) => v[1]),
    line: { width: 1.5, color: th.colors[i % th.colors.length] },
  }));
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
        : "No partition trend data in this window",
      showarrow: false, xref: "paper", yref: "paper", x: 0.5, y: 0.5,
      font: { color: th.font.color, size: 12 },
    }];
  }
  renderPlot("partTrendPlot", traces, layout);
}

function renderPartTable() {
  const tb = $("partTable").querySelector("tbody");
  tb.innerHTML = partRows.map((p) => `
    <tr class="row" data-partition="${escapeHtml(p.name)}">
      <td>${partitionLink(p.name)}</td>
      <td class="num" title="allocated / total GPUs">${escapeHtml(p.gpus_alloc)}/${escapeHtml(p.gpus_total)}</td>
      <td class="num">${escapeHtml(fmtInt(p.job_count))}</td>
      <td class="num">${pctBar(p.mean_util)}</td>
    </tr>`).join("");
  tb.querySelectorAll("tr.row").forEach((tr) =>
    tr.addEventListener("click", () => openPartition(tr.dataset.partition)));
}

function partControlsChanged() { loadPartitions(); }
$("pWindow").addEventListener("change", partControlsChanged);
$("pPartition").addEventListener("change", () => {
  applyPartitionSelection($("pPartition").value);
  loadVram().catch(() => {});
});
$("pRunning").addEventListener("change", (e) => {
  $("pWindow").disabled = e.target.checked;
  loadPartitions();
});
$("partTable").querySelectorAll("th[data-k]").forEach((th) =>
  th.addEventListener("click", () => {
    const k = th.dataset.k;
    partSort = (k === partSort.key)
      ? { key: k, dir: partSort.dir === "desc" ? "asc" : "desc" }
      : { key: k, dir: "desc" };
    const s = partSort.dir === "asc" ? 1 : -1;
    partRows.sort((a, b) => {
      const va = a[k], vb = b[k];
      if (typeof va === "number" && typeof vb === "number") return (va - vb) * s;
      return compareStrings(va, vb) * s;
    });
    renderPartTable();
    markSort($("partTable"), partSort.key, partSort.dir);
  }));

/* ---------------- partitions: VRAM distribution ----------------
 * VRAM usage of jobs in the window, binned by per-job peak VRAM and
 * weighted by allocated GPU-hours. The dual utilization-range slider
 * refilters client-side (no refetch); window / running-only refetch. */

let vramJobs = [];
let vramTotal = 0; // candidates in the window, before the backend cap
let vramToken = 0;
let vramGpuType = "";

function vramWeight() {
  return $("vWeight").value === "eff" ? "eff" : "alloc";
}
function vramHours(j) {
  // Allocated GPU-hours come from sacct and are null for jobs without an
  // allocation row; effective GPU-hours are utilization-weighted and always
  // present. The toggle picks which axis the distribution is measured on.
  return vramWeight() === "eff" ? (j.gpu_hours_eff || 0) : (j.gpu_hours || 0);
}

async function loadVram() {
  const token = ++vramToken;
  // The VRAM fetch blurs only the VRAM panel (vramResults), never the whole
  // partitions tab: window / running-only / partition / weight changes here
  // must not freeze the other graphs.
  const origin = partitionsToken;
  setResultsLoading("vramResults", true);
  status("loading VRAM distribution…");
  try {
    const params = new URLSearchParams({ since_hours: $("pWindow").value });
    if ($("pRunning").checked) params.set("running_only", "true");
    if (selectedPartition) params.set("partition", selectedPartition);
    params.set("weight", vramWeight());
    const data = await api("/api/partitions/vram?" + params);
    if (token !== vramToken) return; // a newer VRAM request supersedes this one
    panelOk("vramResults");
    vramJobs = data.jobs;
    vramTotal = data.total || data.jobs.length;
    fillVramGpuTypes();
    renderVram();
  } catch (e) {
    if (token === vramToken && origin === partitionsToken)
      showPanelError("vramResults", e, loadVram, "the VRAM distribution");
    throw e;
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
  const eff = vramWeight() === "eff";
  const matched = vramJobs.filter((j) =>
    j.mean_util >= lo && j.mean_util <= hi &&
    (!vramGpuType || j.gpu_type === vramGpuType));
  const scopeHours = vramJobs.filter((j) => !vramGpuType || j.gpu_type === vramGpuType)
    .reduce((s, j) => s + vramHours(j), 0);
  const binW = 16;
  const maxG = matched.length
    ? Math.max(...matched.map((j) => j.vram_gb)) : binW;
  const nBins = Math.max(1, Math.ceil(maxG / binW) || 1);
  const bins = new Array(nBins).fill(0);
  const perBin = new Array(nBins).fill(0);
  const matchedHours = matched.reduce((s, j) => s + vramHours(j), 0);
  matched.forEach((j) => {
    const i = Math.min(Math.floor(j.vram_gb / binW), nBins - 1);
    bins[i] += vramHours(j);
    perBin[i] += 1;
  });
  const normalize = $("vNormalize").checked && scopeHours > 0;
  const missing = matched.filter((j) => vramWeight() === "alloc" && j.gpu_hours == null).length;
  const truncated = vramTotal > vramJobs.length;
  const scopeBits = [
    truncated
      ? matched.length + " / " + vramJobs.length + " (top of " + vramTotal + ")"
      : matched.length + " jobs",
    selectedPartition,
    vramGpuType,
  ].filter(Boolean);
  const y = normalize ? bins.map((h) => (h / scopeHours) * 100) : bins;
  const labels = Array.from({ length: nBins }, (_, i) =>
    i * binW + "–" + (i + 1) * binW + " GB");
  const hourLabel = eff ? "effective GPU hours" : "GPU hours";
  const th = plotTheme();
  const trace = {
    type: "bar", x: labels, y,
    marker: { color: th.colors[0] },
    customdata: perBin,
    hovertemplate: "%{x}<br>%{y:.1f}" + (normalize ? "%" : " " + hourLabel) +
      "<br>%{customdata} jobs<extra></extra>",
  };
  const layout = {
    margin: { l: 60, r: 20, t: 10, b: 40 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: th.font,
    // Bar distribution is read-only: no rectangle drag, pan, or axis zoom
    // (same treatment as the partition bar charts).
    xaxis: { title: "VRAM usage (GB per GPU, peak over window)",
             gridcolor: th.grid, fixedrange: true },
    yaxis: { title: normalize ? "% of shown " + hourLabel : hourLabel,
             gridcolor: th.grid, fixedrange: true },
    dragmode: false,
  };
  if (!matched.length) {
    layout.xaxis.showaxis = false;
    layout.yaxis.showaxis = false;
    layout.annotations = [{
      text: "No jobs match the current filters", showarrow: false,
      xref: "paper", yref: "paper", x: 0.5, y: 0.5,
      font: { color: th.font.color, size: 12 },
    }];
  }
  renderPlot("partVramPlot", [trace], layout);
  $("vramMeta").textContent =
    scopeBits.join(" · ") + " · " +
    matchedHours.toFixed(0) + " " + hourLabel +
    (normalize ? " · " + scopeHours.toFixed(0) +
      (truncated ? " in shown set" : " in scope") : "") +
    (missing
      ? " · " + missing + " without allocation data"
      : "") +
    " · utilization " + lo + "–" + hi + "%";
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
$("vWeight").addEventListener("change", () => { loadVram(); });
$("vUtilMin").addEventListener("input", vramSliderInput);
$("vUtilMax").addEventListener("input", vramSliderInput);

/* ---------------- nodes ---------------- */

let nodeRows = [];
let nodeSort = { key: "name", dir: "asc" };

async function loadNodes(force = false) {
  const params = new URLSearchParams({ gpu_only: String($("nGpuOnly").checked) });
  if (force) params.set("refresh", "true");
  const btn = $("nRefresh");
  btn.disabled = true;
  setResultsLoading("nodesResults", true);
  status("loading nodes…");
  try {
    const data = await api("/api/nodes?" + params);
    panelOk("nodesResults");
    nodeRows = data.nodes;
    const t = new Intl.DateTimeFormat("en-GB", {
      timeZone: "Europe/Helsinki", hour: "2-digit", minute: "2-digit",
      hour12: false, timeZoneName: "short",
    }).format(data.time * 1000);
    $("nMeta").textContent = data.count + " GPU nodes · snapshot " + t + " (Europe/Helsinki)";
    fill("nGpuType", [...new Set(nodeRows.map((n) => n.gpu_type).filter(Boolean))].sort());
    renderNodeTable();
    loaded.nodes = true;
  } catch (e) {
    showPanelError("nodesResults", e, () => loadNodes(), "the node list");
    throw e;
  } finally {
    btn.disabled = false;
    setResultsLoading("nodesResults", false);
  }
}

function fill(id, values) {
  const sel = $(id);
  if (sel.options.length > 1) return;
  sel.innerHTML = '<option value="">all</option>' +
    values.map((v) => '<option value="' + escapeHtml(v) + '">' + escapeHtml(v) + "</option>").join("");
}

function filteredNodes() {
  return nodeRows.filter((n) => {
    if (nodeFilters.search && !n.name.toLowerCase().includes(nodeFilters.search)) return false;
    if (nodeFilters.gputype && n.gpu_type !== nodeFilters.gputype) return false;
    if (nodeFilters.busy && !(n.current_util > 0)) return false;
    return true;
  });
}

function renderNodeTable() {
  const rows = filteredNodes();
  const tb = $("nodeTable").querySelector("tbody");
  const nodeFiltered = nodeFilters.search || nodeFilters.gputype || nodeFilters.busy;
  if (!rows.length) {
    tb.innerHTML = emptyRow(8, nodeFiltered
      ? "No nodes match the current filters." : "No GPU nodes in this snapshot.",
      nodeFiltered ? "reset filters" : null);
    const reset = tb.querySelector("button[data-empty-reset]");
    if (reset) reset.addEventListener("click", () => {
      $("nSearch").value = ""; $("nGpuType").value = ""; $("nBusy").checked = false;
      nodeFilters.search = ""; nodeFilters.gputype = ""; nodeFilters.busy = false;
      renderNodeTable();
    });
    return;
  }
  tb.innerHTML = rows.map((n) => {
    const u = n.current_util;
    const busy = u !== null && u > 0;
    const jobs = (n.active_jobs || []).map((j) => jobLink(j.jobid)).join(", ") || "—";
    const rawName = n.name;
    const name = escapeHtml(rawName);
    const gpuType = n.gpu_type
      ? (n.gpu_group ? partitionLink(n.gpu_group, n.gpu_type)
                     : escapeHtml(n.gpu_type))
      : "—";
    const gpusAlloc = escapeHtml(n.gpus_alloc !== undefined ? n.gpus_alloc : 0);
    const gpus = escapeHtml(n.gpus);
    const vram = escapeHtml(n.current_vram === null ? "—" : fmt(n.current_vram));
    const cpusAlloc = escapeHtml(n.cpus_alloc !== undefined ? n.cpus_alloc : 0);
    const cpus = escapeHtml(n.cpus);
    return `
    <tr class="row" data-node="${name}" style="${busy ? "" : "opacity:.55"}">
      <td>${nodeLink(rawName)}</td>
      <td>${gpuType}</td>
      <td title="${escapeHtml(n.reason || "")}">${nodeStateBadges(n)}</td>
      <td class="num" title="allocated / total GPUs">${gpusAlloc}/${gpus}</td>
      <td class="num">${u === null ? "idle" : pctBar(u)}</td>
      <td class="num">${vram}</td>
      <td class="num" title="allocated / total CPUs">${cpusAlloc}/${cpus}</td>
      <td class="small">${jobs}</td>
    </tr>`;
  }).join("");
  tb.querySelectorAll("tr.row").forEach((tr) =>
    tr.addEventListener("click", (e) => {
      const link = e.target.closest("a.joblink");
      if (link) { e.stopPropagation(); openJob(link.dataset.job, { node: tr.dataset.node }); return; }
      const nlink = e.target.closest("a.nodelink");
      if (nlink) { e.stopPropagation(); openNode(nlink.dataset.node); return; }
      const plink = e.target.closest("a.partitionlink");
      if (plink) { e.stopPropagation(); openPartition(plink.dataset.partition); return; }
      loadNodeDetail(tr.dataset.node);
    }));
  markSort($("nodeTable"), nodeSort.key, nodeSort.dir);
  $("nCount").textContent = rows.length + " / " + nodeRows.length;
}
let nodeDetailToken = 0;
let nodeDetailName = null;
let nodeDetailData = null; // raw API payload; traces rebuild per theme
let nodeDetailJob = "";     // filter traces to one job's GPUs
async function loadNodeDetail(name) {
  nodeDetailName = name;
  const token = ++nodeDetailToken;
  $("nodeDetailResults").style.display = "block";
  $("nodeDetailTitle").textContent = "Node " + name;
  setResultsLoading("nodeDetailResults", true);
  $("nodeDetailResults").scrollIntoView({ behavior: "smooth", block: "nearest" });
  try {
    const data = await api("/api/nodes/" + encodeURIComponent(name) + "?view=" + $("ndWindow").value);
    if (token !== nodeDetailToken) return;
    panelOk("nodeDetailResults");
    setUrl("/node/" + encodeURIComponent(name));
    nodeDetailData = data;
    nodeDetailJob = ""; // a window/node change invalidates the job choice
    fillNodeJobSelect(data);
    renderNodeDetail(data, name);
  } catch (e) {
    if (token === nodeDetailToken)
      showPanelError("nodeDetailResults", e, () => loadNodeDetail(name), "the node detail");
    throw e;
  } finally {
    if (token === nodeDetailToken) setResultsLoading("nodeDetailResults", false);
  }
}

function fillNodeJobSelect(data) {
  const sel = $("ndJob");
  const prev = sel.value;
  const ids = [...new Set(data.series.utilization.map((s) => s.metric.slurmjobid)
    .filter(Boolean))].sort(compareStrings);
  sel.innerHTML = '<option value="">all jobs</option>' +
    ids.map((id) => '<option value="' + escapeHtml(id) + '">' + escapeHtml(id) + '</option>').join("");
  if (ids.includes(prev)) sel.value = prev;
}

function renderNodeDetail(data, name) {
  $("nodeDetailTitle").textContent =
    "Node " + name + (nodeDetailJob ? " · job " + nodeDetailJob : "");
  const th = plotTheme();
  const keep = (s) => !nodeDetailJob || s.metric.slurmjobid === nodeDetailJob;
  const utilTraces = [];
  let ci = 0;
  data.series.utilization.forEach((s) => {
    if (!keep(s)) return;
    const dev = "GPU " + (s.metric.gpu !== undefined ? s.metric.gpu : "?");
    const job = s.metric.slurmjobid || "";
    utilTraces.push({
      type: "scatter", mode: "lines", name: dev + (job ? " (job " + job + ")" : ""),
      x: s.values.map((v) => v[0] * 1000), y: s.values.map((v) => v[1]),
      line: { width: 2, color: th.colors[ci % th.colors.length] },
    });
    ci++;
  });
  const vramTraces = [];
  let vi = 0;
  data.series.vram.forEach((s) => {
    if (!keep(s)) return;
    const dev = "GPU " + (s.metric.gpu !== undefined ? s.metric.gpu : "?");
    const job = s.metric.slurmjobid || "";
    vramTraces.push({
      type: "scatter", mode: "lines",
      name: dev + (job ? " (job " + job + ")" : ""),
      x: s.values.map((v) => v[0] * 1000), y: s.values.map((v) => v[1]),
      line: { width: 2, dash: "dot", color: th.colors[vi % th.colors.length] },
    });
    vi++;
  });
  const base = {
    margin: { l: 46, r: 20, t: 10, b: 34 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: th.font,
    showlegend: true, legend: { orientation: "h", y: -0.2 },
    yaxis: { title: "%", range: [0, 105], gridcolor: th.grid },
    xaxis: { type: "date", gridcolor: th.grid },
  };
  renderPlot("nodeDetailUtilPlot", utilTraces, base);
  renderPlot("nodeDetailVramPlot", vramTraces, base);
}

let nodeDebounce = null;
function nodeControlsChanged() {
  clearTimeout(nodeDebounce);
  nodeDebounce = setTimeout(renderNodeTable, 200);
}
$("nSearch").addEventListener("input", (e) => {
  nodeFilters.search = e.target.value.trim().toLowerCase();
  nodeControlsChanged();
});
$("nGpuType").addEventListener("change", (e) => {
  nodeFilters.gputype = e.target.value;
  nodeControlsChanged();
});
$("nBusy").addEventListener("change", (e) => {
  nodeFilters.busy = e.target.checked;
  nodeControlsChanged();
});
$("nGpuOnly").addEventListener("change", () => loadNodes());
$("nRefresh").addEventListener("click", () => { loadNodes(true); });
$("ndWindow").addEventListener("change", () => {
  if (nodeDetailName) loadNodeDetail(nodeDetailName);
});
$("ndJob").addEventListener("change", (e) => {
  nodeDetailJob = e.target.value;
  if (nodeDetailData) renderNodeDetail(nodeDetailData, nodeDetailName);
});
$("nodeTable").querySelectorAll("th[data-k]").forEach((th) =>
  th.addEventListener("click", () => {
    const k = th.dataset.k;
    nodeSort = (k === nodeSort.key)
      ? { key: k, dir: nodeSort.dir === "desc" ? "asc" : "desc" }
      : { key: k, dir: k === "name" ? "asc" : "desc" };
    const s = nodeSort.dir === "asc" ? 1 : -1;
    nodeRows.sort((a, b) => {
      const va = a[k], vb = b[k];
      if (va === null || va === undefined) return 1;
      if (vb === null || vb === undefined) return -1;
      if (typeof va === "number" && typeof vb === "number") return (va - vb) * s;
      return compareStrings(va, vb) * s;
    });
    renderNodeTable();
  }));

/* ---------------- deep links ----------------
 * Shareable URLs: /job/<id> opens the Jobs tab with that job's detail,
 * /node/<name> opens the Nodes tab with that node's detail,
 * /partition/<name> opens the Partitions tab scoped to that partition
 * (trend + VRAM), /user/<name> opens the Users tab with that user's jobs
 * fetched, and /jobs, /partitions, /users and /nodes open the plain tabs
 * (plain /partitions clears any partition selection). The server serves
 * the SPA shell for all of them; this code restores the view from the
 * path and keeps the URL in sync via history.pushState. */

function setUrl(path) {
  if (location.pathname + location.search !== path) {
    history.pushState({ path }, "", path);
  }
}

function openJob(jobid, from) {
  showTab("jobs").then(() => loadJobDetail(jobid, from)).catch(() => {});
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function escapeList(values) {
  const list = Array.isArray(values) ? values : values ? [values] : [];
  return list.map(escapeHtml).join(", ") || "—";
}

function jobLink(jobid) {
  const safe = escapeHtml(jobid);
  return '<a class="joblink" data-job="' + safe +
    '" title="open job ' + safe + ' in the Jobs tab">' + safe + "</a>";
}

function userLink(user) {
  if (!user) return "—";
  const safe = escapeHtml(user);
  return '<a class="userlink" data-user="' + safe +
    '" title="open ' + safe + ' in the Users tab">' + safe + "</a>";
}

function nodeLink(node) {
  if (!node) return "—";
  const safe = escapeHtml(node);
  return '<a class="nodelink" data-node="' + safe +
    '" title="open ' + safe + ' in the Nodes tab">' + safe + "</a>";
}

function nodeLinks(values) {
  const list = Array.isArray(values) ? values : values ? [values] : [];
  return list.map(nodeLink).join(", ") || "—";
}

function partitionLink(partition, label = partition) {
  if (!partition) return "—";
  const safe = escapeHtml(partition);
  return '<a class="partitionlink" data-partition="' + safe +
    '" title="open ' + safe + ' in the Partitions tab">' +
    escapeHtml(label) + "</a>";
}

function openUser(user) {
  showTab("users").then(() => {
    // finalizeUser is the only path that fetches the user's jobs, and it
    // keeps the URL in sync (/user/<name>, /users on deselect). It
    // accepts raw text, so a user with no window activity still resolves.
    finalizeUser(user);
  }).catch(() => {});
}

function openNode(node) {
  showTab("nodes").then(() => loadNodeDetail(node)).catch(() => {});
}

function openPartition(partition) {
  selectedPartition = partition;
  const wasLoaded = loaded.partitions;
  showTab("partitions").then(() => {
    // applyPartitionSelection re-renders the trend and syncs the URL
    // (/partition/<name>). A fresh loadPartitions already scoped the VRAM
    // fetch on its own, so only a pre-loaded tab needs one here.
    applyPartitionSelection(selectedPartition);
    if (wasLoaded) loadVram().catch(() => {});
  }).catch(() => {});
}

function clearPartitionSelection() {
  if (!selectedPartition) return;
  selectedPartition = "";
  $("pPartition").value = "";
  updatePartitionScope();
  renderPartTrend(partTrendData);
  if (loaded.partitions) loadVram().catch(() => {});
}

function restoreFromUrl() {
  let m = location.pathname.match(/^\/job\/([^/]+)\/?$/);
  if (m) { showTab("jobs").then(() => loadJobDetail(m[1])).catch(() => {}); return; }
  m = location.pathname.match(/^\/node\/([^/]+)\/?$/);
  if (m) {
    const node = decodeURIComponent(m[1]);
    showTab("nodes").then(() => loadNodeDetail(node)).catch(() => {});
    return;
  }
  m = location.pathname.match(/^\/user\/([^/]+)\/?$/);
  if (m) {
    const user = decodeURIComponent(m[1]);
    showTab("users").then(() => { finalizeUser(user); }).catch(() => {});
    return;
  }
  m = location.pathname.match(/^\/partition\/([^/]+)\/?$/);
  if (m) { openPartition(decodeURIComponent(m[1])); return; }
  if (location.pathname === "/jobs") { showTab("jobs"); return; }
  if (location.pathname === "/partitions") {
    clearPartitionSelection();
    showTab("partitions");
    return;
  }
  if (location.pathname === "/users") { showTab("users"); return; }
  if (location.pathname === "/nodes") { showTab("nodes"); return; }
}

window.addEventListener("popstate", restoreFromUrl);
document.querySelectorAll("nav.tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    // Plain /partitions clears any partition selection, matching the
    // deep-link restore; clear before showTab so a fresh loadPartitions
    // fetches unscoped.
    if (b.dataset.tab === "partitions") clearPartitionSelection();
    showTab(b.dataset.tab);
    setUrl("/" + b.dataset.tab);
  }));

function rerenderAllPlots() {
  if (loaded.jobs) {
    renderJobEfficiency();
    renderJobsView();
  }
  if (jobDetailData && $("jobDetailResults").style.display !== "none") {
    renderJobDetail(jobDetailData);
  }
  if (loaded.partitions) {
    renderPartBar();
    renderPartOccupancy();
    renderPartTrend(partTrendData);
    if (vramJobs.length) renderVram();
  }
  if (nodeDetailData && $("nodeDetailResults").style.display !== "none") {
    renderNodeDetail(nodeDetailData, nodeDetailName);
  }
}

$("themeBtn").addEventListener("click", () => {
  const next = currentTheme() === "light" ? "dark" : "light";
  localStorage.setItem(THEME_KEY, next);
  applyTheme(next);
  rerenderAllPlots();
});

/* Init theme: saved preference wins, else OS preference. */
applyTheme(currentTheme());

/* ---------------- init ---------------- */

checkHealth();
if (location.pathname === "/" || location.pathname === "") {
  loadJobs().catch(() => {});
} else {
  restoreFromUrl();
}
