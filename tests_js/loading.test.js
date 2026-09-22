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
  partitionsResults: "Loading GPU utilization history…",
  queueResults: "Loading current queue and wait history…",
  vramResults: "Loading VRAM history…",
  usersResults: "Loading user history…",
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
