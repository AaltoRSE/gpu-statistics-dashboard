// The batch counter must not outlive its phase. The shared sacct dump
// publishes progress only while its chunks run (the key is popped on
// completion), so on the slow, Prometheus-dominated remainder of a VRAM or
// queue request the polls answer null — and the chip used to keep showing
// the last observed "batch X of Y" frozen for the rest of the load. These
// tests pin: a poll with no in-flight batches (null, or done == total)
// hands the chip back to the panel's base message, and a long load with no
// batch progress shows elapsed seconds instead of a static sentence.
//
// Node's own test runner + jsdom against the app's real index.html, same
// harness shape as loading.test.js: timers are stubbed so the runner can
// never hang.
import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";
import { readFileSync } from "fs";

const html = readFileSync(new URL("../static/index.html", import.meta.url), "utf8");

const realSetInterval = global.setInterval;
const realClearInterval = global.clearInterval;

function boot() {
  global.document = new JSDOM(html).window.document;
}

test("a null progress poll resets a frozen batch counter to the base message", async () => {
  global.setInterval = () => 0;
  global.clearInterval = () => {};
  boot();
  try {
    const panel = await import("../static/js/core/panel.js");
    panel.setResultsLoading("vramResults", true, "Loading VRAM distribution…");
    // a mid-flight batch state lands on the chip…
    panel.setResultsLoadingMessage("vramResults",
      "Loading VRAM distribution: batch 13 of 31…");
    assert.match(chipText(), /batch 13 of 31/);
    // …the dump finishes and pops its key; the reset (what pollProgress's
    // onIdle calls) must hand the chip back to the base message, not leave
    // the counter frozen for the remaining, slower part of the request.
    panel.resetResultsLoadingMessage("vramResults");
    assert.equal(chipText(),
      "Loading VRAM distribution…");
    // the elapsed suffix only applies once the load has actually run long
    // enough; immediately after the reset the plain message stands.
    assert.ok(!chipText().includes("("), chipText());
  } finally {
    global.setInterval = realSetInterval;
    global.clearInterval = realClearInterval;
    global.document = undefined;
  }
});

test("a long load with no batch progress shows elapsed seconds on the chip", async () => {
  const ticks = [];
  global.setInterval = (fn, ms) => { ticks.push({ fn, ms }); return ticks.length; };
  global.clearInterval = () => {};
  boot();
  try {
    const panel = await import("../static/js/core/panel.js?cb=elapsed");
    const ticker = ticks.at(-1);
    assert.equal(ticker.ms, 1000, "the chip ticker runs once a second");
    panel.setResultsLoading("vramResults", true, "Loading VRAM distribution…");
    // within the first seconds the plain message stands (no suffix churn
    // for fast loads)
    ticker.fn();
    assert.equal(chipText(), "Loading VRAM distribution…");
    // past five seconds the elapsed time appears, and advances
    const start = Date.now();
    global.Date = class extends Date {
      static now() { return start + 7000; }
    };
    ticker.fn();
    assert.equal(chipText(), "Loading VRAM distribution… (7 s)");
    global.Date = class extends Date {
      static now() { return start + 9500; }
    };
    ticker.fn();
    assert.equal(chipText(), "Loading VRAM distribution… (9 s)");
    // the load ends: the state is dropped, the suffix stops
    panel.setResultsLoading("vramResults", false);
    global.Date = Date;
    ticker.fn();
    assert.equal(chipText(), "Loading VRAM distribution… (9 s)",
      "no further writes once the panel is no longer loading");
  } finally {
    global.setInterval = realSetInterval;
    global.clearInterval = realClearInterval;
    global.document = undefined;
  }
});

test("batch text suppresses the elapsed suffix while the counter is live", async () => {
  const ticks = [];
  global.setInterval = (fn, ms) => { ticks.push({ fn, ms }); return ticks.length; };
  global.clearInterval = () => {};
  boot();
  try {
    const panel = await import("../static/js/core/panel.js?cb=suppress");
    const ticker = ticks.at(-1);
    // record the load's start with the real clock BEFORE mocking Date.now,
    // so the mocked later reads measure elapsed time from it
    const start = Date.now();
    panel.setResultsLoading("queueResults", true,
      "Loading current queue and wait history…");
    global.Date = class extends Date {
      static now() { return start + 20000; }
    };
    // a live batch counter owns the chip; the ticker must not clobber it
    panel.setResultsLoadingMessage("queueResults",
      "Loading wait history: batch 3 of 31…");
    ticker.fn();
    assert.match(chipText("#queueResults"), /batch 3 of 31/);
    assert.ok(!chipText("#queueResults").includes("(20 s)"), chipText("#queueResults"));
    // batch phase over: the reset restores the (suffixed) base message
    panel.resetResultsLoadingMessage("queueResults");
    assert.match(chipText("#queueResults"),
      /^Loading current queue and wait history… \(20 s\)$/);
  } finally {
    global.setInterval = realSetInterval;
    global.clearInterval = realClearInterval;
    global.Date = Date;
    global.document = undefined;
  }
});

function chipText(sel = "#vramResults") {
  return global.document.querySelector(sel + " .results-loading").textContent;
}
