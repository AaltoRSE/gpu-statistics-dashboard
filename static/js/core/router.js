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
import * as jobsTab from "../tabs/jobs.js";
import * as usersTab from "../tabs/users.js";
import * as partitionsTab from "../tabs/partitions.js";
import * as nodesTab from "../tabs/nodes.js";

export const loaded = { jobs: false, partitions: false, users: false, nodes: false };

export function showTab(name) {
  document.querySelectorAll("nav.tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tabpage").forEach((p) =>
    p.classList.toggle("active", p.id === "tab-" + name));
  updateStickyOffsets();
  let p = Promise.resolve();
  if (name === "jobs" && !loaded.jobs) p = jobsTab.loadJobs();
  if (name === "partitions" && !loaded.partitions) p = partitionsTab.loadPartitions();
  if (name === "users" && !loaded.users) p = usersTab.loadUsers();
  if (name === "nodes" && !loaded.nodes) p = nodesTab.loadNodes();
  window.dispatchEvent(new Event("resize")); // refit hidden plots
  return p;
}

// Sticky stack (T-1.1/1.2): --nav-h and --controls-h back the top: offsets
// css/layout.css's nav.tabs and css/components.css's .sticky-filters /
// .table-scroll thead th use.
export function updateStickyOffsets() {
  const nav = document.querySelector("nav.tabs");
  const filters = document.querySelector(".tabpage.active .sticky-filters");
  const root = document.documentElement.style;
  if (nav) root.setProperty("--nav-h", nav.getBoundingClientRect().height + "px");
  if (filters) root.setProperty("--controls-h", filters.getBoundingClientRect().height + "px");
}

// A one-shot measurement at load isn't enough: a filter row's own height
// changes once its meta-count/freshness text populates after the first
// fetch (flex-wrap can break onto a different number of lines once that
// text is no longer empty), independent of any window resize. A
// ResizeObserver on every .sticky-filters element (only the active one
// affects layout; the rest are display:none) plus nav.tabs itself catches
// every actual cause of a size change — data populating, wrapping at a
// breakpoint, a real window resize — in one place, rather than hunting
// down each call site that might change these elements' height.
export function initStickyOffsets() {
  updateStickyOffsets();
  if (typeof ResizeObserver === "undefined") return; // not a load-bearing feature; degrade quietly
  const observer = new ResizeObserver(() => updateStickyOffsets());
  document.querySelectorAll("nav.tabs, .sticky-filters").forEach((el) => observer.observe(el));
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

// Auto-refresh (T-22, main.js's onTick): re-fetch whichever tab is
// currently visible. Skips the tick entirely while any of that tab's own
// results panels is mid-load — a stricter reading of "never auto-refresh
// while a detail panel is loading" that also avoids stacking a fresh fetch
// on top of any in-flight one, detail or not — rather than queuing it; the
// next tick (or the return-to-visibility catch-up) covers it. Only ever
// touches the active tab's own list, never an open detail panel — the
// freshness clock next to its own panel is what tells the operator a job/
// node detail may be stale.
//
// The loading check is scoped to `active` (the tabpage element), not the
// whole document: every tab's results panel starts with a static
// class="results-panel loading" in the HTML, on the assumption that its
// own loader clears it on first load — a tab that has never been visited
// still carries that class, and a document-wide check would see it and
// refuse to auto-refresh any OTHER tab, forever, until every tab had been
// opened at least once.
export function refreshActiveTab() {
  const active = document.querySelector(".tabpage.active");
  if (!active) return;
  if (active.querySelector(".results-panel.loading")) return;
  const name = active.id.replace(/^tab-/, "");
  if (name === "jobs") jobsTab.loadJobs();
  else if (name === "partitions") partitionsTab.loadPartitions();
  else if (name === "users") usersTab.loadUsers();
  else if (name === "nodes") nodesTab.loadNodes();
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
