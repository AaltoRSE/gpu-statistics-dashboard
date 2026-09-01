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
  return '<span class="pct-cell"><span class="bar-track">' +
    '<span class="bar ' + cls + '" style="width:' + p.toFixed(0) + '%"></span></span>' +
    '<span class="pct-value">' + p.toFixed(0) + "%</span></span>";
}

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
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
  return '<span class="badge ' + cls + '">' + escapeHtml(label) + "</span>";
}

// scontrol node states carry qualifiers after ``+`` (``IDLE+DRAIN``) and a
// trailing ``*`` marks a node that is presently not responding; the star is
// stripped. Every qualifier is rendered as a badge (neutral allocation
// states included, per the column contract); the parsed drain reason is
// exposed as the cell title by the caller.
export function nodeStateBadges(n) {
  const raw = (n.state_full || n.state || "").replace(/\*$/, "");
  const parts = raw.split("+")
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

export function jobLink(jobid) {
  const safe = escapeHtml(jobid);
  const href = escapeHtml("/job/" + encodeURIComponent(jobid));
  return '<a class="joblink" href="' + href + '" data-job="' + safe +
    '" title="open job ' + safe + ' in the Jobs tab">' + safe + "</a>";
}

export function userLink(user) {
  if (!user) return "—";
  const safe = escapeHtml(user);
  const href = escapeHtml("/user/" + encodeURIComponent(user));
  return '<a class="userlink" href="' + href + '" data-user="' + safe +
    '" title="open ' + safe + ' in the Users tab">' + safe + "</a>";
}

export function nodeLink(node) {
  if (!node) return "—";
  const safe = escapeHtml(node);
  const href = escapeHtml("/node/" + encodeURIComponent(node));
  return '<a class="nodelink" href="' + href + '" data-node="' + safe +
    '" title="open ' + safe + ' in the Nodes tab">' + safe + "</a>";
}

export function nodeLinks(values) {
  const list = Array.isArray(values) ? values : values ? [values] : [];
  return list.map(nodeLink).join(", ") || "—";
}

export function partitionLink(partition, label = partition) {
  if (!partition) return "—";
  const safe = escapeHtml(partition);
  const href = escapeHtml("/partition/" + encodeURIComponent(partition));
  return '<a class="partitionlink" href="' + href + '" data-partition="' + safe +
    '" title="open ' + safe + ' in the Partitions tab">' +
    escapeHtml(label) + "</a>";
}
