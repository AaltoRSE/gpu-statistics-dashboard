/* Plotly wiring shared by every chart: the color palette, the theme-aware
 * hover styling, and the render entry point. partBarColor lives here (not
 * in a tab module) because both the Jobs efficiency charts and the
 * Partitions bar chart color by the same efficiency-band rule. */
"use strict";

import { $ } from "./dom.js";
import { currentTheme } from "./theme.js";

const PLOT_CFG = { displayModeBar: false, responsive: true };
const COLORS = [
  "#4fc3f7", "#81c784", "#ffb74d", "#ba68c8", "#4dd0e1", "#f06292",
  "#aed581", "#7986cb", "#ffd54f", "#a1887f", "#90a4ae", "#e57373",
];
const THEME_LIGHT = {
  text: "#1c2436", dim: "#5a6a8a", grid: "#c9d3e3",
  colors: ["#005a9c", "#1b6e1b", "#a64900", "#6a1b9a", "#006064",
           "#9c1458", "#3d6518", "#283593", "#8d5b00", "#5d4037",
           "#37474f", "#a31515"],
};

export function plotTheme() {
  const light = currentTheme() === "light";
  return {
    font: { color: light ? THEME_LIGHT.text : "#dce3f2", size: 11 },
    grid: light ? THEME_LIGHT.grid : "#2a3552",
    colors: light ? THEME_LIGHT.colors : COLORS,
    ok: light ? "#2e7d32" : "#66bb6a",
    warn: light ? "#ef6c00" : "#ffa726",
    bad: light ? "#d32f2f" : "#ef5350",
    acc: light ? "#0288d1" : "#4fc3f7",
    idle: light ? "#c9d3e3" : "#2a3552",
  };
}

export function renderPlot(elId, traces, layout) {
  // Plotly.react diffs traces and layout, so the same call serves fresh
  // filter data and theme-only palette updates.
  // Apply a theme-aware hover popup: Plotly's default hover is a light box
  // with dark text, which is hard to read on the dark plot. Merge a
  // chart-specific hoverlabel over these defaults so a specialized chart
  // can opt out without duplicating the palette.
  const light = currentTheme() === "light";
  const hover = layout.hoverlabel || {};
  layout = Object.assign({}, layout, {
    hoverlabel: Object.assign({
      bgcolor: light ? "#ffffff" : "#171e2e",
      bordercolor: light ? "#0288d1" : "#4fc3f7",
      font: { color: light ? "#1c2436" : "#dce3f2", size: 12 },
      namelength: -1,
    }, hover),
  });
  return Plotly.react($(elId), traces, layout, PLOT_CFG);
}

export function partBarColor(v) {
  const th = plotTheme();
  return v < 40 ? th.bad : v < 75 ? th.warn : th.ok;
}
