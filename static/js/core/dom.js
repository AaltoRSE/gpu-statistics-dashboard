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

// Wires format.js's chipList "+N more" toggle once, by delegation on
// document: any table that renders a chipList cell gets the click handler
// for free, including a table whose tbody is fully replaced on every sort/
// filter (a per-cell listener would be lost on that next render; a single
// document-level one survives it). One-way expand — a chip list is a
// dead-end detail view, not something worth re-collapsing.
export function initChipToggle() {
  // Capture phase, not bubble: a table row's own click-to-navigate
  // handler lives on the <tr> itself, deeper in the tree than this
  // document-level listener. In the bubble phase that handler would
  // already have run (and navigated away) by the time a bubble-phase
  // listener up here got a chance to stopPropagation(); capture fires
  // top-down, before that, so it can actually intercept the click.
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".chip-more");
    if (!btn) return;
    e.stopPropagation();
    const overflow = btn.nextElementSibling;
    if (overflow && overflow.classList.contains("chip-overflow")) {
      overflow.hidden = false;
    }
    btn.remove();
  }, true);
}
