/* Users tab.
 *
 * The user list is fetched once per window; the text box filters that
 * list locally as you type (no network). A selection is "finalized"
 * only on Enter or a table-row click — and only then is the selected
 * user's job list fetched (server-side user-scoped query).
 *
 * Contact history (Garage Diary) is fetched alongside the list on every
 * load/refresh and joined per user case-insensitively; it never blocks
 * the job metrics. Finalizing a user also fetches that user's per-GPU
 * utilization history (one request serves both the overall and per-job
 * chart views) and renders the all-time contact table with contact-date
 * markers overlaid on the chart.
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
import { renderPlot, plotTheme } from "../core/plot.js";

let userRows = [];
export let userSelected = null; // finalized user name or null
let userJobs = [];
let usersToken = 0;
let userJobsToken = 0;
let userActivityToken = 0;
let contactRows = [];        // all contacts from /api/users/contacts
let contactsAvailable = false; // false until a usable contacts payload lands
let contactWarn = null;      // fixed unavailable-source warning, if any
let contactSkipped = 0;      // malformed/unjoinable source rows
export let userActivityData = null; // last activity response for the selection

export function contactCountFor(name) {
  const key = String(name || "").toLowerCase();
  if (!contactsAvailable) return null;
  return contactRows.filter((c) => c.username === key).length;
}

export function lastContactDateFor(name) {
  const key = String(name || "").toLowerCase();
  const hits = contactRows.filter((c) => c.username === key);
  return hits.length ? hits[0].date : null; // contacts arrive date-desc
}

export async function loadUsers() {
  const token = ++usersToken;
  setResultsLoading("usersResults", true);
  // The list is authoritative: a failed/absent contacts payload only
  // downgrades the Contacts column to "Unavailable", never the list.
  const listP = api("/api/users?since_hours=" + $("uWindow").value);
  const contactsP = api("/api/users/contacts").then(
    (d) => d,
    () => ({ available: false, warning: null, skipped_rows: 0,
             contacts: [] }),
  );
  let data;
  try {
    [data] = await Promise.all([
      listP.then((d) => {
        if (token !== usersToken) return null;
        return d;
      }),
      contactsP.then((d) => {
        if (token !== usersToken) return;
        contactRows = d.contacts || [];
        contactsAvailable = !!d.available;
        contactWarn = d.available ? null : (d.warning || null);
        contactSkipped = d.skipped_rows || 0;
      }),
    ]);
    if (token !== usersToken || !data) return;
    panelOk("usersResults");
    userRows = data.users;
    const w = data.window;
    $("uMetaCount").textContent = data.count + " users";
    $("uMeta").textContent =
      tsToDate(w.start) + " → " + tsToDate(w.end);
    renderUserTable();
    loaded.users = true;
    // An already-finalized selection refreshes its contact panel data
    // too — and, if its chart is open, rebuilds the markers from the
    // fresh history (the activity request may have completed first and
    // rendered markers from the previous contacts without a refetch).
    if (userSelected) {
      renderUserContacts(userSelected);
      if (userActivityData && userActivityData.user === userSelected &&
          $("userActivityResults").style.display !== "none") {
        renderUserActivity();
      }
    }
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
      <td class="small chip-cell">${raw(chipList((u.gpu_types || []).map(escapeHtml)))}</td>
      <td class="num">${raw(userContactsCell(u))}</td>
    </tr>`;
}

function userContactsCell(u) {
  // createTable sorts nulls last regardless of direction, so an
  // unavailable history sinks below every count while staying readable.
  if (u.contact_count === null || u.contact_count === undefined) {
    return escapeHtml("Unavailable");
  }
  if (u.contact_count === 0) return escapeHtml("Never");
  return escapeHtml(u.contact_count + " · last " + u.last_contact_date);
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
    { key: "contact_count", type: "number" },
  ],
  defaultSort: { key: "util_gpu_hours", dir: "desc" },
  renderRow: userRowHtml,
  onRowClick: userRowClick,
  emptyMessage: userTableEmptyMessage,
});

function renderUserTable() {
  // Join contact history per user (case-insensitive). The count is null
  // when the source is unavailable, 0 when available but never contacted.
  const rows = filteredUsers().map((u) => {
    const count = contactCountFor(u.user);
    return Object.assign({}, u, {
      contact_count: count,
      last_contact_date: count ? lastContactDateFor(u.user) : null,
    });
  });
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
    userActivityToken++; // invalidate any in-flight activity response
    userJobsToken++;     // invalidate any in-flight jobs response too
    userActivityData = null;
    $("userJobsResults").style.display = "none";
    $("userActivityResults").style.display = "none";
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
  renderUserContacts(finalName);
  loadUserActivity(finalName);
  loadUserJobs(finalName);
  setUrl("/user/" + encodeURIComponent(finalName));
}

function renderUserContacts(user) {
  // All-time history, newest first (the API pre-sorts date-descending).
  const key = user.toLowerCase();
  const mine = contactRows.filter((c) => c.username === key);
  const status = $("userContactsStatus");
  const bits = [];
  if (!contactsAvailable) {
    bits.push("Contact history unavailable" +
      (contactWarn ? ": " + contactWarn : "."));
  } else {
    if (contactSkipped) {
      bits.push("Skipped " + contactSkipped +
        " malformed or unjoinable contact rows.");
    }
  }
  status.textContent = bits.join(" ");
  contactTable.setRows(mine);
}

const contactTable = createTable({
  el: $("userContactsTable"),
  columns: [
    { key: "date", type: "text" }, { key: "message", type: "text" },
  ],
  defaultSort: { key: "date", dir: "desc" },
  renderRow: contactRowHtml,
  emptyMessage: () => ({
    text: contactsAvailable
      ? "No recorded contacts for " + userSelected + "."
      : "Contact history unavailable.",
    resetLabel: null,
  }),
});

function contactRowHtml(c) {
  return html`
    <tr class="row">
      <td>${c.date}</td>
      <td class="msg-cell">${c.message}</td>
    </tr>`;
}

export async function loadUserJobs(user) {
  const token = ++userJobsToken;
  $("userJobsResults").style.display = "block";
  $("userJobsTitle").textContent =
    "Jobs · " + user + " · last " + $("uWindow").value / 24 + " d";
  setResultsLoading("userJobsResults", true);
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
  // The finalized selection survives a window change: list, activity
  // graph (with its markers), and jobs table all reload for the new
  // window; separate tokens discard any stale in-flight response.
  loadUsers();
  if (userSelected) {
    renderUserContacts(userSelected);
    loadUserActivity(userSelected);
    loadUserJobs(userSelected);
  }
});
$("uRunning").addEventListener("change", () => {
  renderUserTable();
  if (userSelected) loadUserJobs(userSelected);
});
$("uRefresh").addEventListener("click", () => {
  loadUsers();
  if (userSelected) {
    loadUserActivity(userSelected);
    loadUserJobs(userSelected);
  }
});
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

// ---- selected-user utilization history + contact markers ------------

/* One activity fetch per selection/window; both chart views derive from
 * the same response, so switching #uUtilView never refetches. Contact
 * markers come from the already-fetched contact history: only dates
 * inside the response window are charted, same-day contacts collapse
 * into one marker listing every message; the table stays uncollapsed. */
export async function loadUserActivity(user) {
  const token = ++userActivityToken;
  // A different user than the one whose chart is on screen must never
  // show the previous user's utilization or markers while loading: drop
  // the old payload and reset the chart area. A same-user refresh keeps
  // its (stale) chart visible until the new response lands.
  if (userActivityData && userActivityData.user !== user) {
    userActivityData = null;
    $("userUtilPlot").style.display = "none";
    $("userUtilEmpty").style.display = "none";
  }
  $("userActivityResults").style.display = "block";
  $("userActivityTitle").textContent =
    "Utilization history · " + user + " · last " +
    $("uWindow").value / 24 + " d";
  setResultsLoading("userActivityResults", true);
  try {
    const data = await api(
      "/api/users/" + encodeURIComponent(user) + "/activity?since_hours=" +
      $("uWindow").value);
    if (token !== userActivityToken) return;
    panelOk("userActivityResults");
    userActivityData = data;
    renderUserActivity();
  } catch (e) {
    if (token === userActivityToken) {
      showPanelError("userActivityResults", e,
        () => loadUserActivity(user), "the utilization history");
    }
  } finally {
    if (token === userActivityToken) {
      setResultsLoading("userActivityResults", false);
    }
  }
}

function contactMarkers(user, winStart, winEnd) {
  // A contact's ISO date intersects the response window when its
  // Europe/Helsinki calendar day overlaps [winStart, winEnd] — the same
  // zone every other timestamp renders in. Comparing noon-UTC epochs
  // would drop a contact on a partial first/last window day even though
  // its date intersects the window, so derive the window's inclusive
  // Helsinki date range instead.
  const fmt = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Europe/Helsinki", year: "numeric", month: "2-digit",
    day: "2-digit",
  });
  // Field order via formatToParts is locale-dependent; pick each part
  // by type (the same approach tsToDate uses) to build the ISO string.
  const iso = (ts) => {
    const parts = fmt.formatToParts(new Date(ts * 1000));
    const get = (type) => parts.find((p) => p.type === type).value;
    return get("year") + "-" + get("month") + "-" + get("day");
  };
  const firstDate = iso(winStart);
  const lastDate = iso(winEnd);
  const key = user.toLowerCase();
  const byDate = new Map();
  for (const c of contactRows) {
    if (c.username !== key) continue;
    if (c.date < firstDate || c.date > lastDate) {
      continue; // outside window: table only
    }
    if (!byDate.has(c.date)) byDate.set(c.date, []);
    byDate.get(c.date).push(c.message);
  }
  // The plan forbids inferring a contact time: pass the ISO calendar
  // date string itself as the marker/shape x value; Plotly places it on
  // the date axis without inventing an hour of day.
  return [...byDate.entries()]
    .sort((a, b) => (a[0] < b[0] ? -1 : 1))
    .map(([date, messages]) => ({
      date,
      text: date + " — " + messages.map((m) => m || "?").join(" | "),
    }));
}

export function renderUserActivity() {
  const data = userActivityData;
  const panel = $("userActivityResults");
  if (!data || panel.style.display === "none") return;
  const th = plotTheme();
  const view = $("uUtilView").value;
  const traces = [];
  if (view === "aggregate") {
    traces.push({
      type: "scatter", mode: "lines", name: "overall utilization",
      x: data.aggregate.map((v) => v[0] * 1000),
      y: data.aggregate.map((v) => v[1]),
      line: { width: 2, color: th.acc },
    });
  } else {
    data.jobs.forEach((j, i) => {
      traces.push({
        type: "scatter", mode: "lines", name: "job " + j.jobid,
        x: j.values.map((v) => v[0] * 1000),
        y: j.values.map((v) => v[1]),
        line: { width: 2, color: th.colors[i % th.colors.length] },
      });
    });
  }
  const empty = data.aggregate.length === 0;
  $("userUtilEmpty").style.display = empty ? "" : "none";
  $("userUtilPlot").style.display = empty ? "none" : "";
  if (!empty) {
    const markers = contactMarkers(userSelected, data.window.start,
      data.window.end);
    // Dotted vertical shapes at each contact date…
    const shapes = markers.map((m) => ({
      type: "line", x0: m.date, x1: m.date, y0: 0, y1: 105,
      line: { width: 1.5, dash: "dot", color: th.warn },
    }));
    // …plus one distinct marker trace at 100% (same-day contacts
    // collapsed; hover lists every message of the day).
    if (markers.length) {
      traces.push({
        type: "scatter", mode: "markers", name: "contact",
        x: markers.map((m) => m.date),
        y: markers.map(() => 100),
        text: markers.map((m) => escapeHtml(m.text)),
        hoverinfo: "text",
        marker: { symbol: "diamond", size: 9, color: th.warn },
      });
    }
    renderPlot("userUtilPlot", traces, {
      margin: { l: 46, r: 20, t: 10, b: 34 },
      paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
      font: th.font,
      showlegend: view === "jobs" || markers.length > 0,
      legend: { orientation: "h", y: -0.15 },
      hovermode: "x unified",
      yaxis: { title: "%", range: [0, 105], gridcolor: th.grid },
      xaxis: { type: "date", gridcolor: th.grid },
      shapes,
    });
  }
}

$("uUtilView").addEventListener("change", () => {
  // Both views derive from the same cached response: re-render only.
  renderUserActivity();
});
