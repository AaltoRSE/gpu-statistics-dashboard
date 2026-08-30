/* Triton GPU Efficiency Dashboard — frontend logic.
 * All data is fetched on demand from the FastAPI backend (which queries
 * sacct / scontrol / Prometheus live). No client-side caching of data.
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

function stateBadge(s) {
  if (!s) return "";
  const known = ["RUNNING", "COMPLETED", "PENDING", "IDLE", "FAILED", "CANCELLED",
                 "TIMEOUT", "DRAIN", "DOWN", "DRAINING", "MIXED", "ALLOCATED"];
  const cls = known.includes(s) ? s : "PENDING";
  return '<span class="badge ' + cls + '">' + escapeHtml(s) + "</span>";
}

function tsToDate(ts) {
  return new Date(ts * 1000).toISOString().slice(0, 16).replace("T", " ");
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

async function loadJobs() {
  const token = ++jobsToken;
  // A name/ID search is server-backed because it determines the highest
  // efficiency chart. The partition selector filters the returned rows locally.
  const params = new URLSearchParams({ since_hours: $("jWindow").value });
  const search = $("jSearch").value.trim();
  if (search) params.set("search", search);
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
  setResultsLoading("jobsResults", true);
  setResultsLoading("jobEfficiencyResults", true);
  status("loading jobs…");
  try {
    const data = await api("/api/jobs?" + params);
    if (token !== jobsToken) return; // a newer request supersedes this one
    jobRows = data.jobs;
    // The server computes highest/lowest efficiency from the searched rows.
    jobBaseHigh = data.efficiency_high || [];
    jobBaseLow = data.efficiency_low || [];
    // Preserve a compatible local partition selection across a search refresh.
    const selectedPartition = $("jPartition").value;
    $("jPartition").innerHTML =
      '<option value="">all</option>' +
      [...new Set(jobRows.map((j) => j.partition).filter(Boolean))].sort()
        .map((p) => '<option value="' + escapeHtml(p) + '">' + escapeHtml(p) + "</option>").join("");
    $("jPartition").value = [...$("jPartition").options]
      .some((option) => option.value === selectedPartition) ? selectedPartition : "";
    const w = data.window;
    $("jMetaCount").textContent = data.count + " jobs";
    $("jMeta").textContent =
      tsToDate(w.start) + " → " + tsToDate(w.end) + " UTC";
    renderJobEffChart("jobHighBarPlot", jobBaseHigh);
    renderJobEffChart("jobLowBarPlot", jobBaseLow);
    renderJobsView();
    loaded.jobs = true;
  } finally {
    if (token === jobsToken) {
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

// Search and partition are client-side filters over the fetched base set:
// they re-render the table only. The efficiency charts always show the
// base set's extremes (see loadJobs) and are untouched by filtering.
function renderJobsView() {
  const search = $("jSearch").value.trim().toLowerCase();
  const partition = $("jPartition").value;
  let rows = jobRows;
  if (partition) rows = rows.filter((j) => j.partition === partition);
  if (search) rows = rows.filter((j) =>
    j.jobid.includes(search) || (j.name || "").toLowerCase().includes(search));
  jobVisibleRows = rows;
  renderJobTable(sortJobRows());
  markSort($("jobTable"), jobSort.key, jobSort.dir);
  $("jCount").textContent = (partition || search)
    ? rows.length + " / " + jobRows.length + " shown"
    : rows.length + " shown";
}

function renderJobTable(rows) {
  const tb = $("jobTable").querySelector("tbody");
  tb.innerHTML = rows.map((j) => {
    const jobid = escapeHtml(j.jobid);
    const rawName = j.name || "";
    const partition = escapeHtml(j.partition);
    const nodes = escapeList(j.nodes);
    const start = escapeHtml((j.start || "").slice(0, 16));
    const gpus = escapeHtml(j.gpus !== undefined ? j.gpus : "—");
    return `
    <tr class="row" data-job="${jobid}">
      <td>${jobid}</td><td title="${escapeHtml(rawName)}">${escapeHtml(rawName.slice(0, 40))}</td>
      <td>${userLink(j.user)}</td><td>${partition}</td>
      <td>${nodes}</td>
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
      loadJobDetail(tr.dataset.job);
    }));
}

function renderJobEffChart(elId, jobs) {
  const trace = {
    type: "bar", orientation: "h",
    y: jobs.map((j) => j.jobid + " · " + j.user),
    x: jobs.map((j) => j.efficiency),
    customdata: jobs.map((j) => j.jobid),
    marker: {
      color: jobs.map((j) => partBarColor(j.efficiency)),
      line: { width: 0 },
    },
    hovertemplate: "<b>%{y}</b><br>average efficiency: %{x:.1f}%<extra></extra>",
  };
  const th = plotTheme();
  const layout = {
    margin: { l: 130, r: 20, t: 10, b: 30 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: th.font,
    xaxis: { title: "average efficiency %", range: [0, 105], gridcolor: th.grid },
    yaxis: { autorange: "reversed", gridcolor: th.grid },
  };
  if (!jobs.length) {
    layout.annotations = [{
      text: "No jobs match the current filters", showarrow: false,
      xref: "paper", yref: "paper", x: 0.5, y: 0.5,
      font: { color: th.font.color, size: 12 },
    }];
    renderPlot(elId, [trace], layout);
    return;
  }
  renderPlot(elId, [trace], layout).then((g) => {
    // Plotly.react keeps the graph div across renders: clear stale
    // handlers before rebinding or one click fires N detail loads.
    g.removeAllListeners("plotly_click");
    g.on("plotly_click", (ev) => {
      const id = ev.points[0].customdata;
      if (id) loadJobDetail(id);
    });
  });
}

let jobDetailToken = 0;
let jobDetailData = null; // raw API payload; traces rebuild per theme
async function loadJobDetail(jobid) {
  const token = ++jobDetailToken;
  $("jobDetailCard").style.display = "block";
  $("jobDetailTitle").textContent = "Job " + jobid + " — loading…";
  $("jobDetailCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
  const data = await api("/api/jobs/" + jobid + "?since_hours=" + $("jWindow").value);
  if (token !== jobDetailToken) return;
  setUrl("/job/" + jobid);
  jobDetailData = data;
  renderJobDetail(data);
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
  data.series.utilization.forEach((s, i) => {
    const dev = s.metric.gpu !== undefined ? "GPU " + s.metric.gpu : s.metric.instance;
    traces.push({
      type: "scatter", mode: "lines", name: dev + " util %",
      x: s.values.map((v) => v[0] * 1000), y: s.values.map((v) => v[1]),
      line: { width: 2, color: th.colors[i % th.colors.length] },
    });
  });
  data.series.vram.forEach((s, i) => {
    const dev = s.metric.gpu !== undefined ? "GPU " + s.metric.gpu + " VRAM" : "VRAM";
    traces.push({
      type: "scatter", mode: "lines", name: dev + " %",
      x: s.values.map((v) => v[0] * 1000), y: s.values.map((v) => v[1]),
      line: { width: 2, dash: "dot", color: th.colors[(i + 4) % th.colors.length] },
      yaxis: "y2",
    });
  });
  // Timeline delimiters: an open circle where the utilization line starts
  // and a triangle where it ends, at the actual endpoint (t, y) of the
  // first series to reach that timestamp (strict comparisons keep the
  // first-encountered point on ties). Hidden from the legend.
  let startPt = null, endPt = null;
  data.series.utilization.forEach((s) => {
    s.values.forEach((v) => {
      const t = v[0] * 1000;
      if (!startPt || t < startPt[0]) startPt = [t, v[1]];
      if (!endPt || t > endPt[0]) endPt = [t, v[1]];
    });
  });
  if (startPt) {
    traces.push({
      type: "scatter", mode: "markers", name: "Job start", showlegend: false,
      x: [startPt[0]], y: [startPt[1]],
      marker: { symbol: "circle-open", size: 11, color: th.font.color,
                line: { width: 2 } },
    });
    traces.push({
      type: "scatter", mode: "markers", name: "Job end", showlegend: false,
      x: [endPt[0]], y: [endPt[1]],
      marker: { symbol: "triangle-up", size: 11, color: th.warn },
    });
  }
  const detailLayout = {
    margin: { l: 46, r: 46, t: 10, b: 34 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: th.font,
    showlegend: true, legend: { orientation: "h", y: -0.15 },
    yaxis: { title: "util %", range: [0, 105], gridcolor: th.grid },
    yaxis2: { title: "VRAM %", overlaying: "y", side: "right", range: [0, 105], gridcolor: th.grid },
    xaxis: { type: "date", gridcolor: th.grid },
  };
  renderPlot("jobDetailPlot", traces, detailLayout);
}

$("jWindow").addEventListener("change", () => { loadJobs(); });
$("jRunning").addEventListener("change", (e) => {
  $("jWindow").disabled = e.target.checked;
  $("jLimit").disabled = e.target.checked;
  loadJobs();
});
$("jLimit").addEventListener("change", loadJobs);
// Search refreshes the server-calculated highest-efficiency chart. Partition
// filtering is local because it only changes the table.
$("jSearch").addEventListener("input", loadJobs);
$("jPartition").addEventListener("change", renderJobsView);
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
    userRows = data.users;
    const w = data.window;
    $("uMetaCount").textContent = data.count + " users";
    $("uMeta").textContent =
      tsToDate(w.start) + " → " + tsToDate(w.end) + " UTC";
    renderUserTable();
    loaded.users = true;
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

/* Finalize a selection: only this path fetches the user's jobs. An
 * empty finalized value deselects (hides the jobs card). */
function finalizeUser(name) {
  name = (name || "").trim();
  if (!name) {
    userSelected = null;
    $("userJobsCard").style.display = "none";
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
  renderUserTable();
  loadUserJobs(finalName);
  setUrl("/user/" + encodeURIComponent(finalName));
}

async function loadUserJobs(user) {
  const token = ++userJobsToken;
  $("userJobsCard").style.display = "block";
  $("userJobsTitle").textContent =
    "Jobs · " + user + " · last " + $("uWindow").value / 24 + " d";
  setResultsLoading("usersResults", true);
  status("loading " + user + "’s jobs…");
  const params = new URLSearchParams({
    since_hours: $("uWindow").value, user, limit: "500",
  });
  if ($("uRunning").checked) params.set("running_only", "true");
  try {
    const data = await api("/api/jobs?" + params);
    if (token !== userJobsToken) return;
    userJobs = data.jobs;
    renderUserJobsTable();
  } finally {
    if (token === userJobsToken) setResultsLoading("usersResults", false);
  }
}

function renderUserJobsTable() {
  const tb = $("userJobsTable").querySelector("tbody");
  tb.innerHTML = userJobs.map((j) => {
    const jobid = escapeHtml(j.jobid);
    const rawName = j.name || "";
    const partition = escapeHtml(j.partition);
    const nodes = escapeList(j.nodes);
    const start = escapeHtml((j.start || "").slice(0, 16));
    const gpus = escapeHtml(j.gpus !== undefined ? j.gpus : "—");
    return `
    <tr class="row" data-job="${jobid}">
      <td>${jobLink(j.jobid)}</td>
      <td title="${escapeHtml(rawName)}">${escapeHtml(rawName.slice(0, 40))}</td>
      <td>${partition}</td>
      <td>${nodes}</td>
      <td>${stateBadge(j.state)}</td>
      <td>${start}</td>
      <td class="num">${gpus}</td>
      <td class="num">${pctBar(j.mean_util)}</td>
      <td class="num">${escapeHtml(j.efficiency !== undefined ? fmt(j.efficiency) : "—")}</td>
      <td class="num">${escapeHtml(fmtInt(j.gpu_hours_eff))}</td>
    </tr>`;
  }).join("");
  tb.querySelectorAll("tr.row").forEach((tr) =>
    tr.addEventListener("click", (e) => {
      const link = e.target.closest("a.joblink");
      if (link) { e.stopPropagation(); openJob(tr.dataset.job); return; }
      openJob(tr.dataset.job);
    }));
}

$("uWindow").addEventListener("change", () => {
  userSelected = null;
  $("userJobsCard").style.display = "none";
  loadUsers();
});
$("uRunning").addEventListener("change", () => {
  renderUserTable();
  if (userSelected) loadUserJobs(userSelected);
});
$("uRefresh").addEventListener("click", loadUsers);
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
let partSort = { key: "mean_util", dir: "desc" };
let partitionsToken = 0;

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
    if (token === partitionsToken) setResultsLoading("partitionsResults", false);
    throw e;
  }
  if (token !== partitionsToken) return; // a newer request supersedes this one
  partRows = data.partitions;
  const w = data.window;
  $("pCount").textContent = data.partitions.length + " groups · " +
    tsToDate(w.start) + " → " + tsToDate(w.end) + " UTC";
  renderPartBar();
  renderPartOccupancy();
  renderPartTrend(data.trend);
  partTrendData = data.trend;
  renderPartTable();
  markSort($("partTable"), partSort.key, partSort.dir);
  loaded.partitions = true;
  // The GPU-type selector shares the tab's window / running-only controls;
  // validate its selection against the fresh groups, then fetch the VRAM
  // distribution for it. The VRAM fetch blurs only its own card, so the
  // other graphs stay interactive while it loads.
  refreshVramSelector(partRows);
  if (token !== partitionsToken) return;
  await loadVram().catch(() => {});
  if (token === partitionsToken) setResultsLoading("partitionsResults", false);
}

function refreshVramSelector(groups) {
  const sel = $("vGpuType");
  const cur = sel.value;
  const types = [...new Set(groups.map((g) => g.name))].sort();
  sel.innerHTML = '<option value="">all GPU types</option>' +
    types.map((t) => '<option value="' + escapeHtml(t) + '">' + escapeHtml(t) + "</option>").join("");
  sel.value = cur && types.includes(cur) ? cur : "";
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
  // Same threshold palette as Mean utilization by group: every GPU type
  // shows exactly the color it has in the utilization chart (including
  // zero-height / no-occupancy bars).
  const th = plotTheme();
  const rows = partRows.slice().sort((a, b) => compareStrings(a.name, b.name));
  renderPlot("partOccupancyPlot", [{
    type: "bar",
    x: rows.map((p) => p.name),
    y: rows.map((p) => (p.mean_occupancy === null ? 0 : p.mean_occupancy)),
    marker: { color: rows.map((p) => partBarColor(p.mean_util)) },
    hovertemplate: rows.map((p) => p.mean_occupancy === null
      ? "<b>%{x}</b><br>no occupancy data<extra></extra>"
      : "<b>%{x}</b><br>mean occupancy %{y:.1f}%<extra></extra>"),
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
  const traces = Object.entries(trend).map(([name, values], i) => ({
    type: "scatter", mode: "lines", name,
    x: values.map((v) => v[0] * 1000), y: values.map((v) => v[1]),
    line: { width: 1.5, color: th.colors[i % th.colors.length] },
  }));
  renderPlot("partTrendPlot", traces, {
    margin: { l: 40, r: 20, t: 10, b: 30 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: th.font,
    showlegend: true, legend: { orientation: "h", y: -0.18 },
    yaxis: { title: "avg util %", range: [0, 105], gridcolor: th.grid },
    xaxis: { type: "date", gridcolor: th.grid },
  });
}

function renderPartTable() {
  const tb = $("partTable").querySelector("tbody");
  tb.innerHTML = partRows.map((p) => `
    <tr>
      <td>${escapeHtml(p.name)}</td>
      <td class="num" title="allocated / total GPUs">${escapeHtml(p.gpus_alloc)}/${escapeHtml(p.gpus_total)}</td>
      <td class="num">${escapeHtml(fmtInt(p.job_count))}</td>
      <td class="num">${pctBar(p.mean_util)}</td>
    </tr>`).join("");
}

function partControlsChanged() { loadPartitions(); }
$("pWindow").addEventListener("change", partControlsChanged);
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

async function loadVram() {
  const token = ++vramToken;
  // The VRAM fetch blurs only the VRAM card (vramResults), never the whole
  // partitions panel: window / running-only / GPU-type changes here must
  // not freeze the other graphs. Only clear when both this VRAM request
  // and its originating partitions request are still the latest.
  const origin = partitionsToken;
  setResultsLoading("vramResults", true);
  status("loading VRAM distribution…");
  try {
    const params = new URLSearchParams({ since_hours: $("pWindow").value });
    if ($("pRunning").checked) params.set("running_only", "true");
    if ($("vGpuType").value) params.set("gpu_type", $("vGpuType").value);
    const data = await api("/api/partitions/vram?" + params);
    if (token !== vramToken) return; // a newer VRAM request supersedes this one
    vramJobs = data.jobs;
    vramTotal = data.total || data.jobs.length;
    renderVram();
  } finally {
    if (token === vramToken && origin === partitionsToken)
      setResultsLoading("vramResults", false);
  }
}
// GPU-type selector: refetches only the VRAM distribution (window /
// running-only changes are already owned by loadPartitions).
function reloadVram() { loadVram(); }

function renderVram() {
  const lo = Math.min(+$("vUtilMin").value, +$("vUtilMax").value);
  const hi = Math.max(+$("vUtilMin").value, +$("vUtilMax").value);
  const matched = vramJobs.filter((j) => j.mean_util >= lo && j.mean_util <= hi);
  const allHours = vramJobs.reduce((s, j) => s + (j.gpu_hours || 0), 0);
  const binW = 16;
  const maxG = matched.length
    ? Math.max(...matched.map((j) => j.vram_gb)) : binW;
  const nBins = Math.max(1, Math.ceil(maxG / binW) || 1);
  const bins = new Array(nBins).fill(0);
  const perBin = new Array(nBins).fill(0);
  const matchedHours = matched.reduce((s, j) => s + (j.gpu_hours || 0), 0);
  matched.forEach((j) => {
    const i = Math.min(Math.floor(j.vram_gb / binW), nBins - 1);
    bins[i] += j.gpu_hours || 0;
    perBin[i] += 1;
  });
  const normalize = $("vNormalize").checked && allHours > 0;
  const weighted = vramJobs.filter((j) => j.gpu_hours != null).length;
  const truncated = vramTotal > vramJobs.length;
  const scopeBits = [
    truncated
      ? matched.length + " / " + vramJobs.length + " (top of " + vramTotal + ")"
      : matched.length + " jobs",
    $("vGpuType").value,
  ].filter(Boolean);
  const y = normalize ? bins.map((h) => (h / allHours) * 100) : bins;
  const labels = Array.from({ length: nBins }, (_, i) =>
    i * binW + "–" + (i + 1) * binW + " GB");
  const th = plotTheme();
  const trace = {
    type: "bar", x: labels, y,
    marker: { color: th.colors[0] },
    customdata: perBin,
    hovertemplate: "%{x}<br>%{y:.1f}" + (normalize ? "%" : " GPU hours") +
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
    yaxis: { title: normalize ? "% of shown GPU hours" : "GPU hours",
             gridcolor: th.grid, fixedrange: true },
    dragmode: false,
  };
  if (!matched.length) {
    layout.annotations = [{
      text: "No jobs match the current filters", showarrow: false,
      xref: "paper", yref: "paper", x: 0.5, y: 0.5,
      font: { color: th.font.color, size: 12 },
    }];
  }
  renderPlot("partVramPlot", [trace], layout);
  $("vramMeta").textContent =
    scopeBits.join(" · ") + " · " +
    matchedHours.toFixed(0) + " GPU hours" +
    (normalize ? " · " + allHours.toFixed(0) +
      (truncated ? " in shown set" : " in window") : "") +
    (weighted < vramJobs.length
      ? " · " + (vramJobs.length - weighted) + " without allocation data"
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
$("vUtilMin").addEventListener("input", vramSliderInput);
$("vUtilMax").addEventListener("input", vramSliderInput);
$("vGpuType").addEventListener("change", reloadVram);

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
    nodeRows = data.nodes;
    const t = new Intl.DateTimeFormat("en-GB", {
      timeZone: "Europe/Helsinki", hour: "2-digit", minute: "2-digit",
      hour12: false, timeZoneName: "short",
    }).format(data.time * 1000);
    $("nMeta").textContent = data.count + " GPU nodes · snapshot " + t + " (Europe/Helsinki)";
    fill("nGpuType", [...new Set(nodeRows.map((n) => n.gpu_type).filter(Boolean))].sort());
    renderNodeTable();
    loaded.nodes = true;
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
  tb.innerHTML = rows.map((n) => {
    const u = n.current_util;
    const busy = u !== null && u > 0;
    const jobs = (n.active_jobs || []).map((j) =>
      jobLink(j.jobid) + " " + userLink(j.user)
    ).join(", ") || "—";
    const name = escapeHtml(n.name);
    const gpuType = escapeHtml(n.gpu_type || "—");
    const gpusAlloc = escapeHtml(n.gpus_alloc !== undefined ? n.gpus_alloc : 0);
    const gpus = escapeHtml(n.gpus);
    const vram = escapeHtml(n.current_vram === null ? "—" : fmt(n.current_vram));
    const cpusAlloc = escapeHtml(n.cpus_alloc !== undefined ? n.cpus_alloc : 0);
    const cpus = escapeHtml(n.cpus);
    return `
    <tr class="row" data-node="${name}" style="${busy ? "" : "opacity:.55"}">
      <td>${name}</td>
      <td>${gpuType}</td>
      <td class="num" title="allocated / total GPUs">${gpusAlloc}/${gpus}</td>
      <td class="num">${u === null ? "idle" : pctBar(u)}</td>
      <td class="num">${vram}</td>
      <td class="num" title="allocated / total CPUs">${cpusAlloc}/${cpus}</td>
      <td class="small">${jobs}</td>
    </tr>`;
  }).join("");
  tb.querySelectorAll("tr.row").forEach((tr) =>
    tr.addEventListener("click", (e) => {
      const userEl = e.target.closest("a.userlink");
      if (userEl) { e.stopPropagation(); openUser(userEl.dataset.user); return; }
      const link = e.target.closest("a.joblink");
      if (link) { e.stopPropagation(); openJob(link.dataset.job); return; }
      loadNodeDetail(tr.dataset.node);
    }));
  markSort($("nodeTable"), nodeSort.key, nodeSort.dir);
  $("nCount").textContent = rows.length + " / " + nodeRows.length;
}

let nodeDetailToken = 0;
let nodeDetailName = null;
let nodeDetailData = null; // raw API payload; traces rebuild per theme
async function loadNodeDetail(name) {
  nodeDetailName = name;
  const token = ++nodeDetailToken;
  $("nodeDetailCard").style.display = "block";
  $("nodeDetailTitle").textContent = "Node " + name + " — loading…";
  $("nodeDetailCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
  const data = await api("/api/nodes/" + name + "?view=" + $("ndWindow").value);
  if (token !== nodeDetailToken) return;
  setUrl("/node/" + name);
  nodeDetailData = data;
  renderNodeDetail(data, name);
}

function renderNodeDetail(data, name) {
  $("nodeDetailTitle").textContent = "Node " + name;
  const th = plotTheme();
  const utilTraces = [];
  data.series.utilization.forEach((s, i) => {
    const dev = "GPU " + (s.metric.gpu !== undefined ? s.metric.gpu : "?");
    const job = s.metric.slurmjobid || "";
    utilTraces.push({
      type: "scatter", mode: "lines", name: dev + (job ? " (job " + job + ")" : ""),
      x: s.values.map((v) => v[0] * 1000), y: s.values.map((v) => v[1]),
      line: { width: 2, color: th.colors[i % th.colors.length] },
    });
  });
  const vramTraces = [];
  data.series.vram.forEach((s, i) => {
    const dev = "GPU " + (s.metric.gpu !== undefined ? s.metric.gpu : "?");
    const job = s.metric.slurmjobid || "";
    vramTraces.push({
      type: "scatter", mode: "lines",
      name: dev + (job ? " (job " + job + ")" : ""),
      x: s.values.map((v) => v[0] * 1000), y: s.values.map((v) => v[1]),
      line: { width: 2, dash: "dot", color: th.colors[i % th.colors.length] },
    });
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
 * /node/<name> opens the Nodes tab with that node's detail, /user/<name>
 * opens the Users tab with that user's jobs fetched, and /jobs,
 * /partitions, /users and /nodes open the plain tabs. The server serves
 * the SPA shell for all of them; this code restores the view from the
 * path and keeps the URL in sync via history.pushState. */

function setUrl(path) {
  if (location.pathname + location.search !== path) {
    history.pushState({ path }, "", path);
  }
}

function openJob(jobid) {
  showTab("jobs").then(() => loadJobDetail(jobid)).catch(() => {});
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

function openUser(user) {
  showTab("users").then(() => {
    // finalizeUser is the only path that fetches the user's jobs, and it
    // keeps the URL in sync (/user/<name>, /users on deselect). It
    // accepts raw text, so a user with no window activity still resolves.
    finalizeUser(user);
  }).catch(() => {});
}

function restoreFromUrl() {
  let m = location.pathname.match(/^\/job\/([^/]+)\/?$/);
  if (m) { showTab("jobs").then(() => loadJobDetail(m[1])).catch(() => {}); return; }
  m = location.pathname.match(/^\/node\/([^/]+)\/?$/);
  if (m) { showTab("nodes").then(() => loadNodeDetail(m[1])).catch(() => {}); return; }
  m = location.pathname.match(/^\/user\/([^/]+)\/?$/);
  if (m) {
    const user = decodeURIComponent(m[1]);
    showTab("users").then(() => { finalizeUser(user); }).catch(() => {});
    return;
  }
  if (location.pathname === "/jobs") { showTab("jobs"); return; }
  if (location.pathname === "/partitions") { showTab("partitions"); return; }
  if (location.pathname === "/users") { showTab("users"); return; }
  if (location.pathname === "/nodes") { showTab("nodes"); return; }
}

window.addEventListener("popstate", restoreFromUrl);
document.querySelectorAll("nav.tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    showTab(b.dataset.tab);
    setUrl("/" + b.dataset.tab);
  }));

function rerenderAllPlots() {
  if (loaded.jobs) {
    renderJobEffChart("jobHighBarPlot", jobBaseHigh);
    renderJobEffChart("jobLowBarPlot", jobBaseLow);
    renderJobsView();
  }
  if (jobDetailData && $("jobDetailCard").style.display !== "none") {
    renderJobDetail(jobDetailData);
  }
  if (loaded.partitions) {
    renderPartBar();
    renderPartOccupancy();
    renderPartTrend(partTrendData);
    if (vramJobs.length) renderVram();
  }
  if (nodeDetailData && $("nodeDetailCard").style.display !== "none") {
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
