/* Metric glossary (T-23): a single <dialog> (see static/index.html) that
 * every "?" trigger opens and scrolls to its own term. One glossary,
 * multiple entry points — not a separate popover per column, which would
 * mean five copies of the same definitions to keep in sync. */
"use strict";

import { $ } from "./dom.js";

export function initGlossary() {
  const dialog = $("glossaryDialog");
  document.querySelectorAll("[data-glossary]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      // Every glossary trigger this ships with today sits inside a
      // sortable <th> (core/table.js's own header button covers the rest
      // of the cell); stopping propagation keeps opening the glossary from
      // also firing a sort.
      e.stopPropagation();
      e.preventDefault();
      dialog.showModal();
      const term = document.getElementById("glossary-" + btn.dataset.glossary);
      if (term) term.scrollIntoView({ block: "nearest" });
    });
  });
  $("glossaryClose").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (e) => {
    if (e.target === dialog) dialog.close(); // click on the ::backdrop
  });
}
