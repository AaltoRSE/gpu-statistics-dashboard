/* Tab switching, shareable deep links (/job/<id>, /node/<name>,
 * /partition/<name>, /user/<name>, and the plain /jobs, /partitions,
 * /users, /nodes) and the cross-tab navigation helpers (openJob, openUser,
 * openNode, openPartition) that every tab module calls to jump to another
 * tab. rerenderAllPlots is the theme-toggle re-render sweep across all four
 * tabs.
 *
 * This module imports every tab module, and every tab module imports this
 * one back (for openJob/openUser/openNode/openPartition, setUrl and the
 * shared `loaded` flags) — an intentional cycle. ES modules resolve
 * circular imports via live bindings; every use site here and in the tabs
 * is inside a function body (event handler, async loader), never at
 * top-level, so by the time any of these functions actually runs, both
 * sides of the cycle have finished evaluating. */
"use strict";

import { $ } from "./dom.js";
import { setActiveFreshnessPanel } from "./panel.js";
import * as jobsTab from "../tabs/jobs.js";
import * as usersTab from "../tabs/users.js";
import * as partitionsTab from "../tabs/partitions.js";
import * as nodesTab from "../tabs/nodes.js";

export const loaded = { jobs: false, partitions: false, users: false, nodes: false };

// PLAN-1 3.4: document.title was the same string on every route despite
// real per-tab URLs already existing (/jobs, /partitions, /users, /nodes,
// and the job/node/user/partition deep links, which all resolve to one of
// these four tabs). Every route funnels through showTab, so setting it
// here covers all of them in one place.
const TAB_TITLES = { jobs: "Jobs", partitions: "Partitions", users: "Users", nodes: "Nodes" };

// The header freshness stamp represents the ACTIVE tab's primary
// panel (data is loaded once per window and shared server-side, so a
// page-wide "when was this loaded" belongs in one place). A
// background/detail panel (queue, VRAM, job detail, selected-user
// jobs, node detail) never replaces the header identity.
const MAIN_RESULTS_BY_TAB = {
  jobs: "jobsResults",
  partitions: "partitionsResults",
  users: "usersResults",
  nodes: "nodesResults",
};

export function showTab(name) {
  document.querySelectorAll("nav.tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tabpage").forEach((p) =>
    p.classList.toggle("active", p.id === "tab-" + name));
  document.title = (TAB_TITLES[name] || "Dashboard") + " — Triton GPU Efficiency Dashboard";
  setActiveFreshnessPanel(MAIN_RESULTS_BY_TAB[name] || null);
  let p = Promise.resolve();
  if (name === "jobs" && !loaded.jobs) p = jobsTab.loadJobs();
  if (name === "partitions" && !loaded.partitions) p = partitionsTab.loadPartitions();
  if (name === "users" && !loaded.users) p = usersTab.loadUsers();
  if (name === "nodes" && !loaded.nodes) p = nodesTab.loadNodes();
  window.dispatchEvent(new Event("resize")); // refit hidden plots
  return p;
}

export function setUrl(path) {
  if (location.pathname + location.search !== path) {
    history.pushState({ path }, "", path);
  }
}

export function openJob(jobid, from) {
  showTab("jobs").then(() => jobsTab.loadJobDetail(jobid, from));
}

export function openUser(user) {
  showTab("users").then(() => {
    // finalizeUser is the only path that fetches the user's jobs, and it
    // keeps the URL in sync (/user/<name>, /users on deselect). It
    // accepts raw text, so a user with no window activity still resolves.
    usersTab.finalizeUser(user);
  });
}

export function openNode(node) {
  showTab("nodes").then(() => nodesTab.loadNodeDetail(node));
}

export function openPartition(partition) {
  partitionsTab.setSelectedPartition(partition);
  const wasLoaded = loaded.partitions;
  showTab("partitions").then(() => {
    // applyPartitionSelection re-renders the trend and syncs the URL
    // (/partition/<name>). A fresh loadPartitions already scoped the VRAM
    // fetch on its own, so only a pre-loaded tab needs one here.
    partitionsTab.applyPartitionSelection(partitionsTab.selectedPartition);
    if (wasLoaded) partitionsTab.loadVram();
  });
}

export function clearPartitionSelection() {
  partitionsTab.clearPartitionSelection();
}

export function restoreFromUrl() {
  let m = location.pathname.match(/^\/job\/([^/]+)\/?$/);
  if (m) { showTab("jobs").then(() => jobsTab.loadJobDetail(m[1])); return; }
  m = location.pathname.match(/^\/node\/([^/]+)\/?$/);
  if (m) {
    const node = decodeURIComponent(m[1]);
    showTab("nodes").then(() => nodesTab.loadNodeDetail(node));
    return;
  }
  m = location.pathname.match(/^\/user\/([^/]+)\/?$/);
  if (m) {
    const user = decodeURIComponent(m[1]);
    showTab("users").then(() => { usersTab.finalizeUser(user); });
    return;
  }
  m = location.pathname.match(/^\/partition\/([^/]+)\/?$/);
  if (m) { openPartition(decodeURIComponent(m[1])); return; }
  if (location.pathname === "/jobs") { showTab("jobs"); return; }
  if (location.pathname === "/partitions") {
    clearPartitionSelection();
    showTab("partitions");
    return;
  }
  if (location.pathname === "/users") { showTab("users"); return; }
  if (location.pathname === "/nodes") { showTab("nodes"); return; }
}

// Re-fetch whichever tab is currently visible — the one entry point the
// auto-refresh timer (force=false, cache-friendly) and the header's
// global refresh button (force=true, bypass caches) share. Skips the
// tick entirely while any of that tab's own results panels is mid-load —
// a stricter reading of "never auto-refresh while a detail panel is
// loading" that also avoids stacking a fresh fetch on top of any
// in-flight one, detail or not — rather than queuing it; the next tick
// (or the return-to-visibility catch-up) covers it. Only ever touches
// the active tab's own list, never an open detail panel — the header
// freshness stamp is what tells the operator a job/node detail may be
// stale.
//
// The loading check is scoped to `active` (the tabpage element), not the
// whole document: every tab's results panel starts with a static
// class="results-panel loading" in the HTML, on the assumption that its
// own loader clears it on first load — a tab that has never been visited
// still carries that class, and a document-wide check would see it and
// refuse to auto-refresh any OTHER tab, forever, until every tab had
// been opened at least once.
//
// force=true semantics per tab: Jobs sends refresh=true (backend
// invalidates its shared utilization/VRAM source entries), Users sends
// the same refresh parameter, Nodes keeps its refresh=true snapshot
// semantics, and Partitions refreshes the core charts and VRAM (the
// pending queue keeps its own 300 s accounting cadence — documented in
// the button's title). Returns the loader promise so the header button
// can stay disabled until the reload settles.
export function refreshActiveTab(force = false) {
  const active = document.querySelector(".tabpage.active");
  if (!active) return Promise.resolve();
  if (active.querySelector(".results-panel.loading")) {
    return Promise.resolve();
  }
  const name = active.id.replace(/^tab-/, "");
  if (name === "jobs") return jobsTab.loadJobs(force);
  if (name === "partitions") return partitionsTab.loadPartitions(force);
  if (name === "users") return usersTab.loadUsers(force);
  if (name === "nodes") return nodesTab.loadNodes(force);
  return Promise.resolve();
}

export function rerenderAllPlots() {
  if (loaded.jobs) {
    jobsTab.renderJobEfficiency();
    jobsTab.renderJobsView();
  }
  if (jobsTab.jobDetailData && $("jobDetailResults").style.display !== "none") {
    jobsTab.renderJobDetail(jobsTab.jobDetailData);
  }
  if (loaded.partitions) {
    partitionsTab.renderPartBar();
    partitionsTab.renderPartOccupancy();
    partitionsTab.renderPartTrend(partitionsTab.partTrendData);
    if (partitionsTab.vramJobs.length) partitionsTab.renderVram();
  }
  if (nodesTab.nodeDetailData && $("nodeDetailResults").style.display !== "none") {
    nodesTab.renderNodeDetail(nodesTab.nodeDetailData, nodesTab.nodeDetailName);
  }
}
