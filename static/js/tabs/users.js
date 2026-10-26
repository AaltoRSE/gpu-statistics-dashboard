/* Users tab.
 *
 * The user list is fetched once per window; the text box filters that
 * list locally as you type (no network). A selection is "finalized"
 * only on Enter or a table-row click — and only then are the selected
 * user's priority card (fairshare associations, pending jobs' sprio
 * priority and rank) and job list fetched (server-side user-scoped
 * queries).
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
  fmt, fmtInt, pctBar, chipList, escapeHtml, html, raw, tsToDate, fmtSacctTime,
  jobLink, nodeLinks, partitionLink, stateBadge,
} from "../core/format.js";
import { setResultsLoading, showPanelError, panelOk } from "../core/panel.js";
import { api } from "../core/api.js";
import { loaded, setUrl, openJob, openNode, openPartition } from "../core/router.js";
import { createTable } from "../core/table.js";

let userRows = [];
let userSelected = null;   // finalized user name or null
let userJobs = [];
let usersToken = 0;
let userJobsToken = 0;
let userPriority = null;   // the selected user's /priority body, or null
let userPriorityToken = 0;

export async function loadUsers() {
  const token = ++usersToken;
  setResultsLoading("usersResults", true, "Loading user history…");
  try {
    const data = await api("/api/users?since_hours=" + $("uWindow").value);
    if (token !== usersToken) return;
    panelOk("usersResults");
    userRows = data.users;
    const w = data.window;
    $("uMetaCount").textContent = data.count + " users";
    $("uFairshareUnavailable").hidden = data.fairshare_available !== false;
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
      <td class="num" title="${u.fairshare_account ? "account " + u.fairshare_account : ""}">${fmt(u.fairshare, 3)}</td>
      <td class="small chip-cell">${raw(chipList((u.gpu_types || []).map(escapeHtml)))}</td>
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
    { key: "fairshare", type: "number" }, { key: "gpu_types", type: "text" },
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
    $("userPriorityResults").style.display = "none";
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
  loadUserPriority(finalName);
  loadUserJobs(finalName);
  setUrl("/user/" + encodeURIComponent(finalName));
}

// Fractions of the account's shares / usage as a percentage; a nonzero
// share too small for two decimals must not read as zero.
function sharePct(v) {
  if (v === null || v === undefined) return "—";
  const pct = v * 100;
  return pct > 0 && pct < 0.01 ? "<0.01%" : fmt(pct, 2) + "%";
}

async function loadUserPriority(user) {
  const token = ++userPriorityToken;
  $("userPriorityResults").style.display = "block";
  $("userPriorityTitle").textContent = "Priority · " + user;
  setResultsLoading("userPriorityResults", true, "Loading the user’s priority…");
  try {
    const data = await api("/api/users/" + encodeURIComponent(user) + "/priority");
    if (token !== userPriorityToken) return;
    panelOk("userPriorityResults");
    userPriority = data;
    renderUserPriority();
  } catch (e) {
    if (token === userPriorityToken)
      showPanelError("userPriorityResults", e, () => loadUserPriority(user), "the user’s priority");
  } finally {
    if (token === userPriorityToken) setResultsLoading("userPriorityResults", false);
  }
}

function renderUserPriority() {
  const p = userPriority;
  $("userPriorityTitle").textContent = "Priority · " + p.user +
    (p.default_account ? " · default account " + p.default_account : "");
  const missing = [];
  if (!p.fairshare_available) missing.push("sshare could not be read, so fairshare is unknown");
  if (!p.pending_available) missing.push("sprio could not be read, so pending-job priorities are unknown");
  const note = $("userPriorityNote");
  note.textContent = missing.join(" · ");
  note.hidden = !missing.length;
  userAssocTable.setRows(p.associations || []);
  const pending = p.pending || [];
  const jobs = new Set(pending.map((r) => r.jobid)).size;
  $("userPendingCount").textContent = p.pending
    ? jobs + (jobs === 1 ? " job" : " jobs") : "";
  userPendingTable.setRows(pending);
}

function userAssocRowHtml(a) {
  return html`
    <tr>
      <td>${a.account}</td>
      <td class="num" title="${a.raw_shares === null ? "" : a.raw_shares + " raw shares"}">${sharePct(a.norm_shares)}</td>
      <td class="num">${sharePct(a.effective_usage)}</td>
      <td class="num">${fmt(a.fairshare, 3)}</td>
      <td>${a.primary ? "yes" : ""}</td>
    </tr>`;
}

function userPendingRowHtml(r) {
  return html`
    <tr>
      <td>${r.jobid}</td><td>${r.partition}</td><td>${r.qos}</td>
      <td class="num">${fmtInt(r.priority)}</td>
      <td class="num">${fmtInt(r.rank)} / ${fmtInt(r.queue_length)}</td>
      <td class="num">${fmtInt(r.fairshare)}</td>
      <td class="num">${fmtInt(r.age)}</td>
      <td class="num">${fmtInt(r.partition_factor)}</td>
      <td class="num">${fmtInt(r.qos_factor)}</td>
      <td class="num">${fmtInt(r.jobsize)}</td>
    </tr>`;
}

const userAssocTable = createTable({
  el: $("userAssocTable"),
  columns: [
    { key: "account", type: "text" }, { key: "norm_shares", type: "number" },
    { key: "effective_usage", type: "number" }, { key: "fairshare", type: "number" },
  ],
  defaultSort: { key: "effective_usage", dir: "desc" },
  renderRow: userAssocRowHtml,
  onRowClick: () => {},
  emptyMessage: () => ({
    text: userPriority && !userPriority.fairshare_available
      ? "Fairshare unavailable." : "Slurm lists no account for " + userSelected + ".",
    resetLabel: null,
  }),
});

const userPendingTable = createTable({
  el: $("userPendingTable"),
  columns: [
    { key: "jobid", type: "text" }, { key: "partition", type: "text" },
    { key: "qos", type: "text" }, { key: "priority", type: "number" },
    { key: "rank", type: "number" }, { key: "fairshare", type: "number" },
    { key: "age", type: "number" }, { key: "partition_factor", type: "number" },
    { key: "qos_factor", type: "number" }, { key: "jobsize", type: "number" },
  ],
  defaultSort: { key: "priority", dir: "desc" },
  renderRow: userPendingRowHtml,
  onRowClick: () => {},
  emptyMessage: () => ({
    text: userPriority && !userPriority.pending_available
      ? "Pending-job priorities unavailable." : "No pending jobs for " + userSelected + ".",
    resetLabel: null,
  }),
});

async function loadUserJobs(user) {
  const token = ++userJobsToken;
  $("userJobsResults").style.display = "block";
  $("userJobsTitle").textContent =
    "Jobs · " + user + " · last " + $("uWindow").value / 24 + " d";
  setResultsLoading("userJobsResults", true, "Loading selected-user job history…");
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
    { key: "gpu_group", type: "text" }, { key: "nodes", type: "text" },
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
  $("userPriorityResults").style.display = "none";
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
