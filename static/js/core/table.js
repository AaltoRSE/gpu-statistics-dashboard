/* One sortable-table driver for all five tables (Jobs, Users, a user's
 * Jobs, Partitions, Nodes). Owns everything about *how a column sorts and
 * how the header reflects it* — comparator, default direction by column
 * type, null handling, sort-a-copy, colspan, aria-sort and turning each
 * sortable header into a real <button> — so that logic exists exactly
 * once. Row markup and click semantics stay with the caller: which links
 * take priority over the row's own click, and what a click ultimately
 * does, are irreducibly different per table.
 *
 * renderRow must build its markup with core/format.js's html`` tagged
 * template (escapes every interpolation by default; wrap an
 * already-trusted value — the output of jobLink, pctBar, stateBadge, ... —
 * in raw() to opt out). This table never runs a caller's markup through
 * its own escaping; renderRow's output is trusted as-is and assigned
 * straight to tbody.innerHTML, so html``/raw() is the only thing standing
 * between a Slurm-supplied job name and script injection. See T-20.
 *
 * Column sort behavior, decided once here instead of per table (see the
 * T-19 commit message for the previous, inconsistent per-table behavior
 * this replaces):
 *   - Every column click sorts a COPY of the caller's row array. The
 *     caller's array (and therefore the server's original order) is never
 *     mutated.
 *   - A column's default direction is by type: text starts ascending,
 *     number starts descending. This applies uniformly, both for the
 *     table's initial sort and the first click on any given column.
 *   - null/undefined values sort last, regardless of direction.
 *   - An array value (e.g. a job's node list) sorts by its
 *     comma-joined string, matching how it is displayed.
 */
"use strict";

import { compareStrings } from "./format.js";
import { emptyRow } from "./dom.js";

export function createTable({ el, columns, defaultSort, renderRow, onRowClick, emptyMessage }) {
  const tbody = el.querySelector("tbody");
  // Every header cell counts toward colspan, not just the sortable ones
  // named in `columns` — e.g. the Nodes table's trailing "Active jobs"
  // column has no data-k. This is what retires the hardcoded emptyRow(N,
  // …) literals (and their one pre-existing off-by-one) from every tab.
  const colCount = el.querySelectorAll("thead th").length;
  let rows = [];
  let sort = { ...defaultSort };

  function columnType(key) {
    const col = columns.find((c) => c.key === key);
    return col && col.type === "number" ? "number" : "text";
  }

  function sortValue(row, key) {
    const v = row[key];
    return Array.isArray(v) ? v.join(",") : v;
  }

  function compareRows(a, b) {
    const s = sort.dir === "asc" ? 1 : -1;
    const va = sortValue(a, sort.key), vb = sortValue(b, sort.key);
    const an = va === null || va === undefined;
    const bn = vb === null || vb === undefined;
    if (an && bn) return 0;
    if (an) return 1;  // nulls sort last regardless of direction
    if (bn) return -1;
    if (typeof va === "number" && typeof vb === "number") return (va - vb) * s;
    return compareStrings(va, vb) * s;
  }

  function markHeader() {
    el.querySelectorAll("th[data-k]").forEach((th) => {
      const active = th.dataset.k === sort.key;
      th.classList.toggle("sorted-asc", active && sort.dir === "asc");
      th.classList.toggle("sorted-desc", active && sort.dir === "desc");
      th.setAttribute("aria-sort", active
        ? (sort.dir === "asc" ? "ascending" : "descending") : "none");
    });
  }

  function render() {
    const sorted = rows.slice().sort(compareRows);
    if (!sorted.length) {
      const msg = emptyMessage();
      tbody.innerHTML = emptyRow(colCount, msg.text, msg.resetLabel);
      const reset = tbody.querySelector("button[data-empty-reset]");
      if (reset && msg.onReset) reset.addEventListener("click", msg.onReset);
      markHeader();
      return;
    }
    tbody.innerHTML = sorted.map(renderRow).join("");
    tbody.querySelectorAll("tr.row").forEach((tr) =>
      tr.addEventListener("click", (e) => onRowClick(e, tr)));
    markHeader();
  }

  // Sortable headers become real <button>s (keyboard- and
  // screen-reader-reachable), wired once; every later render only
  // rewrites the tbody.
  el.querySelectorAll("th[data-k]").forEach((th) => {
    // A header can carry a glossary trigger (core/glossary.js) alongside
    // its label; detach it before reading textContent (else its own "?"
    // text ends up folded into the label) and reattach it as the sort
    // button's sibling once the button exists.
    const glossaryBtn = th.querySelector(".glossary-btn");
    if (glossaryBtn) glossaryBtn.remove();
    const label = th.textContent;
    th.textContent = "";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = label;
    th.appendChild(btn);
    if (glossaryBtn) th.appendChild(glossaryBtn);
    btn.addEventListener("click", () => {
      const k = th.dataset.k;
      sort = k === sort.key
        ? { key: k, dir: sort.dir === "asc" ? "desc" : "asc" }
        : { key: k, dir: columnType(k) === "number" ? "desc" : "asc" };
      render();
    });
  });

  return {
    setRows(newRows) {
      rows = newRows;
      render();
    },
  };
}
