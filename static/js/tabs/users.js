/* Users tab.
 *
 * The user list is fetched once per window; the text box filters that
 * list locally as you type (no network). A selection is "finalized"
 * only on Enter or a table-row click — and only then is the selected
 * user's job list fetched (server-side user-scoped query).
 *
 * See tabs/jobs.js for why this module and core/router.js import each
 * other. */
"use strict";

import { $, isPlainClick, markSort, emptyRow } from "../core/dom.js";
import {
  fmt, fmtInt, pctBar, escapeHtml, escapeList, compareStrings, tsToDate,
  jobLink, nodeLinks, partitionLink, stateBadge,
} from "../core/format.js";
import { setResultsLoading, showPanelError, panelOk } from "../core/panel.js";
import { api, status } from "../core/api.js";
import { loaded, setUrl, openJob, openNode, openPartition } from "../core/router.js";

let userRows = [];
let userSelected = null;   // finalized user name or null
let userJobs = [];
let userSort = { key: "util_gpu_hours", dir: "desc" };
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
    tb.innerHTML = emptyRow(9,
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
      <td class="num">${escapeHtml(fmtInt(j.gpu_hours_eff))}</td>
    </tr>`;
  }).join("");
  tb.querySelectorAll("tr.row").forEach((tr) =>
    tr.addEventListener("click", (e) => {
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
