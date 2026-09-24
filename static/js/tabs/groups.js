/* Groups tab: GPU efficiency per professor group / department / school.
 *
 * One fetch per window+level+running change; the school filter and the
 * search box filter that response locally (no fetch) and keep the URL in
 * sync (/groups?school=&level=&running=). A row is one professor's
 * research group (the AD unit their prof_groups.conf row names); the
 * Unaffiliated and Unresolved rows always render — an empty row means
 * nobody is in it, never that nobody exists. The coverage banner
 * discloses how much of the window's job owners the directory could
 * classify; a school-colored bar chart shows the top 30 groups by mean
 * utilization, and clicking a row opens the member drill-down whose
 * users link to the Users tab.
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

// The bucket every schoolless row reads as: the backend sends "Other"
// for a department code that matches no school prefix and null for rows
// with no department at all — normalizing both here keeps the filter,
// table, legend and colors in agreement.
const OTHER = "Other";

// Aalto brand colors per school
// (brand.aalto.fi/en/brand/visual-guidelines/colours), keyed by the
// [schools] short names prof_groups.conf carries (tools/build_prof_groups.py
// SCHOOL_BY_PREFIX generates them). A school missing from this map falls
// back to the theme palette.
const SCHOOL_COLORS = {
  ENG: "#DC6ADE", ELEC: "#A987FF", CHEM: "#5DD089",
  ARTS: "#FFC341", BIZ: "#9BD84C", SCI: "#FF8D4F",
};

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
    groupRows = data.groups.map((g) =>
      ({ ...g, school_code: g.school_code || OTHER }));
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
  // pre-v2 deep links said level=unit; the professor-group level is the
  // same idea under its new name
  if (params.get("level") === "unit") $("gLevel").value = "group";
  if (params.get("running") === "true") $("gRunning").checked = true;
}

function syncUrl() {
  const params = new URLSearchParams();
  if ($("gSchool").value) params.set("school", $("gSchool").value);
  if ($("gLevel").value !== "group") params.set("level", $("gLevel").value);
  if ($("gRunning").checked) params.set("running", "true");
  const q = params.toString();
  setUrl("/groups" + (q ? "?" + q : ""));
}

// One option per distinct short name, plus an Other option at the end
// when any row carries it — so a legend entry can also be filtered (and
// deep-linked with ?school=Other). A selection — including a
// just-deep-linked one, held in pendingSchool until the options exist —
// survives the rebuild.
function renderSchoolOptions() {
  const sel = $("gSchool");
  const shorts = [...new Set(schools.map((s) => s.short))];
  const values = groupRows.some((g) => g.school_code === OTHER)
    ? [...shorts, OTHER]
    : shorts;
  sel.innerHTML = '<option value="">all schools</option>' +
    values.map((s) =>
      '<option value="' + escapeHtml(s) + '">' + escapeHtml(s) + "</option>"
    ).join("");
  const want = pendingSchool !== null ? pendingSchool : sel.value;
  pendingSchool = null;
  if (values.includes(want)) sel.value = want;
}

function filteredGroups() {
  const q = $("gSearch").value.trim().toLowerCase();
  const school = $("gSchool").value;
  return groupRows.filter((g) => {
    if (school && g.school_code !== school) return false;
    if (!q) return true;
    return [g.group_name, g.leader_name, g.dept_name, g.dept_code]
      .some((v) => v && v.toLowerCase().includes(q));
  });
}

// Like userLink but labeled with the leader's display name (the anchor
// carries the same userlink class, so row clicks route to the Users tab).
function leaderCell(g) {
  if (!g.leader) return "—";
  return raw(html`<a class="entity-link userlink"
    href="/user/${encodeURIComponent(g.leader)}" data-user="${g.leader}"
    title="open ${g.leader} in the Users tab">${g.leader_name || g.leader}</a>`);
}

function groupRowHtml(g) {
  return html`
    <tr class="row" data-gid="${g.group_id}">
      <td><b>${g.group_name}</b></td>
      <td>${leaderCell(g)}</td>
      <td>${g.school_code}</td>
      <td class="num">${fmtInt(g.users)}</td>
      <td class="num">${fmtInt(g.jobs)}</td>
      <td class="num">${fmtInt(g.running_jobs)}</td>
      <td class="num">${raw(pctBar(g.mean_util))}</td>
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
    { key: "group_name", type: "text" }, { key: "leader_name", type: "text" },
    { key: "school_code", type: "text" },
    { key: "users", type: "number" }, { key: "jobs", type: "number" },
    { key: "running_jobs", type: "number" },
    { key: "mean_util", type: "number" },
    { key: "gpu_hours", type: "number" }, { key: "vram_avg", type: "number" },
    { key: "low_eff_jobs", type: "number" },
  ],
  defaultSort: { key: "gpu_hours", dir: "desc" },
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
// of reading the table. Lookup failures tint it — a failed user's
// activity is in no row at all. (Department-only users are not a defect:
// they are the honest bucket for people whose department has no
// professor group in prof_groups.conf.)
function renderCoverage(coverage) {
  const el = $("groupsCoverage");
  const bits = [
    coverage.in_prof_group + " of " + coverage.users +
    " job owners in a professor group",
  ];
  if (coverage.dept_only)
    bits.push(coverage.dept_only + " department-only");
  if (coverage.unaffiliated)
    bits.push(coverage.unaffiliated + " unaffiliated");
  if (coverage.unresolved)
    bits.push(coverage.unresolved + " unknown to the directory");
  if (coverage.failed)
    bits.push(coverage.failed + " lookup failures — their activity is " +
      "missing from every figure below");
  el.textContent = "Group coverage: " + bits.join(" · ");
  el.classList.toggle("warn", !!coverage.failed);
  el.hidden = false;
}

// ---- mean-utilization bar chart, colored by school --------------------
// Aalto brand colors per school; a school missing from the brand set
// falls back to the theme palette, and the schoolless Other bucket draws
// in the theme's neutral gray — visible on both themes and clearly apart
// from the six saturated brand hues.
function schoolColorMap() {
  const th = plotTheme();
  const map = { [OTHER]: th.other };
  let i = 0;
  [...new Set(schools.map((s) => s.short))].forEach((short) => {
    map[short] = SCHOOL_COLORS[short] || th.colors[i++ % th.colors.length];
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
  // Legend entries in config school order, Other last — only schools
  // present in the current filtered rows.
  const present = new Set(rows.map((g) => g.school_code));
  const entries = schools.map((s) => s.short).filter((s) => present.has(s));
  if (present.has(OTHER)) entries.push(OTHER);
  $("groupsSchoolLegend").innerHTML = entries.map((s) =>
    '<span class="legend-key" style="background:' + colors[s] +
    '"></span> ' + escapeHtml(s)
  ).join(" ");
  // The chart grows with the row count — the .chart default's fixed
  // 340px cannot hold 30 labeled bars (30 rows ≈ 790px). Set on both
  // the element and the layout so the box and the render agree.
  const h = Math.max(340, rows.length * 24 + 70);
  renderPlot("groupsBarPlot", [{
    type: "bar",
    orientation: "h",
    x: rows.map((g) => g.mean_util),
    // Categories are the group_id, not the name: two same-named rows
    // would otherwise merge into one bar.
    y: rows.map((g) => g.group_id),
    marker: { color: rows.map((g) => colors[g.school_code]) },
    hovertemplate: rows.map((g) =>
      "<b>" + escapeHtml(g.group_name) + "</b><br>mean %{x:.1f}%<br>" +
      escapeHtml(g.users + " users · " + g.jobs + " jobs · " +
        g.gpu_hours + " GPU-h held<extra></extra>")),
  }], {
    height: h,
    margin: { l: 10, r: 20, t: 10, b: 40 },
    paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: th.font,
    // tickmode array forces a label on EVERY bar — Plotly otherwise
    // thins crowded category labels.
    yaxis: {
      tickmode: "array", tickvals: rows.map((g) => g.group_id),
      ticktext: rows.map((g) => g.group_name), automargin: true,
      gridcolor: th.grid, fixedrange: true,
    },
    xaxis: { title: "mean utilization %", range: [0, 105],
             gridcolor: th.grid, fixedrange: true },
    dragmode: false,
  });
  $("groupsBarPlot").style.height = h + "px";
}

// ---- the members drill-down ------------------------------------------
function groupRowClick(e, tr) {
  // the leader's name is a link out to the Users tab, not a drill-down
  const link = e.target.closest("a.userlink");
  if (link) {
    e.stopPropagation();
    if (!isPlainClick(e)) return;
    e.preventDefault();
    openUser(link.dataset.user);
    return;
  }
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
      <td>${m.group || "—"}</td>
      <td class="num">${fmtInt(m.jobs)}</td>
      <td class="num">${fmtInt(m.running_jobs)}</td>
      <td class="num">${raw(pctBar(m.mean_util))}</td>
      <td class="num">${fmt(m.util_gpu_hours, 2)}</td>
      <td class="num">${fmt(m.vram_avg)}</td>
      <td class="small chip-cell">${raw(chipList((m.extra_groups || [])
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
    { key: "user", type: "text" }, { key: "group", type: "text" },
    { key: "jobs", type: "number" }, { key: "running_jobs", type: "number" },
    { key: "mean_util", type: "number" },
    { key: "util_gpu_hours", type: "number" },
    { key: "vram_avg", type: "number" },
    { key: "extra_groups", type: "text" },
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
