/* Groups tab: GPU efficiency per research group / department / school.
 *
 * One fetch per window+level+running change; the school filter and the
 * search box filter that response locally (no fetch) and keep the URL in
 * sync (/groups?school=&level=&running=). The Unaffiliated and Unresolved
 * rows always render — an empty row means nobody is in it, never that
 * nobody exists. The coverage banner discloses how much of the window's
 * job owners the directory could classify; a school-colored bar chart
 * shows the top 30 groups by mean utilization, and clicking a row opens
 * the member drill-down whose users link to the Users tab.
 *
 * See tabs/users.js for why this module and core/router.js import each
 * other. */
"use strict";

import { $, isPlainClick } from "../core/dom.js";
import {
  fmt, fmtInt, pctBar, escapeHtml, chipList, html, raw, tsToDate, userLink,
} from "../core/format.js";
import { setResultsLoading, showPanelError, panelOk } from "../core/panel.js";
import { api } from "../core/api.js";
import { loaded, setUrl, openUser } from "../core/router.js";
import { createTable } from "../core/table.js";
import { plotTheme, renderPlot } from "../core/plot.js";

let groupRows = [];        // the /api/groups rows for the current fetch
let schools = [];          // [{code, short, full}] from the response
let pendingSchool = null;  // deep-linked school before options exist
let groupsToken = 0;
let membersToken = 0;
let selectedGroupId = null;

export async function loadGroups() {
  const token = ++groupsToken;
  setResultsLoading("groupsResults", true, "Loading group efficiency…");
  const params = new URLSearchParams({ since_hours: $("gWindow").value });
  if ($("gRunning").checked) params.set("running_only", "true");
  params.set("level", $("gLevel").value);
  try {
    const data = await api("/api/groups?" + params);
    if (token !== groupsToken) return;
    panelOk("groupsResults");
    groupRows = data.groups;
    schools = data.schools;
    renderSchoolOptions();
    const w = data.window;
    $("gMetaCount").textContent = data.count + " groups";
    $("gMeta").textContent = tsToDate(w.start) + " → " + tsToDate(w.end);
    renderCoverage(data.coverage);
    renderGroupsBar();
    renderGroupTable();
    syncUrl();
    loaded.groups = true;
  } catch (e) {
    if (token === groupsToken)
      showPanelError("groupsResults", e, loadGroups, "the group list");
  } finally {
    if (token === groupsToken) setResultsLoading("groupsResults", false);
  }
}

/* Deep-link state (/groups?school=&level=&running=) — router.js calls
 * this BEFORE the tab's first load so the controls' values are what the
 * first fetch uses. */
export function prefillFromUrl(params) {
  if (params.get("school")) pendingSchool = params.get("school");
  if (params.get("level") === "department") $("gLevel").value = "department";
  if (params.get("running") === "true") $("gRunning").checked = true;
}

function syncUrl() {
  const params = new URLSearchParams();
  if ($("gSchool").value) params.set("school", $("gSchool").value);
  if ($("gLevel").value !== "unit") params.set("level", $("gLevel").value);
  if ($("gRunning").checked) params.set("running", "true");
  const q = params.toString();
  setUrl("/groups" + (q ? "?" + q : ""));
}

// One option per distinct short name (T5/T6 both map to Other), order
// kept stable; a selection — including a just-deep-linked one, held in
// pendingSchool until the options exist — survives the rebuild.
function renderSchoolOptions() {
  const sel = $("gSchool");
  const shorts = [...new Set(schools.map((s) => s.short))];
  sel.innerHTML = '<option value="">all schools</option>' +
    shorts.map((s) =>
      '<option value="' + escapeHtml(s) + '">' + escapeHtml(s) + "</option>"
    ).join("");
  const want = pendingSchool !== null ? pendingSchool : sel.value;
  pendingSchool = null;
  if (shorts.includes(want)) sel.value = want;
}

function filteredGroups() {
  const q = $("gSearch").value.trim().toLowerCase();
  const school = $("gSchool").value;
  return groupRows.filter((g) => {
    if (school && g.school_code !== school) return false;
    if (!q) return true;
    return [g.group_name, g.dept_name, g.dept_code]
      .some((v) => v && v.toLowerCase().includes(q));
  });
}

function groupRowHtml(g) {
  return html`
    <tr class="row" data-gid="${g.group_id}">
      <td><b>${g.group_name}</b></td>
      <td>${g.school_code || "—"}</td>
      <td class="num">${fmtInt(g.users)}</td>
      <td class="num">${fmtInt(g.jobs)}</td>
      <td class="num">${fmtInt(g.running_jobs)}</td>
      <td class="num">${raw(pctBar(g.mean_util))}</td>
      <td class="num">${fmt(g.util_gpu_hours, 2)}</td>
      <td class="num">${fmt(g.gpu_hours, 2)}</td>
      <td class="num">${fmt(g.vram_avg)}</td>
      <td class="num">${fmtInt(g.low_eff_jobs)}</td>
    </tr>`;
}

function groupTableEmptyMessage() {
  const searched = $("gSearch").value.trim() || $("gSchool").value;
  return {
    text: searched
      ? "No groups match the current filters."
      : "No GPU activity by any group in this window.",
    resetLabel: searched ? "clear filters" : null,
    onReset: () => {
      $("gSearch").value = "";
      $("gSchool").value = "";
      renderGroupTable();
      syncUrl();
    },
  };
}

const groupTable = createTable({
  el: $("groupTable"),
  columns: [
    { key: "group_name", type: "text" }, { key: "school_code", type: "text" },
    { key: "users", type: "number" }, { key: "jobs", type: "number" },
    { key: "running_jobs", type: "number" },
    { key: "mean_util", type: "number" },
    { key: "util_gpu_hours", type: "number" },
    { key: "gpu_hours", type: "number" }, { key: "vram_avg", type: "number" },
    { key: "low_eff_jobs", type: "number" },
  ],
  defaultSort: { key: "util_gpu_hours", dir: "desc" },
  renderRow: groupRowHtml,
  onRowClick: groupRowClick,
  emptyMessage: groupTableEmptyMessage,
});

function renderGroupTable() {
  const rows = filteredGroups();
  groupTable.setRows(rows);
  $("gCount").textContent = rows.length + " shown";
}

// ---- coverage banner -------------------------------------------------
// Classification completeness, always shown: the mapping quality is part
// of reading the table. Lookup failures and unmapped codes tint it — a
// failed user's activity is in no row at all, and an unmapped code is a
// one-line org_units.conf edit away from a proper name.
function renderCoverage(coverage) {
  const el = $("groupsCoverage");
  const bits = [
    coverage.affiliated + " of " + coverage.users +
    " job owners mapped to a group or department",
  ];
  if (coverage.unaffiliated)
    bits.push(coverage.unaffiliated + " unaffiliated");
  if (coverage.unresolved)
    bits.push(coverage.unresolved + " unknown to the directory");
  if (coverage.failed)
    bits.push(coverage.failed + " lookup failures — their activity is " +
      "missing from every figure below");
  if (coverage.unmapped_codes.length)
    bits.push("unmapped unit code(s) " + coverage.unmapped_codes.join(", ") +
      " — add a name to org_units.conf to label them");
  el.textContent = "Group coverage: " + bits.join(" · ");
  el.classList.toggle("warn",
    !!(coverage.failed || coverage.unmapped_codes.length));
  el.hidden = false;
}

// ---- mean-utilization bar chart, colored by school --------------------
// Stable per-school colors (config order, one hue per distinct short
// name); rows outside any school (unaffiliated) draw in the idle gray.
function schoolColorMap() {
  const th = plotTheme();
  const map = { "": th.idle };
  [...new Set(schools.map((s) => s.short))].forEach((short, i) => {
    map[short] = th.colors[i % th.colors.length];
  });
  return map;
}

export function renderGroupsBar() {
  const rows = filteredGroups().filter((g) => g.jobs > 0)
    .sort((a, b) => b.mean_util - a.mean_util).slice(0, 30).reverse();
  const colors = schoolColorMap();
  const th = plotTheme();
  $("gChartNote").textContent = rows.length
    ? "top " + rows.length + " of " + groupRows.length + " by mean util %" : "";
  $("groupsBarEmpty").hidden = !!rows.length;
  $("groupsSchoolLegend").innerHTML = [...new Set(rows.map((g) =>
    g.school_code || ""))].map((s) =>
    '<span class="legend-key" style="background:' + colors[s] +
    '"></span> ' + escapeHtml(s || "no school")
  ).join(" ");
  renderPlot("groupsBarPlot", [{
    type: "bar",
    orientation: "h",
    x: rows.map((g) => g.mean_util),
    y: rows.map((g) => g.group_name),
    marker: { color: rows.map((g) => colors[g.school_code || ""]) },
    hovertemplate: rows.map((g) =>
      "<b>" + escapeHtml(g.group_name) + "</b><br>mean %{x:.1f}%<br>" +
      escapeHtml(g.users + " users · " + g.jobs + " jobs · " +
        g.util_gpu_hours + " GPU-h<extra></extra>")),
  }], {
    margin: { l: 10, r: 20, t: 10, b: 40 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: th.font,
    yaxis: { automargin: true, gridcolor: th.grid, fixedrange: true },
    xaxis: { title: "mean utilization %", range: [0, 105],
             gridcolor: th.grid, fixedrange: true },
    dragmode: false,
  });
}

// ---- the members drill-down ------------------------------------------
function groupRowClick(e, tr) {
  loadGroupMembers(tr.dataset.gid);
}

async function loadGroupMembers(gid) {
  const token = ++membersToken;
  selectedGroupId = gid;
  const row = groupRows.find((g) => g.group_id === gid);
  $("groupMembersResults").style.display = "block";
  $("groupMembersTitle").textContent =
    "Members · " + (row ? row.group_name : gid) + " · last " +
    $("gWindow").value / 24 + " d";
  setResultsLoading("groupMembersResults", true, "Loading group members…");
  const params = new URLSearchParams({ since_hours: $("gWindow").value });
  if ($("gRunning").checked) params.set("running_only", "true");
  params.set("level", $("gLevel").value);
  try {
    const data = await api("/api/groups/" + encodeURIComponent(gid) +
      "/users?" + params);
    if (token !== membersToken) return;
    panelOk("groupMembersResults");
    memberTable.setRows(data.users);
  } catch (e) {
    if (token === membersToken)
      showPanelError("groupMembersResults", e,
        () => loadGroupMembers(gid), "the member list");
  } finally {
    if (token === membersToken) setResultsLoading("groupMembersResults", false);
  }
}

function memberRowHtml(m) {
  return html`
    <tr class="row" data-user="${m.user}">
      <td>${raw(userLink(m.user))}</td>
      <td>${m.unit_code || "—"}</td>
      <td class="num">${fmtInt(m.jobs)}</td>
      <td class="num">${fmtInt(m.running_jobs)}</td>
      <td class="num">${raw(pctBar(m.mean_util))}</td>
      <td class="num">${fmt(m.util_gpu_hours, 2)}</td>
      <td class="num">${fmt(m.vram_avg)}</td>
      <td class="small chip-cell">${raw(chipList((m.extra_units || [])
        .map(escapeHtml)))}</td>
    </tr>`;
}

function memberRowClick(e, tr) {
  const link = e.target.closest("a.userlink");
  if (link) {
    e.stopPropagation();
    if (!isPlainClick(e)) return;
    e.preventDefault();
    openUser(link.dataset.user);
    return;
  }
  openUser(tr.dataset.user);
}

function membersEmptyMessage() {
  return {
    text: "No members with GPU activity in this window.",
    resetLabel: null,
  };
}

const memberTable = createTable({
  el: $("memberTable"),
  columns: [
    { key: "user", type: "text" }, { key: "unit_code", type: "text" },
    { key: "jobs", type: "number" }, { key: "running_jobs", type: "number" },
    { key: "mean_util", type: "number" },
    { key: "util_gpu_hours", type: "number" },
    { key: "vram_avg", type: "number" },
    { key: "extra_units", type: "text" },
  ],
  defaultSort: { key: "util_gpu_hours", dir: "desc" },
  renderRow: memberRowHtml,
  onRowClick: memberRowClick,
  emptyMessage: membersEmptyMessage,
});

$("gWindow").addEventListener("change", () => {
  loadGroups();
  if (selectedGroupId) loadGroupMembers(selectedGroupId);
});
$("gLevel").addEventListener("change", () => {
  loadGroups();
  if (selectedGroupId) loadGroupMembers(selectedGroupId);
});
$("gRunning").addEventListener("change", () => {
  loadGroups();
  if (selectedGroupId) loadGroupMembers(selectedGroupId);
});
$("gSchool").addEventListener("change", () => {
  renderGroupsBar();
  renderGroupTable();
  syncUrl();
});
$("gRefresh").addEventListener("click", loadGroups);
$("gSearch").addEventListener("input", () => {
  renderGroupsBar();
  renderGroupTable();
});
