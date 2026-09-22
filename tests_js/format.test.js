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

import { html, raw, escapeHtml, jobLink, userLink, jobDetailTitle, pctBar, stateBadge } from "../static/js/core/format.js";

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
// Issue #2: the job detail title ("Job <id> — <name> (<user>) · <state>")
// renders the user through this same builder, so the title's user link is
// whatever userLink produces — the /user/<name> deep link the Users tab
// resolves.
test("userLink targets the /user/<name> deep link and keeps the name visible", () => {
  const dom = new JSDOM(`<div>${userLink("alice")}</div>`);
  const a = dom.window.document.querySelector("a");
  assert.equal(a.getAttribute("href"), "/user/alice");
  assert.equal(a.getAttribute("data-user"), "alice");
  assert.equal(a.textContent, "alice");
});

test("userLink escapes a username in href, data attribute and label", () => {
  const payload = 'bob"><script>alert(1)</script>';
  const dom = new JSDOM(`<div>${userLink(payload)}</div>`);
  const doc = dom.window.document;
  assert.equal(doc.querySelectorAll("script").length, 0);
  const links = doc.querySelectorAll("a");
  assert.equal(links.length, 1,
    "the payload must stay inside the one <a> tag, not break out");
  const a = links[0];
  assert.equal(a.textContent, payload,
    "the username must still be visible verbatim as the link label");
  assert.equal(a.getAttribute("href"),
    "/user/" + encodeURIComponent(payload),
    "the href must carry the encoded username");
});
// Issue #2, rendered through the job detail title itself: the user is the
// title's link and must carry the same /user/<name> deep link the table's
// user column uses.
test("jobDetailTitle renders the user as the /user/<name> link", () => {
  const out = jobDetailTitle("123", { name: "train.sh", user: "alice",
                                     state: "RUNNING" });
  const dom = new JSDOM(`<div>${out}</div>`);
  const doc = dom.window.document;
  const a = doc.querySelector("a");
  assert.equal(a.getAttribute("href"), "/user/alice");
  assert.equal(doc.querySelector("div").textContent,
    "Job 123 — train.sh (alice) · RUNNING");
});

test("jobDetailTitle keeps the pre-link \"?\" placeholders for missing fields", () => {
  // The textContent title rendered "(?)" for a job whose metadata never
  // resolved; the builder must reproduce that exactly (never the table's
  // "—" dash, which means something different in this title) and keep the
  // remaining fields' own fallbacks.
  assert.equal(jobDetailTitle("123", {}), "Job 123 — ? (?) · ?");
  // With only a user resolved, the user renders as the link while name
  // and state keep their "?" fallbacks.
  const dom = new JSDOM(`<div>${jobDetailTitle("123", { user: "alice" })}</div>`);
  assert.equal(dom.window.document.querySelector("div").textContent,
    "Job 123 — ? (alice) · ?");
  assert.equal(dom.window.document.querySelector("a").getAttribute("href"),
    "/user/alice");
});

test("jobDetailTitle escapes name and state, not the user label", () => {
  const out = jobDetailTitle('9"onload="x', { name: "<img onerror=1>",
                                              user: "alice",
                                              state: "<b>RUN</b>" });
  const dom = new JSDOM(`<div>${out}</div>`);
  const doc = dom.window.document;
  assert.equal(doc.querySelectorAll("img").length, 0);
  assert.equal(doc.querySelectorAll("b").length, 0);
  // The jobid is a numeric Slurm field, but even if it carried markup it
  // interpolates as plain text.
  assert.equal(doc.querySelector("div").textContent,
    'Job 9"onload="x — <img onerror=1> (alice) · <b>RUN</b>');
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
