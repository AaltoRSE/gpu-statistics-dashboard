/* Users tab.
 *
 * The user list is fetched once per window; the text box filters that
 * list locally as you type (no network). A selection is "finalized"
 * only on Enter or a table-row click — and only then is the selected
 * user's job list fetched (server-side user-scoped query).
 *
 * See tabs/jobs.js for why this module and core/router.js import each
 * other. The user's job table (userJobsTable) is wired to the shared
 * table component here for the first time — its headers carry data-k
 * attributes in the HTML, but the original app.js never attached a sort
 * handler to them, so clicking a column header did nothing. Routing it
 * through createTable (T-19) gives it working sort for free, consistent
 * with the other four tables. */
"use strict";

import { $, isPlainClick } from "../core/dom.js";
import {
  fmt, fmtInt, pctBar, escapeList, html, raw, tsToDate, fmtSacctTime,
  jobLink, nodeLinks, partitionLink, stateBadge,
} from "../core/format.js";
import { setResultsLoading, showPanelError, panelOk } from "../core/panel.js";
import { api, status } from "../core/api.js";
import { loaded, setUrl, openJob, openNode, openPartition } from "../core/router.js";
import { createTable } from "../core/table.js";

let userRows = [];
let userSelected = null;   // finalized user name or null
let userJobs = [];
let usersToken = 0;
let userJobsToken = 0;

export async function loadUsers() {
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
      tsToDate(w.start) + " → " + tsToDate(w.end);
    renderUserTable();
    loaded.users = true;
  } catch (e) {
    if (token === usersToken)
      showPanelError("usersResults", e, loadUsers, "the user list");
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

function userRowHtml(u) {
  const user = u.user;
  const selected = u.user === userSelected;
  return html`
    <tr class="row${selected ? " selected-user" : ""}" data-user="${user}">
      <td><b>${user}</b></td>
      <td class="num">${fmtInt(u.jobs)}</td>
      <td class="num">${fmtInt(u.running_jobs)}</td>
      <td class="num">${raw(pctBar(u.mean_util))}</td>
      <td class="num">${fmtInt(u.util_gpu_hours)}</td>
      <td class="num">${fmt(u.vram_avg)}</td>
      <td class="small">${raw(escapeList(u.gpu_types))}</td>
    </tr>`;
}

function userRowClick(e, tr) {
  $("uSearch").value = tr.dataset.user;
  finalizeUser(tr.dataset.user);
}

function userTableEmptyMessage() {
  const searched = $("uSearch").value.trim();
  return {
    text: searched ? "No users match that search." : "No users with GPU activity in this window.",
    resetLabel: searched ? "clear search" : null,
    onReset: () => { $("uSearch").value = ""; renderUserTable(); },
  };
}

const userTable = createTable({
  el: $("userTable"),
  columns: [
    { key: "user", type: "text" }, { key: "jobs", type: "number" },
    { key: "running_jobs", type: "number" }, { key: "mean_util", type: "number" },
    { key: "util_gpu_hours", type: "number" }, { key: "vram_avg", type: "number" },
    { key: "gpu_types", type: "text" },
  ],
  defaultSort: { key: "util_gpu_hours", dir: "desc" },
  renderRow: userRowHtml,
  onRowClick: userRowClick,
  emptyMessage: userTableEmptyMessage,
});

function renderUserTable() {
  const rows = filteredUsers();
  userTable.setRows(rows);
  $("uCount").textContent = rows.length + " shown";
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
export function finalizeUser(name) {
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
    userJobsTable.setRows(userJobs);
  } catch (e) {
    if (token === userJobsToken)
      showPanelError("userJobsResults", e, () => loadUserJobs(user), "the job list");
  } finally {
    if (token === userJobsToken) setResultsLoading("userJobsResults", false);
  }
}

function userJobRowHtml(j) {
  const jobid = j.jobid;
  const rawName = j.name || "";
  const start = fmtSacctTime(j.start);
  const gpus = j.gpus !== undefined ? j.gpus : "—";
  return html`
    <tr class="row" data-job="${jobid}">
      <td>${raw(jobLink(j.jobid))}</td>
      <td class="name-cell" title="${rawName}">${rawName}</td>
      <td>${raw(partitionLink(j.gpu_group || j.partition))}</td>
      <td>${raw(nodeLinks(j.nodes))}</td>
      <td>${raw(stateBadge(j.state))}</td><td>${start}</td>
      <td class="num">${gpus}</td>
      <td class="num">${raw(pctBar(j.mean_util))}</td>
      <td class="num">${fmtInt(j.gpu_hours_eff)}</td>
    </tr>`;
}

function userJobRowClick(e, tr) {
  const link = e.target.closest("a.joblink");
  if (link) {
    e.stopPropagation();
    if (!isPlainClick(e)) return;
    e.preventDefault();
    openJob(link.dataset.job, { kind: "user", user: userSelected });
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
  openJob(tr.dataset.job, { kind: "user", user: userSelected });
}

function userJobsEmptyMessage() {
  return {
    text: $("uRunning").checked
      ? "No running jobs for " + userSelected + " in this window."
      : "No jobs for " + userSelected + " in this window.",
    resetLabel: null,
  };
}

const userJobsTable = createTable({
  el: $("userJobsTable"),
  columns: [
    { key: "jobid", type: "text" }, { key: "name", type: "text" },
    { key: "partition", type: "text" }, { key: "nodes", type: "text" },
    { key: "state", type: "text" }, { key: "start", type: "text" },
    { key: "gpus", type: "number" }, { key: "mean_util", type: "number" },
    { key: "gpu_hours_eff", type: "number" },
  ],
  defaultSort: { key: "mean_util", dir: "desc" },
  renderRow: userJobRowHtml,
  onRowClick: userJobRowClick,
  emptyMessage: userJobsEmptyMessage,
});

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
