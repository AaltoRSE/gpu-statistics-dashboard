/* Fetch wrapper and the fixed-position load-time status chip. */
"use strict";

import { $ } from "./dom.js";

export function status(msg) { $("status").textContent = msg || ""; }

export async function api(path) {
  const t0 = performance.now();
  let resp;
  try {
    resp = await fetch(path);
  } catch (e) {
    throw e;
  }
  if (!resp.ok) {
    let detail = resp.status + " " + resp.statusText;
    try { detail += " — " + JSON.stringify((await resp.json()).detail || ""); } catch (_) {}
    throw new Error(detail);
  }
  const data = await resp.json();
  status("loaded in " + Math.round(performance.now() - t0) + " ms");
  return data;
}
