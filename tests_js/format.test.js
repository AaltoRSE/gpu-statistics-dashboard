// Escaping tests for static/js/core/format.js's html``/raw() primitive
// (T-20). Node's own test runner + assert; jsdom stands in for the
// browser DOM so a test can assert on the *actual parsed structure* of
// rendered markup, not just its string form — a malicious job name that
// ends up as an <img> element is the real-world failure this guards
// against, and only a DOM parse can tell the difference between that and
// the same string sitting inertly inside a text node.
import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";

import { html, raw, escapeHtml, jobLink, pctBar, stateBadge } from "../static/js/core/format.js";

test("html`` escapes a plain interpolation", () => {
  const out = html`<td>${"<script>alert(1)</script>"}</td>`;
  assert.equal(out, "<td>&lt;script&gt;alert(1)&lt;/script&gt;</td>");
});

test("html`` leaves a raw()-wrapped value untouched", () => {
  const trusted = "<b>bold</b>";
  const out = html`<td>${raw(trusted)}</td>`;
  assert.equal(out, "<td><b>bold</b></td>");
});

test("html`` escapes quotes so an attribute interpolation can't break out", () => {
  const payload = 'x" onmouseover="alert(1)"';
  const out = html`<a title="${payload}">x</a>`;
  const dom = new JSDOM(`<div>${out}</div>`);
  const a = dom.window.document.querySelector("a");
  assert.equal(a.getAttribute("onmouseover"), null,
    "the interpolated quote must not have closed the title attribute early");
  assert.equal(a.getAttribute("title"), payload,
    "the whole payload must still be readable back out of the one title attribute");
});

// The scenario the ticket asks for: a job whose name is an XSS payload
// must render as inert text, never as a real <img> element, when its row
// markup is parsed as HTML — which is exactly what tbody.innerHTML =
// renderRow(job) does in core/table.js.
test("a job row with an <img onerror> name renders as text, not an element", () => {
  const maliciousName = '<img src=x onerror="window.__pwned = true">';
  // Mirrors tabs/jobs.js's jobRowHtml: the job name is a plain-text
  // interpolation (no raw()), everything else in this row is irrelevant
  // to the test and omitted.
  const row = html`<tr class="row" data-job="20019445"><td title="${maliciousName}">${maliciousName}</td></tr>`;

  const dom = new JSDOM(`<table><tbody>${row}</tbody></table>`);
  const doc = dom.window.document;

  assert.equal(doc.querySelectorAll("img").length, 0,
    "the malicious name must not become a real <img> element");
  assert.ok(dom.window.__pwned === undefined,
    "onerror must never have had a chance to fire");
  assert.equal(doc.querySelector("td").textContent, maliciousName,
    "the name must still be visible, verbatim, as text");
  assert.equal(doc.querySelector("td").getAttribute("title"), maliciousName);
});

test("jobLink escapes a job id used in both an href and a data attribute", () => {
  const out = jobLink('19807768"><script>alert(1)</script>');
  const dom = new JSDOM(`<table><tbody><tr>${out}</tr></tbody></table>`);
  const doc = dom.window.document;
  assert.equal(doc.querySelectorAll("script").length, 0);
  assert.equal(doc.querySelectorAll("a").length, 1);
});

test("pctBar and stateBadge never need a caller-side escapeHtml call", () => {
  // Regression guard for the T-20 refactor itself: these two markup
  // builders now escape internally via html``, so a caller interpolating
  // their output only needs raw(), never escapeHtml() first.
  assert.equal(pctBar(150), pctBar(100)); // clamped, no throw
  assert.match(stateBadge("RUNNING"), /^<span class="badge RUNNING">RUNNING<\/span>$/);
  // An unrecognized state still renders (falls back to the PENDING class)
  // instead of throwing, and never trusts the state string as markup.
  const weird = stateBadge('<img onerror=1 src=x>');
  const dom = new JSDOM(`<table><tbody><tr><td>${weird}</td></tr></tbody></table>`);
  assert.equal(dom.window.document.querySelectorAll("img").length, 0);
});

test("escapeHtml is unaffected by this change (still exported, still escapes)", () => {
  assert.equal(escapeHtml('<x>&"\''), "&lt;x&gt;&amp;&quot;&#39;");
});
