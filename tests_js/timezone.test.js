// T-23: one display timezone (Europe/Helsinki) everywhere, and sacct's
// unlabeled local-time strings handled without ever constructing a Date
// from them (see core/format.js's own comment on why).
import assert from "node:assert/strict";
import { test } from "node:test";

import { tsToDate, fmtSacctTime } from "../static/js/core/format.js";

test("tsToDate renders in Europe/Helsinki, not UTC or the test runner's zone", () => {
  // 2025-01-01T00:00:00Z is Helsinki winter time (EET, UTC+2).
  assert.equal(tsToDate(1735689600), "2025-01-01 02:00");
});

test("tsToDate is DST-aware (EEST, UTC+3, in summer)", () => {
  // 2024-07-14T23:33:20Z is Helsinki summer time (EEST, UTC+3) -> next day.
  assert.equal(tsToDate(1721000000), "2024-07-15 02:33");
});

test("fmtSacctTime reformats Slurm's unlabeled local-time string", () => {
  assert.equal(fmtSacctTime("2026-08-29T02:00:00"), "2026-08-29 02:00");
});

test("fmtSacctTime passes through Slurm's sentinel values instead of parsing them", () => {
  assert.equal(fmtSacctTime("Unknown"), "Unknown");
  assert.equal(fmtSacctTime("INVALID"), "INVALID");
});

test("fmtSacctTime renders an empty/missing value as an em dash, not a blank cell", () => {
  assert.equal(fmtSacctTime(""), "—");
  assert.equal(fmtSacctTime(undefined), "—");
  assert.equal(fmtSacctTime(null), "—");
});

test("fmtSacctTime never throws on garbage input, and shows it verbatim", () => {
  assert.equal(fmtSacctTime("not-a-date-at-all"), "not-a-date-at-all");
});
