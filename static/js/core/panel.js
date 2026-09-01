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
