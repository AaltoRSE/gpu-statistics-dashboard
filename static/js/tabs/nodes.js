/* Nodes tab: the node table (with client-side search/type/busy filters)
 * and the per-node GPU time-series detail.
 *
 * See tabs/jobs.js for why this module and core/router.js import each
 * other. */
"use strict";

import { $, isPlainClick } from "../core/dom.js";
import {
  fmt, pctBar, chipList, escapeHtml, html, raw, compareStrings,
  jobLink, nodeLink, partitionLink,
} from "../core/format.js";
import { setResultsLoading, showPanelError, panelOk } from "../core/panel.js";
import { renderPlot, plotTheme } from "../core/plot.js";
import { api } from "../core/api.js";
import { loaded, setUrl, openJob, openNode, openPartition } from "../core/router.js";
import { createTable } from "../core/table.js";

let nodeRows = [];
let nodesToken = 0;
const nodeFilters = { search: "", gputype: "", busy: false, gpuOnly: true };

export async function loadNodes(force = false) {
  const token = ++nodesToken;
  const params = new URLSearchParams({ gpu_only: String($("nGpuOnly").checked) });
  if (force) params.set("refresh", "true");
  const btn = $("nRefresh");
  btn.disabled = true;
  setResultsLoading("nodesResults", true);
  try {
    const data = await api("/api/nodes?" + params);
    if (token !== nodesToken) return; // a newer request supersedes this one
    panelOk("nodesResults");
    nodeRows = data.nodes;
    const t = new Intl.DateTimeFormat("en-GB", {
      timeZone: "Europe/Helsinki", hour: "2-digit", minute: "2-digit",
      hour12: false, timeZoneName: "short",
    }).format(data.time * 1000);
    $("nMeta").textContent = data.count + " GPU nodes · snapshot " + t;
    fill("nGpuType", [...new Set(nodeRows.map((n) => n.gpu_type).filter(Boolean))].sort());
    renderNodeTable();
    loaded.nodes = true;
  } catch (e) {
    if (token === nodesToken)
      showPanelError("nodesResults", e, () => loadNodes(), "the node list");
  } finally {
    if (token === nodesToken) {
      btn.disabled = false;
      setResultsLoading("nodesResults", false);
    }
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

function nodeRowHtml(n) {
  const u = n.current_util;
  const busy = u !== null && u > 0;
  // jobLink/partitionLink/escapeHtml outputs below are already safe HTML
  // (or already-escaped text); raw() marks them so html`` doesn't escape
  // them a second time. Everything else interpolates as plain text.
  const jobs = raw(chipList((n.active_jobs || []).map((j) => jobLink(j.jobid))));
  const rawName = n.name;
  const gpuType = n.gpu_type
    ? raw(n.gpu_group ? partitionLink(n.gpu_group, n.gpu_type) : escapeHtml(n.gpu_type))
    : "—";
  const gpusAlloc = n.gpus_alloc !== undefined ? n.gpus_alloc : 0;
  const vram = n.current_vram === null ? "—" : fmt(n.current_vram);
  const cpusAlloc = n.cpus_alloc !== undefined ? n.cpus_alloc : 0;
  return html`
    <tr class="row" data-node="${rawName}">
      <td>${raw(nodeLink(rawName))}</td>
      <td>${gpuType}</td>
      <td class="num" title="allocated / total GPUs">${raw(allocSplit(gpusAlloc, n.gpus))}</td>
      <td class="num${busy ? "" : " muted-cell"}">${u === null ? "idle" : raw(pctBar(u))}</td>
      <td class="num">${vram}</td>
      <td class="num" title="allocated / total CPUs">${raw(allocSplit(cpusAlloc, n.cpus))}</td>
      <td class="small chip-cell">${jobs}</td>
    </tr>`;
}

function allocSplit(alloc, total) {
  return html`${alloc}/${total}`;
}

function nodeRowClick(e, tr) {
  const link = e.target.closest("a.joblink");
  if (link) {
    e.stopPropagation();
    if (!isPlainClick(e)) return;
    e.preventDefault();
    openJob(link.dataset.job, { kind: "node", node: tr.dataset.node });
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
  loadNodeDetail(tr.dataset.node);
}

function nodeTableEmptyMessage() {
  const nodeFiltered = nodeFilters.search || nodeFilters.gputype || nodeFilters.busy;
  return {
    text: nodeFiltered ? "No nodes match the current filters." : "No GPU nodes in this snapshot.",
    resetLabel: nodeFiltered ? "reset filters" : null,
    onReset: () => {
      $("nSearch").value = ""; $("nGpuType").value = ""; $("nBusy").checked = false;
      nodeFilters.search = ""; nodeFilters.gputype = ""; nodeFilters.busy = false;
      renderNodeTable();
    },
  };
}

const nodeTable = createTable({
  el: $("nodeTable"),
  columns: [
    { key: "name", type: "text" }, { key: "gpu_type", type: "text" },
    { key: "gpus_alloc", type: "number" },
    { key: "current_util", type: "number" }, { key: "current_vram", type: "number" },
    { key: "cpus_alloc", type: "number" },
  ],
  defaultSort: { key: "name", dir: "asc" },
  renderRow: nodeRowHtml,
  onRowClick: nodeRowClick,
  emptyMessage: nodeTableEmptyMessage,
});

function renderNodeTable() {
  const rows = filteredNodes();
  nodeTable.setRows(rows);
  $("nCount").textContent = rows.length + " / " + nodeRows.length;
}

let nodeDetailToken = 0;
export let nodeDetailName = null;
export let nodeDetailData = null; // raw API payload; traces rebuild per theme
let nodeDetailJob = "";     // filter traces to one job's GPUs

export async function loadNodeDetail(name) {
  nodeDetailName = name;
  const token = ++nodeDetailToken;
  $("nodeDetailResults").style.display = "block";
  $("nodeDetailTitle").textContent = "Node " + name;
  setResultsLoading("nodeDetailResults", true);
  clearNodeTableHighlight();
  try {
    const data = await api("/api/nodes/" + encodeURIComponent(name) + "?view=" + $("ndWindow").value);
    if (token !== nodeDetailToken) return;
    panelOk("nodeDetailResults");
    setUrl("/node/" + encodeURIComponent(name));
    nodeDetailData = data;
    nodeDetailJob = ""; // a window/node change invalidates the job choice
    fillNodeJobSelect(data);
    renderNodeDetail(data, name);
    // Land the deep link, not a thousand pixels above it (PLAN-1 2.2):
    // scrolling before the detail panel's real content (charts, job
    // select) has rendered targets whatever height its loading skeleton
    // happened to have, not where the panel actually ends up. block:
    // "start" (not "nearest") always aligns the panel's own top edge to
    // the viewport, regardless of whether it already overlapped it.
    $("nodeDetailResults").scrollIntoView({ behavior: "smooth", block: "start" });
    highlightNodeRow(name);
  } catch (e) {
    if (token === nodeDetailToken)
      showPanelError("nodeDetailResults", e, () => loadNodeDetail(name), "the node detail");
  } finally {
    if (token === nodeDetailToken) setResultsLoading("nodeDetailResults", false);
  }
}

function highlightNodeRow(name) {
  clearNodeTableHighlight();
  const tr = $("nodeTable").querySelector('tr.row[data-node="' + name + '"]');
  // No scrollIntoView here: the detail panel's own scroll above is the
  // actual navigation target (that's the whole fix for 2.2) — this is
  // just a visual marker for whoever scrolls back up to the table, not a
  // second place to land.
  if (tr) tr.classList.add("sel");
}

function clearNodeTableHighlight() {
  const tb = $("nodeTable").querySelector("tbody");
  if (tb) tb.querySelectorAll("tr.row.sel").forEach((t) => t.classList.remove("sel"));
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

// One trace per (GPU, job) series, each its own color: the "gpu" label is
// job-local, not a physical device id (api/nodes.py's comment on the
// sibling VRAM query says it plainly — every job's own cgroup sees its
// first GPU as "gpu 0" regardless of which physical card it actually
// landed on), so several different jobs can share a "GPU 0" label over
// the course of a window. Giving every (gpu, job) segment its own line
// and color — rather than merging same-label segments into one shared
// line — is what makes that distinction visible: a job with several GPUs
// shows several differently-colored lines, and a "gpu" label reused by
// different jobs over time shows one line per job instead of one
// misleadingly continuous line spanning unrelated jobs.
// A MIG slice's "gpu" label is its full device UUID ("MIG-2879d28e-ee0c-
// 5e14-95fe-4cd17b0f7566"), not a small index — shorten it to "MIG-" plus
// its first 8 hex chars for the legend/axis label, which is already
// unique across the handful of MIG slices one node can have; the full
// value stays in the hover text.
function shortGpuId(gpu) {
  return typeof gpu === "string" && gpu.startsWith("MIG-") && gpu.length > 12
    ? gpu.slice(0, 12)
    : gpu;
}

function buildDeviceTraces(seriesList, keep, colors, lineExtra) {
  const kept = seriesList.filter((s) => keep(s) && s.values && s.values.length);
  const sorted = [...kept].sort((a, b) => {
    const g = compareStrings(String(a.metric.gpu ?? "?"), String(b.metric.gpu ?? "?"));
    return g !== 0 ? g : compareStrings(a.metric.slurmjobid || "", b.metric.slurmjobid || "");
  });
  return sorted.map((s, i) => {
    const gpu = s.metric.gpu !== undefined ? s.metric.gpu : "?";
    const job = s.metric.slurmjobid || "";
    const label = "GPU " + shortGpuId(gpu) + (job ? " · job " + job : "");
    return {
      type: "scatter", mode: "lines", name: label,
      x: s.values.map((v) => v[0] * 1000), y: s.values.map((v) => v[1]),
      line: Object.assign({ width: 2, color: colors[i % colors.length] }, lineExtra),
      hovertemplate: "<b>" + label + "</b><br>%{y:.1f}<extra></extra>",
    };
  });
}

export function renderNodeDetail(data, name) {
  $("nodeDetailTitle").textContent =
    "Node " + name + (nodeDetailJob ? " · job " + nodeDetailJob : "");
  const th = plotTheme();
  const keep = (s) => !nodeDetailJob || s.metric.slurmjobid === nodeDetailJob;
  const utilTraces = buildDeviceTraces(data.series.utilization, keep, th.colors, {});
  const vramTraces = buildDeviceTraces(data.series.vram, keep, th.colors, { dash: "dot" });
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
