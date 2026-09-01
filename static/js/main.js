/* Entry point: theme + health-check bootstrap, tab-nav wiring, and the
 * initial view (deep link or the Jobs tab). Importing the tab modules for
 * their side effects (each wires its own DOM listeners at import time) is
 * what actually builds the page; this file just sequences startup. */
"use strict";

import { $ } from "./core/dom.js";
import { escapeHtml } from "./core/format.js";
import { api } from "./core/api.js";
import { errBox, initAutoRefresh } from "./core/panel.js";
import { initTheme } from "./core/theme.js";
import { initGlossary } from "./core/glossary.js";
import {
  showTab, setUrl, restoreFromUrl, rerenderAllPlots, clearPartitionSelection,
  refreshActiveTab, initStickyOffsets,
} from "./core/router.js";

import { loadJobs } from "./tabs/jobs.js";
import "./tabs/users.js";
import "./tabs/partitions.js";
import "./tabs/nodes.js";

async function checkHealth() {
  try {
    const h = await api("/api/health");
    errBox(false);
    $("health").innerHTML = '<b>&#9679;</b> prometheus: ' +
      escapeHtml(h.prometheus.replace(/^https?:\/\//, ""));
  } catch (_) {
    errBox(true, "Backend unreachable — the health check failed");
    $("health").innerHTML = '<span style="color:var(--bad)">&#9679; backend down</span>';
  }
}

initTheme(rerenderAllPlots);
initAutoRefresh(refreshActiveTab);
initGlossary();

initStickyOffsets();

window.addEventListener("popstate", restoreFromUrl);
document.querySelectorAll("nav.tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    // Plain /partitions clears any partition selection, matching the
    // deep-link restore; clear before showTab so a fresh loadPartitions
    // fetches unscoped.
    if (b.dataset.tab === "partitions") clearPartitionSelection();
    showTab(b.dataset.tab);
    setUrl("/" + b.dataset.tab);
  }));

checkHealth();
if (location.pathname === "/" || location.pathname === "") {
  loadJobs();
} else {
  restoreFromUrl();
}
