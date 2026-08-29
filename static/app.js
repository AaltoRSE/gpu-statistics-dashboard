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
  return '<span class="bar-track"><span class="bar ' + cls +
    '" style="width:' + p.toFixed(0) + '%"></span></span>' + p.toFixed(0) + "%";
}

function stateBadge(s) {
  if (!s) return "";
  const known = ["RUNNING", "COMPLETED", "PENDING", "IDLE", "FAILED", "CANCELLED",
                 "TIMEOUT", "DRAIN", "DOWN", "DRAINING", "MIXED", "ALLOCATED"];
  const cls = known.includes(s) ? s : "PENDING";
  return '<span class="badge ' + cls + '">' + s + "</span>";
}

function tsToDate(ts) {
  return new Date(ts * 1000).toISOString().slice(0, 16).replace("T", " ");
}

/* ---------------- health ---------------- */

async function checkHealth() {
  try {
    const h = await api("/api/health");
    $("health").innerHTML = '<b>&#9679;</b> prometheus: ' + h.prometheus.replace(/^https?:\/\//, "");
  } catch (_) {
    $("health").innerHTML = '<span style="color:var(--bad)">&#9679; backend down</span>';
  }
}

/* ---------------- tabs ---------------- */

const loaded = { jobs: false, partitions: false, nodes: false };
const nodeFilters = { search: "", gputype: "", busy: false, gpuOnly: true };

function showTab(name) {
  document.querySelectorAll("nav.tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tabpage").forEach((p) =>
    p.classList.toggle("active", p.id === "tab-" + name));
  let p = Promise.resolve();
  if (name === "jobs" && !loaded.jobs) p = loadJobs();
  if (name === "partitions" && !loaded.partitions) p = loadPartitions();
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

/* ---------------- jobs ---------------- */

let jobRows = [];
let jobSort = { key: "gpu_hours_eff", dir: "desc" };

async function loadJobs() {
  const params = new URLSearchParams({ since_hours: $("jWindow").value, limit: "500" });
  if ($("jRunning").checked) params.set("running_only", "true");
  if ($("jUser").value.trim()) params.set("user", $("jUser").value.trim());
  if ($("jPartition").value) params.set("partition", $("jPartition").value);
  status("loading jobs…");
  const data = await api("/api/jobs?" + params);
  jobRows = data.jobs;
  const sel = $("jPartition");
  const cur = sel.value;
  sel.innerHTML = '<option value="">all</option>' +
    data.partitions.map((p) => '<option value="' + p + '">' + p + "</option>").join("");
  sel.value = cur && data.partitions.includes(cur) ? cur : "";
  const w = data.window;
  $("jMeta").textContent =
    data.count + " jobs · " + tsToDate(w.start) + " → " + tsToDate(w.end) + " UTC";
  renderJobBar();
  renderJobTable(sortJobRows());
  markSort($("jobTable"), jobSort.key, jobSort.dir);
  loaded.jobs = true;
}

function sortJobRows() {
  const rows = jobRows.slice();
  const k = jobSort.key, s = jobSort.dir === "asc" ? 1 : -1;
  rows.sort((a, b) => {
    const va = a[k], vb = b[k];
    if (typeof va === "number" && typeof vb === "number") return (va - vb) * s;
    return String(va || "").localeCompare(String(vb || "")) * s;
  });
  return rows;
}

function renderJobTable(rows) {
  const tb = $("jobTable").querySelector("tbody");
  const shown = rows.slice(0, 300);
  tb.innerHTML = shown.map((j) => `
    <tr class="row" data-job="${j.jobid}">
      <td>${j.jobid}</td><td title="${(j.name || "").replace(/"/g, "&quot;")}">${(j.name || "").slice(0, 40)}</td>
      <td>${j.user}</td><td>${j.partition}</td>
      <td>${stateBadge(j.state)}</td><td>${(j.start || "").slice(0, 16)}</td>
      <td class="num">${j.gpus !== undefined ? j.gpus : "—"}</td>
      <td class="num">${pctBar(j.mean_util)}</td>
      <td class="num">${fmt(j.max_util)}</td>
      <td class="num">${fmt(j.gpu_hours_eff)}</td>
      <td class="num">${j.efficiency !== undefined ? fmt(j.efficiency) : "—"}</td>
      <td class="num">${fmt(j.vram_avg)}</td>
    </tr>`).join("");
  tb.querySelectorAll("tr.row").forEach((tr) =>
    tr.addEventListener("click", () => loadJobDetail(tr.dataset.job)));
  $("jCount").textContent = rows.length + " shown" +
    (rows.length > 300 ? " (of " + jobRows.length + ")" : "");
}

function renderJobBar() {
  const top = jobRows.slice(0, 30);
  const labels = top.map((j) => j.jobid + " · " + j.user).reverse();
  const values = top.map((j) => j.gpu_hours_eff).reverse();
  const util = top.map((j) => j.mean_util).reverse();
  Plotly.newPlot("jobBarPlot", [{
    type: "bar", orientation: "h",
    y: labels,
    x: values,
    marker: {
      color: util.map((u) =>
        u < 40 ? "#ef5350" : u < 75 ? "#ffa726" : "#66bb6a"),
      line: { width: 0 },
    },
    hovertemplate: "<b>%{y}</b><br>eff. GPU-h: %{x:.1f}<extra></extra>",
  }], {
    margin: { l: 130, r: 20, t: 10, b: 30 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: "#dce3f2", size: 11 },
    xaxis: { title: "effective GPU-hours", gridcolor: "#2a3552" },
    yaxis: { autorange: "reversed", gridcolor: "#2a3552" },
  }, PLOT_CFG).then((g) => {
    g.on("plotly_click", (ev) => {
      const label = ev.points[0].y;
      const job = jobRows.find((j) => j.jobid + " · " + j.user === label);
      if (job) loadJobDetail(job.jobid);
    });
  });
}

let jobDetailToken = 0;
async function loadJobDetail(jobid) {
  const token = ++jobDetailToken;
  $("jobDetailCard").style.display = "block";
  $("jobDetailTitle").textContent = "Job " + jobid + " — loading…";
  $("jobDetailCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
  const data = await api("/api/jobs/" + jobid + "?since_hours=" + $("jWindow").value);
  if (token !== jobDetailToken) return;
  setUrl("/job/" + jobid);
  const m = data.metadata || {};
  $("jobDetailTitle").textContent =
    "Job " + jobid + " — " + (m.name || "?") + " (" + (m.user || "?") + ") · " + (m.state || "?");
  const metaBits = [];
  if (m.partition) metaBits.push("partition " + m.partition);
  if (m.node_list) metaBits.push("nodes " + m.node_list);
  if (m.gpus) metaBits.push(m.gpus + "× " + (m.gpu_type || "gpu"));
  if (m.start) metaBits.push("start " + m.start);
  if (m.end) metaBits.push("end " + m.end);
  $("jobDetailMeta").textContent = metaBits.join(" · ");
  const traces = [];
  data.series.utilization.forEach((s, i) => {
    const dev = s.metric.gpu !== undefined ? "GPU " + s.metric.gpu : s.metric.instance;
    traces.push({
      type: "scatter", mode: "lines", name: dev + " util %",
      x: s.values.map((v) => v[0] * 1000), y: s.values.map((v) => v[1]),
      line: { width: 1.5, color: COLORS[i % COLORS.length] },
    });
  });
  data.series.vram.forEach((s, i) => {
    const dev = s.metric.gpu !== undefined ? "GPU " + s.metric.gpu + " VRAM" : "VRAM";
    traces.push({
      type: "scatter", mode: "lines", name: dev + " %",
      x: s.values.map((v) => v[0] * 1000), y: s.values.map((v) => v[1]),
      line: { width: 1, dash: "dot", color: COLORS[(i + 4) % COLORS.length] },
      yaxis: "y2",
    });
  });
  // Lifecycle markers on the utilization axis: open circle at job start,
  // triangle at job end. Only drawn inside the returned window; running
  // jobs have no end marker.
  const w0 = data.window.start * 1000, w1 = data.window.end * 1000;
  if (m.start_epoch && m.start_epoch * 1000 >= w0 && m.start_epoch * 1000 <= w1) {
    traces.push({
      type: "scatter", mode: "markers", name: "Job start",
      x: [m.start_epoch * 1000], y: [102],
      marker: { symbol: "circle-open", size: 11, color: "#dce3f2",
                line: { width: 2 } },
    });
  }
  if (m.end_epoch && m.end_epoch * 1000 >= w0 && m.end_epoch * 1000 <= w1) {
    traces.push({
      type: "scatter", mode: "markers", name: "Job end",
      x: [m.end_epoch * 1000], y: [102],
      marker: { symbol: "triangle-up", size: 11, color: "#ffa726" },
    });
  }
  Plotly.newPlot("jobDetailPlot", traces, {
    margin: { l: 46, r: 46, t: 10, b: 34 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: "#dce3f2", size: 11 },
    showlegend: true, legend: { orientation: "h", y: -0.15 },
    yaxis: { title: "util %", range: [0, 105], gridcolor: "#2a3552" },
    yaxis2: { title: "VRAM %", overlaying: "y", side: "right", range: [0, 105], gridcolor: "#2a3552" },
    xaxis: { type: "date", gridcolor: "#2a3552" },
  }, PLOT_CFG);
}

let jobDebounce = null;
function jobControlsChanged() {
  clearTimeout(jobDebounce);
  jobDebounce = setTimeout(loadJobs, 350);
}
$("jWindow").addEventListener("change", () => { loadJobs(); });
$("jRunning").addEventListener("change", (e) => {
  $("jWindow").disabled = e.target.checked;
  loadJobs();
});
$("jUser").addEventListener("input", jobControlsChanged);
$("jSearch").addEventListener("input", jobControlsChanged);
$("jobTable").querySelectorAll("th[data-k]").forEach((th) =>
  th.addEventListener("click", () => {
    const k = th.dataset.k;
    jobSort = (k === jobSort.key)
      ? { key: k, dir: jobSort.dir === "desc" ? "asc" : "desc" }
      : { key: k, dir: "desc" };
    renderJobTable(sortJobRows());
    markSort($("jobTable"), jobSort.key, jobSort.dir);
  }));

/* ---------------- partitions ---------------- */

let partRows = [];
let partSort = { key: "mean_util", dir: "desc" };

async function loadPartitions() {
  const params = new URLSearchParams({ since_hours: $("pWindow").value });
  if ($("pRunning").checked) params.set("running_only", "true");
  status("loading partitions…");
  const data = await api("/api/partitions?" + params);
  partRows = data.partitions;
  const w = data.window;
  $("pCount").textContent = data.partitions.length + " groups · " +
    tsToDate(w.start) + " → " + tsToDate(w.end) + " UTC";
  renderPartBar();
  renderPartTrend(data.trend);
  renderPartTable();
  markSort($("partTable"), partSort.key, partSort.dir);
  loaded.partitions = true;
}

function renderPartBar() {
  const rows = partRows.slice().sort((a, b) => a.name.localeCompare(b.name));
  Plotly.newPlot("partBarPlot", [{
    type: "bar",
    x: rows.map((p) => p.name),
    y: rows.map((p) => p.mean_util),
    marker: {
      color: rows.map((p) =>
        p.mean_util < 40 ? "#ef5350" : p.mean_util < 75 ? "#ffa726" : "#66bb6a"),
    },
    hovertemplate: "<b>%{x}</b><br>mean %{y:.1f}%<extra></extra>",
  }], {
    margin: { l: 46, r: 20, t: 10, b: 60 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: "#dce3f2", size: 11 },
    yaxis: { title: "mean utilization %", range: [0, 105], gridcolor: "#2a3552" },
    xaxis: { tickangle: -30, automargin: true, gridcolor: "#2a3552" },
  }, PLOT_CFG);
}

function renderPartTrend(trend) {
  const traces = Object.entries(trend).map(([name, values], i) => ({
    type: "scatter", mode: "lines", name,
    x: values.map((v) => v[0] * 1000), y: values.map((v) => v[1]),
    line: { width: 1.5, color: COLORS[i % COLORS.length] },
  }));
  Plotly.newPlot("partTrendPlot", traces, {
    margin: { l: 40, r: 20, t: 10, b: 30 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: "#dce3f2", size: 11 },
    showlegend: true, legend: { orientation: "h", y: -0.18 },
    yaxis: { title: "avg util %", range: [0, 105], gridcolor: "#2a3552" },
    xaxis: { type: "date", gridcolor: "#2a3552" },
  }, PLOT_CFG);
}

function renderPartTable() {
  const tb = $("partTable").querySelector("tbody");
  tb.innerHTML = partRows.map((p) => `
    <tr>
      <td>${p.name}</td>
      <td class="num" title="allocated / total GPUs">${p.gpus_alloc}/${p.gpus_total}</td>
      <td class="num">${fmtInt(p.job_count)}</td>
      <td class="num">${pctBar(p.mean_util)}</td>
      <td class="num">${fmt(p.max_util)}</td>
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
      return String(va || "").localeCompare(String(vb || "")) * s;
    });
    renderPartTable();
    markSort($("partTable"), partSort.key, partSort.dir);
  }));

/* ---------------- nodes ---------------- */

let nodeRows = [];
let nodeSort = { key: "name", dir: "asc" };

async function loadNodes(force = false) {
  const params = new URLSearchParams({ gpu_only: String($("nGpuOnly").checked) });
  if (force) params.set("refresh", "true");
  const btn = $("nRefresh");
  btn.disabled = true;
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
  }
}

function fill(id, values) {
  const sel = $(id);
  if (sel.options.length > 1) return;
  sel.innerHTML = '<option value="">all</option>' +
    values.map((v) => "<option>" + v + "</option>").join("");
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
      '<a class="joblink" data-job="' + j.jobid +
      '" title="' + (j.user || "") + " · " + j.jobid + '">' + j.jobid + "</a>"
    ).join(", ") || "—";
    return `
    <tr class="row" data-node="${n.name}" style="${busy ? "" : "opacity:.55"}">
      <td>${n.name}</td>
      <td>${n.gpu_type || "—"}</td>
      <td class="num" title="allocated / total GPUs">${n.gpus_alloc !== undefined ? n.gpus_alloc : 0}/${n.gpus}</td>
      <td class="num">${u === null ? "idle" : pctBar(u)}</td>
      <td class="num">${n.current_vram === null ? "—" : fmt(n.current_vram)}</td>
      <td class="num" title="allocated / total CPUs">${n.cpus_alloc !== undefined ? n.cpus_alloc : 0}/${n.cpus}</td>
      <td class="small">${jobs}</td>
    </tr>`;
  }).join("");
  tb.querySelectorAll("tr.row").forEach((tr) =>
    tr.addEventListener("click", (e) => {
      const link = e.target.closest("a.joblink");
      if (link) { e.stopPropagation(); openJob(link.dataset.job); return; }
      loadNodeDetail(tr.dataset.node);
    }));
  markSort($("nodeTable"), nodeSort.key, nodeSort.dir);
  $("nCount").textContent = rows.length + " / " + nodeRows.length;
}

let nodeDetailToken = 0;
let nodeDetailName = null;
async function loadNodeDetail(name) {
  nodeDetailName = name;
  const token = ++nodeDetailToken;
  $("nodeDetailCard").style.display = "block";
  $("nodeDetailTitle").textContent = "Node " + name + " — loading…";
  $("nodeDetailCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
  const data = await api("/api/nodes/" + name + "?view=" + $("ndWindow").value);
  if (token !== nodeDetailToken) return;
  setUrl("/node/" + name);
  $("nodeDetailTitle").textContent = "Node " + name;
  const utilTraces = [];
  data.series.utilization.forEach((s, i) => {
    const dev = "GPU " + (s.metric.gpu !== undefined ? s.metric.gpu : "?");
    const job = s.metric.slurmjobid || "";
    utilTraces.push({
      type: "scatter", mode: "lines", name: dev + (job ? " (job " + job + ")" : ""),
      x: s.values.map((v) => v[0] * 1000), y: s.values.map((v) => v[1]),
      line: { width: 1.5, color: COLORS[i % COLORS.length] },
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
      line: { width: 1, dash: "dot", color: COLORS[i % COLORS.length] },
    });
  });
  const base = {
    margin: { l: 46, r: 20, t: 10, b: 34 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { color: "#dce3f2", size: 11 },
    showlegend: true, legend: { orientation: "h", y: -0.2 },
    yaxis: { title: "%", range: [0, 105], gridcolor: "#2a3552" },
    xaxis: { type: "date", gridcolor: "#2a3552" },
  };
  Plotly.newPlot("nodeDetailUtilPlot", utilTraces, base, PLOT_CFG);
  Plotly.newPlot("nodeDetailVramPlot", vramTraces, base, PLOT_CFG);
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
      return String(va || "").localeCompare(String(vb || "")) * s;
    });
    renderNodeTable();
  }));

/* ---------------- deep links ----------------
 * Shareable URLs: /job/<id> opens the Jobs tab with that job's detail,
 * /node/<name> opens the Nodes tab with that node's detail, /jobs,
 * /partitions and /nodes open the plain tabs. The server serves the SPA
 * shell for all of them; this code restores the view from the path and
 * keeps the URL in sync via history.pushState. */

function setUrl(path) {
  if (location.pathname + location.search !== path) {
    history.pushState({ path }, "", path);
  }
}

function openJob(jobid) {
  showTab("jobs").then(() => loadJobDetail(jobid)).catch(() => {});
}

function restoreFromUrl() {
  let m = location.pathname.match(/^\/job\/([^/]+)\/?$/);
  if (m) { showTab("jobs").then(() => loadJobDetail(m[1])).catch(() => {}); return; }
  m = location.pathname.match(/^\/node\/([^/]+)\/?$/);
  if (m) { showTab("nodes").then(() => loadNodeDetail(m[1])).catch(() => {}); return; }
  if (location.pathname === "/jobs") { showTab("jobs"); return; }
  if (location.pathname === "/partitions") { showTab("partitions"); return; }
  if (location.pathname === "/nodes") { showTab("nodes"); return; }
}

window.addEventListener("popstate", restoreFromUrl);
document.querySelectorAll("nav.tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    showTab(b.dataset.tab);
    setUrl("/" + b.dataset.tab);
  }));

/* ---------------- init ---------------- */

checkHealth();
if (location.pathname === "/" || location.pathname === "") {
  loadJobs().catch(() => {});
} else {
  restoreFromUrl();
}
