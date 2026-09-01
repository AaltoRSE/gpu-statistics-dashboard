/* Text/markup formatting shared by every tab: number formatting, HTML
 * escaping, badges and the cross-tab link builders. Pure functions only —
 * no DOM reads beyond what callers pass in. */
"use strict";

export function fmt(v, digits) {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  if (Number.isNaN(n)) return String(v);
  return n.toFixed(digits === undefined ? 1 : digits);
}

export function fmtInt(v) {
  if (v === null || v === undefined) return "—";
  return Math.round(Number(v)).toLocaleString();
}

export function pctBar(v) {
  if (v === null || v === undefined) return "";
  const p = Math.max(0, Math.min(100, v));
  const cls = p < 40 ? "lo" : p < 75 ? "mid" : "hi";
  const pct = p.toFixed(0);
  return html`<span class="pct-cell"><span class="bar-track"><span class="bar ${cls}" style="width:${pct}%"></span></span><span class="pct-value">${pct}%</span></span>`;
}

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

// A value produced by one of this module's own markup builders (jobLink,
// pctBar, stateBadge, ...) is already-safe HTML. Wrapping it in raw() is
// the only way to opt out of html`` escaping a value — see html() below.
class SafeString {
  constructor(value) { this.value = value; }
}
export function raw(value) {
  return new SafeString(value);
}

// Tagged template: every interpolated value is HTML-escaped by default,
// unless it is wrapped in raw(). A table row built with this template
// cannot render an unescaped Slurm-supplied string (job name, username,
// node name, ...) by omission — the previous convention required calling
// escapeHtml() at every interpolation site by hand, and it was only ever
// as safe as the least careful edit to one of those call sites.
export function html(strings, ...values) {
  let out = strings[0];
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    out += (v instanceof SafeString ? v.value : escapeHtml(v)) + strings[i + 1];
  }
  return out;
}

export function escapeList(values) {
  const list = Array.isArray(values) ? values : values ? [values] : [];
  return list.map(escapeHtml).join(", ") || "—";
}

export function stateBadge(state, label = state) {
  if (!state) return "";
  const known = ["RUNNING", "COMPLETED", "PENDING", "IDLE", "FAILED", "CANCELLED",
                 "TIMEOUT", "DRAIN", "DRAINED", "DOWN", "DRAINING", "RESERVED",
                 "MIXED", "ALLOCATED", "PLANNED", "NOT_RESPONDING"];
  const cls = known.includes(state) ? state : "PENDING";
  return html`<span class="badge ${cls}">${label}</span>`;
}

// scontrol node states carry qualifiers after ``+`` (``IDLE+DRAIN``) and a
// trailing ``*`` marks a node that is presently not responding; the star is
// stripped. Every qualifier is rendered as a badge (neutral allocation
// states included, per the column contract); the parsed drain reason is
// exposed as the cell title by the caller.
export function nodeStateBadges(n) {
  const cleaned = (n.state_full || n.state || "").replace(/\*$/, "");
  const parts = cleaned.split("+")
    .map((p) => p.split(":")[0])
    .filter(Boolean);
  if (!parts.length) return "";
  return parts.map((p) => stateBadge(p, p.replace(/_/g, " "))).join(" ");
}

export function tsToDate(ts) {
  return new Date(ts * 1000).toISOString().slice(0, 16).replace("T", " ");
}

// Natural (numeric-aware) string comparison: "gpu2" < "gpu3" < "gpu15" <
// "gpu28", unlike localeCompare's lexicographic "gpu2" < "gpu15" < "gpu3".
// Used by every column sort so node/partition names order the same way in
// all tabs.
export function compareStrings(a, b) {
  a = String(a || "");
  b = String(b || "");
  const ra = a.split(/(\d+)/g);
  const rb = b.split(/(\d+)/g);
  const len = Math.max(ra.length, rb.length);
  for (let i = 0; i < len; i++) {
    const x = ra[i] || "", y = rb[i] || "";
    const nx = /^\d+$/.test(x) ? Number(x) : null;
    const ny = /^\d+$/.test(y) ? Number(y) : null;
    if (nx !== null && ny !== null) {
      if (nx !== ny) return nx - ny;
    } else if (nx !== null || ny !== null) {
      // Digit run vs non-digit run: shorter (prefix) side comes first.
      return x < y ? -1 : 1;
    } else if (x !== y) {
      return x.localeCompare(y);
    }
  }
  return a.length - b.length;
}

// entity-link (static/css/components.css) is the one class that styles
// all four link types; joblink/userlink/nodelink/partitionlink stay on the
// markup too because tabs/*.js's onRowClick handlers key off them to tell
// which kind of link a click landed on.
export function jobLink(jobid) {
  const href = "/job/" + encodeURIComponent(jobid);
  return html`<a class="entity-link joblink" href="${href}" data-job="${jobid}" title="open job ${jobid} in the Jobs tab">${jobid}</a>`;
}

export function userLink(user) {
  if (!user) return "—";
  const href = "/user/" + encodeURIComponent(user);
  return html`<a class="entity-link userlink" href="${href}" data-user="${user}" title="open ${user} in the Users tab">${user}</a>`;
}

export function nodeLink(node) {
  if (!node) return "—";
  const href = "/node/" + encodeURIComponent(node);
  return html`<a class="entity-link nodelink" href="${href}" data-node="${node}" title="open ${node} in the Nodes tab">${node}</a>`;
}

export function nodeLinks(values) {
  const list = Array.isArray(values) ? values : values ? [values] : [];
  return list.map(nodeLink).join(", ") || "—";
}

export function partitionLink(partition, label = partition) {
  if (!partition) return "—";
  const href = "/partition/" + encodeURIComponent(partition);
  return html`<a class="entity-link partitionlink" href="${href}" data-partition="${partition}" title="open ${partition} in the Partitions tab">${label}</a>`;
}
