/* Partitions tab: utilization/occupancy bar charts, the trend chart, the
 * partition table and the VRAM-distribution chart. The VRAM panel loads
 * independently under its own results-panel so a slow VRAM query never
 * blocks the rest of the tab.
 *
 * See tabs/jobs.js for why this module and core/router.js import each
 * other. */
"use strict";

import { $ } from "../core/dom.js";
import { escapeHtml, fmtInt, pctBar, compareStrings, tsToDate, partitionLink } from "../core/format.js";
import { setResultsLoading, showPanelError, panelOk } from "../core/panel.js";
import { renderPlot, plotTheme, partBarColor } from "../core/plot.js";
import { api, status } from "../core/api.js";
import { loaded, setUrl, openPartition } from "../core/router.js";
import { createTable } from "../core/table.js";

let partRows = [];
export let partTrendData = {};
export let selectedPartition = ""; // deep-linked or chosen partition; "" = all
let partitionsToken = 0;

export function setSelectedPartition(name) {
  selectedPartition = name || "";
}

export function applyPartitionSelection(name) {
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
  renderPartTrend(partTrendData);
  setUrl(sel.value ? "/partition/" + encodeURIComponent(sel.value) : "/partitions");
}

export async function loadPartitions() {
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
    ($("pRunning").checked
      ? "live jobs · instantaneous"
      : tsToDate(w.start) + " → " + tsToDate(w.end) + " UTC");
  renderPartBar();
  renderPartOccupancy();
  partTrendData = data.trend;
  applyPartitionSelection(selectedPartition);
  renderPartTable();
  loaded.partitions = true;
  // The summary panel is unblocked as soon as its response renders; the
  // VRAM distribution then fetches independently under its own panel.
  setResultsLoading("partitionsResults", false);
  if (token !== partitionsToken) return;
  await loadVram().catch(() => {});
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

function partRowHtml(p) {
  return `
    <tr class="row" data-partition="${escapeHtml(p.name)}">
      <td>${partitionLink(p.name)}</td>
      <td class="num" title="allocated / total GPUs">${escapeHtml(p.gpus_alloc)}/${escapeHtml(p.gpus_total)}</td>
      <td class="num">${escapeHtml(fmtInt(p.job_count))}</td>
      <td class="num">${pctBar(p.mean_util)}</td>
    </tr>`;
}

function partRowClick(e, tr) {
  openPartition(tr.dataset.partition);
}

function partTableEmptyMessage() {
  return { text: "No partitions in this window.", resetLabel: null };
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

/* ---------------- VRAM distribution ----------------
 * VRAM usage of jobs in the window, binned by per-job peak VRAM and
 * weighted by allocated GPU-hours. The dual utilization-range slider
 * refilters client-side (no refetch); window / running-only refetch. */

export let vramJobs = [];
let vramTotal = 0; // candidates in the window, before the backend cap
let vramToken = 0;
let vramGpuType = "";

export async function loadVram() {
  const token = ++vramToken;
  // The VRAM fetch blurs only the VRAM panel (vramResults), never the whole
  // partitions tab: window / running-only / partition changes here must not
  // freeze the other graphs.
  const origin = partitionsToken;
  setResultsLoading("vramResults", true);
  status("loading VRAM distribution…");
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
    // (same treatment as the partition bar charts).
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
  if (loaded.partitions) loadVram().catch(() => {});
}

export { renderPartBar, renderPartOccupancy, renderPartTrend, renderVram };
