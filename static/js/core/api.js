/* Fetch wrapper. */
"use strict";

// T-27: this used to also drive a fixed-position "loaded in N ms" chip
// (this module's own status()), removed — with concurrent panel loads (the
// common case: e.g. Partitions plus its independent VRAM fetch) it just
// reported whichever request happened to finish last, which is not
// meaningfully attributable to any one panel. Each panel's own loading
// state (setResultsLoading) and, once loaded, its freshness clock
// (core/panel.js's tickFreshness, T-22) already say what this chip only
// approximated for the page as a whole.
export async function api(path) {
  const resp = await fetch(path);
  if (!resp.ok) {
    let detail = resp.status + " " + resp.statusText;
    try { detail += " — " + JSON.stringify((await resp.json()).detail || ""); } catch (_) {}
    throw new Error(detail);
  }
  return await resp.json();
}
