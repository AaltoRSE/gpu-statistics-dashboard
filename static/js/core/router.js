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
