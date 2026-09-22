// DOM contract for the dashboard's time-window selectors: the Jobs,
// Partitions, and Users tabs must each expose the same five windows —
// 24 h, 3 d, 7 d, 14 d, 30 d — in the same order, with 24 h selected by
// default. One shared contract covers all three selectors and fails if
// a single tab diverges. Node's own test runner + jsdom against the
// app's real index.html.
import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";
import { readFileSync } from "fs";

const html = readFileSync(new URL("../static/index.html", import.meta.url), "utf8");

const EXPECTED = [
  ["24", "last 24 h"],
  ["72", "last 3 days"],
  ["168", "last 7 days"],
  ["336", "last 14 days"],
  ["720", "last 30 days"],
];

test("every tab's window selector offers the same five ordered windows", () => {
  const dom = new JSDOM(html);
  const doc = dom.window.document;
  for (const id of ["jWindow", "pWindow", "uWindow"]) {
    const select = doc.getElementById(id);
    assert.ok(select, `#${id} missing from index.html`);
    const options = [...select.querySelectorAll("option")];
    assert.deepEqual(
      options.map((o) => [o.value, o.textContent.trim()]),
      EXPECTED,
      `#${id} options diverge from the shared five-window contract`,
    );
  }
});

test("24 h is the only preselected window on every tab", () => {
  const dom = new JSDOM(html);
  const doc = dom.window.document;
  for (const id of ["jWindow", "pWindow", "uWindow"]) {
    const select = doc.getElementById(id);
    assert.equal(select.value, "24", `#${id} default is not 24 h`);
    const selected = [...select.querySelectorAll("option")]
      .filter((o) => o.selected);
    assert.deepEqual(
      selected.map((o) => o.value),
      ["24"],
      `#${id} must have exactly one selected option: 24`,
    );
  }
});
