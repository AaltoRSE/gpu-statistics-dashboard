/* Panel-local loading/error/stale state, plus the page-level error banner.
 * A failed refresh keeps the last data visible (flagged stale) without
 * depending on the global banner. The <div class="panel-error"> is appended
 * to the panel's results-content so it sits with the data it belongs to. */
"use strict";

import { $ } from "./dom.js";
import { escapeHtml } from "./format.js";

export function errBox(show, msg) {
  const box = $("errBox");
  box.style.display = show ? "block" : "none";
  if (show) box.textContent = msg;
}

export function setResultsLoading(resultsId, loading) {
  const el = $(resultsId);
  el.classList.toggle("loading", loading);
  el.setAttribute("aria-busy", loading ? "true" : "false");
}

// resultsId -> ms timestamp of last successful load.
export const panelLoadedAt = {};

export function fmtClock(ts) {
  return new Date(ts).toLocaleTimeString("en-GB", { hour12: false });
}

export function showPanelError(resultsId, err, reload, source) {
  const panel = $(resultsId);
  if (!panel) return;
  clearPanelError(resultsId);
  const box = document.createElement("div");
  box.className = "panel-error";
  box.innerHTML = "&#9888; " + escapeHtml(
    "Could not load " + (source || "data") + ": " +
    (err && err.message ? err.message : err));
  if (typeof reload === "function") {
    const btn = document.createElement("button");
    btn.textContent = "retry";
    btn.addEventListener("click", () => { clearPanelError(resultsId); reload(); });
    box.appendChild(btn);
  }
  const content = panel.querySelector(".results-content") || panel;
  content.appendChild(box);
  markStale(resultsId, true);
}

export function clearPanelError(resultsId) {
  const panel = $(resultsId);
  if (!panel) return;
  panel.querySelectorAll(".panel-error").forEach((el) => el.remove());
  markStale(resultsId, false);
}

// A successful load: remember when it happened (for the stale timestamp) and
// drop any error/stale state. Call at each loader's success point.
export function panelOk(resultsId) {
  panelLoadedAt[resultsId] = Date.now();
  clearPanelError(resultsId);
  tickFreshness();
}

// ---- freshness clock -----------------------------------------------
// "updated N ago" next to a panel's count, from the same panelLoadedAt
// timestamp the stale-note already used. Any element with
// data-fresh-for="<resultsId>" is kept in sync — on every successful load
// (via panelOk above) and once a minute so the text keeps advancing on an
// otherwise-idle tab.

function freshnessText(ts) {
  if (!ts) return "";
  const mins = Math.floor((Date.now() - ts) / 60000);
  if (mins < 1) return "updated just now";
  if (mins === 1) return "updated 1 min ago";
  if (mins < 60) return "updated " + mins + " min ago";
  const hrs = Math.floor(mins / 60);
  return "updated " + hrs + (hrs === 1 ? " hour ago" : " hours ago");
}

export function tickFreshness() {
  document.querySelectorAll("[data-fresh-for]").forEach((el) => {
    el.textContent = freshnessText(panelLoadedAt[el.dataset.freshFor]);
  });
}

setInterval(tickFreshness, 60000);

// ---- opt-in auto-refresh --------------------------------------------
// Off by default (see the ticket's own watch-out: a wall-display tab left
// open would otherwise poll sacct/Prometheus forever with no operator in
// the loop to notice). Persisted in localStorage beside the theme
// preference. Paused while the tab is hidden — a background tab gains
// nothing from polling — and, since a paused interval can leave the data
// stale by however long the tab was hidden, does one refresh immediately
// on return to visibility rather than waiting out the rest of that interval.
const AUTO_REFRESH_KEY = "gpu-dash-auto-refresh";
let autoRefreshTimer = null;
let autoRefreshPending = false; // tab was hidden through at least one tick

function autoRefreshSeconds() {
  return Number(localStorage.getItem(AUTO_REFRESH_KEY) || "0") || 0;
}

// onTick is called on every interval tick and once on return to visibility
// after a paused period; it decides what "refresh" means (which tab, and
// whether anything is mid-load right now) and is the only place that
// touches tab-specific state, so this module stays tab-agnostic.
export function initAutoRefresh(onTick) {
  const sel = $("autoRefresh");
  sel.value = String(autoRefreshSeconds());
  sel.addEventListener("change", () => {
    localStorage.setItem(AUTO_REFRESH_KEY, sel.value);
    restartAutoRefreshTimer(onTick);
  });
  restartAutoRefreshTimer(onTick);

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopAutoRefreshTimer();
      autoRefreshPending = autoRefreshSeconds() > 0;
      return;
    }
    if (autoRefreshPending) {
      autoRefreshPending = false;
      onTick();
    }
    restartAutoRefreshTimer(onTick);
  });
}

function stopAutoRefreshTimer() {
  if (autoRefreshTimer) { clearInterval(autoRefreshTimer); autoRefreshTimer = null; }
}

function restartAutoRefreshTimer(onTick) {
  stopAutoRefreshTimer();
  const seconds = autoRefreshSeconds();
  if (!seconds || document.hidden) return;
  autoRefreshTimer = setInterval(onTick, seconds * 1000);
}

// Flag (or clear) the panel's data as stale. Only meaningful once a load has
// actually succeeded; a first-load failure shows just the error box (there is
// no prior data to be "stale"). The note carries the last-load timestamp so
// the operator knows how old the visible numbers are.
export function markStale(resultsId, stale) {
  const panel = $(resultsId);
  if (!panel) return;
  panel.classList.toggle("stale", !!stale);
  const at = panelLoadedAt[resultsId];
  if (!stale || !at) return;
  let note = panel.querySelector(".stale-note");
  if (!note) {
    note = document.createElement("div");
    note.className = "stale-note";
    (panel.querySelector(".results-content") || panel).appendChild(note);
  }
  note.textContent = "Out of date — last loaded " + fmtClock(at) +
    ". The refresh failed, so this may not reflect the current state.";
}
