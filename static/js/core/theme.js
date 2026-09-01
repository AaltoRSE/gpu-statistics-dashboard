/* Dark is the default; light is a saved preference (localStorage) or the
 * OS-level preference. Toggling recolors the document; the caller supplies
 * an onChange callback to re-render whichever plots are on screen (plot
 * colors are baked in at draw), so this module never needs to know about
 * tabs or charts. */
"use strict";

import { $ } from "./dom.js";

const THEME_KEY = "gpu-dash-theme";

export function currentTheme() {
  const t = localStorage.getItem(THEME_KEY);
  return t === "light" || t === "dark"
    ? t
    : (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches)
      ? "light" : "dark";
}

export function applyTheme(t) {
  if (t === "light") document.documentElement.dataset.theme = "light";
  else delete document.documentElement.dataset.theme;
  const btn = $("themeBtn");
  if (btn) btn.innerHTML =
    (t === "light" ? "&#9790;&#xFE0E; dark" : "&#9728;&#xFE0E; light");
}

// Applies the initial theme and wires the toggle button. onChange is called
// after every toggle so the current page can re-render its plots.
export function initTheme(onChange) {
  applyTheme(currentTheme());
  $("themeBtn").addEventListener("click", () => {
    const next = currentTheme() === "light" ? "dark" : "light";
    localStorage.setItem(THEME_KEY, next);
    applyTheme(next);
    if (onChange) onChange();
  });
}
