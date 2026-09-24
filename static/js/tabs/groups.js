/* Groups tab: GPU efficiency per research group / department / school.
 *
 * One fetch per window+level+running change; the school filter and the
 * search box filter that response locally (no fetch) and keep the URL in
 * sync (/groups?school=&level=&running=). The Unaffiliated and Unresolved
 * rows always render — an empty row means nobody is in it, never that
 * nobody exists.
 *
 * See tabs/users.js for why this module and core/router.js import each
 * other. */
"use strict";

import { $ } from "../core/dom.js";
import {
  fmt, fmtInt, pctBar, escapeHtml, html, raw, tsToDate,
} from "../core/format.js";
import { setResultsLoading, showPanelError, panelOk } from "../core/panel.js";
import { api } from "../core/api.js";
import { loaded, setUrl } from "../core/router.js";
import { createTable } from "../core/table.js";

let groupRows = [];        // the /api/groups rows for the current fetch
let schools = [];          // [{code, short, full}] from the response
let pendingSchool = null;  // deep-linked school before options exist
let groupsToken = 0;

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
  onRowClick: () => {},   // the members drill-down lands with the chart
  emptyMessage: groupTableEmptyMessage,
});

function renderGroupTable() {
  const rows = filteredGroups();
  groupTable.setRows(rows);
  $("gCount").textContent = rows.length + " shown";
}

$("gWindow").addEventListener("change", loadGroups);
$("gLevel").addEventListener("change", loadGroups);
$("gRunning").addEventListener("change", loadGroups);
$("gSchool").addEventListener("change", () => {
  renderGroupTable();
  syncUrl();
});
$("gRefresh").addEventListener("click", loadGroups);
$("gSearch").addEventListener("input", renderGroupTable);
