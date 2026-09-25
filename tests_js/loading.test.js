// DOM contract for the historical panels' loading labels: each
// window-driven panel names its own subject while loading, the live
// Nodes list keeps its generic message, and a loader re-entering a
// loading state resets any batch-progress text a prior load left in
// the chip (plain textContent — an escaped "&hellip;" once rendered
// as the literal "hellip"). Node's own test runner + jsdom against
// the app's real index.html.
import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";
import { readFileSync } from "fs";

const html = readFileSync(new URL("../static/index.html", import.meta.url), "utf8");

const EXPECTED = {
  jobDetailResults: "Loading job detail history…",
  jobEfficiencyResults: "Loading job history…",
  jobsResults: "Loading job history…",
  partitionsResults: "Loading GPU type summary…",
  queueResults: "Loading current queue and wait history…",
  vramResults: "Loading VRAM history…",
  usersResults: "Loading user history…",
  groupsResults: "Loading group efficiency…",
  groupMembersResults: "Loading group members…",
  userJobsResults: "Loading selected-user job history…",
  nodeDetailResults: "Loading node history…",
};

test("every historical panel carries its specific initial label", () => {
  const dom = new JSDOM(html);
  const doc = dom.window.document;
  for (const [id, label] of Object.entries(EXPECTED)) {
    const chip = doc.getElementById(id)?.querySelector(".results-loading");
    assert.ok(chip, `#${id} has a loading chip`);
    assert.equal(chip.textContent, label, `#${id} initial label`);
    assert.ok(!chip.textContent.includes("hellip"));
  }
  dom.window.close();
});

test("the live Nodes list keeps its generic loading message", () => {
  const dom = new JSDOM(html);
  const doc = dom.window.document;
  const chip = doc.querySelector("#nodesResults .results-loading");
  assert.ok(chip);
  assert.equal(chip.textContent, "Data is loading…");
  dom.window.close();
});

// panel.js registers a 60s freshness interval at import time; stub it
// exactly like the queue-test harness so the runner can exit.
const realSetInterval = global.setInterval;
const realClearInterval = global.clearInterval;

test("re-entering loading resets stale batch-progress copy", async () => {
  global.setInterval = () => 0;
  global.clearInterval = () => {};
  global.document = new JSDOM(html).window.document;
  try {
    const panel = await import("../static/js/core/panel.js");
    // Simulate a prior load's batch-progress text (as setQueueProgress
    // writes it), then start a fresh loading state with the label.
    panel.setResultsLoadingMessage("queueResults",
      "Loading wait history: batch 9 of 30…");
    panel.setResultsLoading("queueResults", true,
      "Loading current queue and wait history…");
    const chip = global.document.querySelector("#queueResults .results-loading");
    assert.equal(chip.textContent, "Loading current queue and wait history…");
    assert.ok(!chip.textContent.includes("hellip"));
    // The message is assigned as text, never interpreted HTML.
    panel.setResultsLoadingMessage("queueResults", "<b>not markup</b>");
    assert.equal(chip.textContent, "<b>not markup</b>");
    assert.equal(chip.querySelector("b"), null);
  } finally {
    global.setInterval = realSetInterval;
    global.clearInterval = realClearInterval;
  }
});

// The panels a tab loads on arrival must never sit blank while their first
// fetch runs: each starts with the loading class so its chip shows before
// any JS has run. The display:none detail panels are toggled by their own
// tabs instead and deliberately carry no initial state.
test("every data panel starts in the loading state", () => {
  const dom = new JSDOM(html);
  const doc = dom.window.document;
  for (const id of [
    "jobEfficiencyResults", "jobsResults", "partitionsResults",
    "queueResults", "vramResults", "usersResults", "groupsResults",
    "nodesResults",
  ]) {
    const panel = doc.getElementById(id);
    assert.ok(panel.classList.contains("loading"), `#${id} starts loading`);
    assert.ok(panel.querySelector(".results-loading"), `#${id} has its chip`);
  }
  for (const id of ["jobDetailResults", "userJobsResults",
                    "groupMembersResults", "nodeDetailResults"]) {
    assert.ok(!doc.getElementById(id).classList.contains("loading"),
      `#${id} stays toggle-managed by its tab`);
  }
  dom.window.close();
});

test("a loading panel shows a chip in every card, batch text reaches all of them", async () => {
  global.setInterval = () => 0;
  global.clearInterval = () => {};
  global.document = new JSDOM(html).window.document;
  try {
    const panel = await import("../static/js/core/panel.js");
    panel.setResultsLoading("partitionsResults", true,
      "Loading GPU type summary…");
    const el = global.document.getElementById("partitionsResults");
    const cards = el.querySelectorAll(".card");
    assert.ok(cards.length > 1, "the partitions panel has several cards");
    const chips = el.querySelectorAll(".card .results-loading");
    assert.equal(chips.length, cards.length, "one chip per card");
    for (const chip of chips) {
      assert.equal(chip.textContent, "Loading GPU type summary…");
    }
    // Batch progress (setResultsLoadingMessage) rewrites every chip, so no
    // card keeps an earlier label while its siblings report batch state.
    panel.setResultsLoadingMessage("partitionsResults",
      "Loading wait history: batch 2 of 9…");
    for (const chip of chips) {
      assert.equal(chip.textContent, "Loading wait history: batch 2 of 9…");
      assert.ok(!chip.textContent.includes("hellip"));
    }
    // The panel-level chip stands down once card chips exist...
    assert.ok(el.classList.contains("has-card-chips"));
    // ...a second loading state reuses them instead of cloning anew...
    panel.setResultsLoading("partitionsResults", true,
      "Loading GPU type summary…");
    assert.equal(el.querySelectorAll(".card .results-loading").length, cards.length);
    // ...and clearing the state drops the loading class (CSS hides the chips).
    panel.setResultsLoading("partitionsResults", false);
    assert.equal(el.classList.contains("loading"), false);
  } finally {
    global.setInterval = realSetInterval;
    global.clearInterval = realClearInterval;
  }
});

test("a panel with no .card children keeps its panel-level chip", async () => {
  global.setInterval = () => 0;
  global.clearInterval = () => {};
  global.document = new JSDOM(html).window.document;
  try {
    const panel = await import("../static/js/core/panel.js");
    const el = global.document.getElementById("jobDetailResults");
    el.querySelectorAll(".card").forEach((card) => card.remove());
    panel.setResultsLoading("jobDetailResults", true, "Loading job detail history…");
    const chip = el.querySelector(".results-loading");
    assert.ok(chip, "the panel-level chip is the fallback");
    assert.equal(chip.textContent, "Loading job detail history…");
    assert.ok(!el.classList.contains("has-card-chips"));
  } finally {
    global.setInterval = realSetInterval;
    global.clearInterval = realClearInterval;
  }
});
