/* Small, generic DOM helpers with no data/tab knowledge. markSort and
 * emptyRow are table-rendering helpers that predate the shared table
 * component (see T-19); they live here until that component absorbs them. */
"use strict";

import { escapeHtml } from "./format.js";

export const $ = (id) => document.getElementById(id);

// Trailing-edge debounce for high-frequency input events; the returned
// wrapper exposes .cancel() so a load can be cancelled mid-flight.
export function debounce(fn, wait) {
  let t = null;
  const wrapped = (...args) => {
    clearTimeout(t);
    t = setTimeout(() => { t = null; fn(...args); }, wait);
  };
  wrapped.cancel = () => { clearTimeout(t); t = null; };
  return wrapped;
}

// True for a plain left-click with no modifier held. Cross-tab links carry a
// real href now, so a modified click (cmd/ctrl/shift/alt, or a non-primary
// button) must fall through to the browser's own new-tab/download handling
// instead of being intercepted for the in-page SPA transition.
export function isPlainClick(e) {
  return e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey;
}

export function markSort(tableEl, key, dir) {
  tableEl.querySelectorAll("th").forEach((th) => {
    th.classList.toggle("sorted-asc", th.dataset.k === key && dir === "asc");
    th.classList.toggle("sorted-desc", th.dataset.k === key && dir === "desc");
  });
}

// A filtered table with zero rows says what was filtered out and offers a
// reset, instead of a blank box that reads as "no data".
export function emptyRow(cols, msg, resetLabel) {
  const btn = resetLabel ? ' <button data-empty-reset="1">' + escapeHtml(resetLabel) + '</button>' : '';
  return '<tr class="empty-state-row"><td colspan="' + cols + '">' +
    '<div class="empty-state">' + escapeHtml(msg) + btn + '</div></td></tr>';
}
