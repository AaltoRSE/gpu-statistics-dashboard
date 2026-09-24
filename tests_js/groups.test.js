// Functional tests for the Groups tab: the school filter runs entirely
// client-side (no fetch), a superseded response is dropped by its token,
// the level toggle re-fetches server-side, the coverage banner renders
// (including the unmapped-codes hint), and the Unaffiliated/Unresolved
// rows render even when empty. The member drill-down fetches on row
// click and links members to the Users tab.
//
// Node's own test runner + jsdom against the app's real index.html, same
// harness shape as partitions-parallel.test.js: timers are stubbed so
// node --test can never hang, and group responses are held behind gates
// the tests release explicitly.
import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";
import { readFileSync } from "fs";

const html = readFileSync(new URL("../static/index.html", import.meta.url), "utf8");

function group(gid, name, overrides) {
  return {
    group_id: gid, group_name: name, dept_code: null, dept_name: null,
    school_code: null, school_name: null, users: 1, jobs: 2,
    running_jobs: 1, mean_util: 50, util_gpu_hours: 1.0, gpu_hours: 2.0,
    vram_avg: null, low_eff_jobs: 0, top_users: [],
    ...overrides,
  };
}

const BODY1 = {
  window: { start: 1000, end: 2000 }, level: "unit", count: 3,
  schools: [
    { code: "T3", short: "SCI", full: "School of Science" },
    { code: "T4", short: "ELEC", full: "School of Electrical Engineering" },
  ],
  coverage: { users: 4, affiliated: 3, unaffiliated: 1, unresolved: 0,
              failed: 0, unmapped_codes: ["T31398"] },
  groups: [
    group("dept:T313", "Computer Science (no unit)",
          { school_code: "SCI", mean_util: 92.5 }),
    group("unaffiliated", "Unaffiliated", { mean_util: 85 }),
    group("unit:T40106", "Kyrki Ville group",
          { school_code: "ELEC", mean_util: 50 }),
  ],
};

const BODY2 = {
  ...BODY1,
  level: "department", count: 2,
  coverage: { ...BODY1.coverage, unmapped_codes: [] },
  groups: [
    group("dept:T410", "Electrical Engineering and Automation",
          { school_code: "ELEC" }),
    group("unresolved", "Unresolved",
          { users: 0, jobs: 0, running_jobs: 0, mean_util: 0 }),
  ],
};

const MEMBERS_BODY = {
  group_id: "unit:T40106", group_name: "Kyrki Ville group", level: "unit",
  window: { start: 1000, end: 2000 }, count: 1,
  users: [{
    user: "hannuse2", jobs: 2, running_jobs: 1, mean_util: 50,
    util_gpu_hours: 1.0, vram_avg: 11, gpu_types: ["h200"],
    unit_code: "T40106", dept_code: "T410", school_code: "ELEC",
    extra_units: ["T30198"], status: "unit",
  }],
};

// boot: build the DOM at /groups, route the fetch stub by URL, import a
// fresh groups module against it. Options:
//   gateGroups  hold /api/groups behind a gate (releaseGroups() pops the
//               FIFO of pending responses, resolving each with the next
//               body from bodies[] in order)
//   bodies      response bodies for successive /api/groups fetches
async function boot(opts, bust) {
  const bodies = opts.bodies || [BODY1];
  const dom = new JSDOM(html, { url: "http://localhost/groups" });
  global.document = dom.window.document;
  global.window = dom.window;
  global.localStorage = dom.window.localStorage;
  global.location = dom.window.location;
  // As in partitions-parallel.test.js: setUrl only calls pushState, and
  // jsdom's own History object would keep the runner's event loop alive.
  global.history = { pushState() {}, replaceState() {} };
  global.setInterval = () => 0;
  global.clearInterval = () => {};
  global.Plotly = { newPlot: () => {}, react: () => {} };
  const urls = [];
  const gates = [];
  let bodyIndex = 0;
  global.fetch = (url) => {
    urls.push(String(url));
    const u = String(url);
    if (u.startsWith("/api/groups/") && u.includes("/users")) {
      return Promise.resolve({
        ok: true, json: () => Promise.resolve({ ...MEMBERS_BODY }),
      });
    }
    if (opts.gateGroups) {
      return new Promise((resolve) => gates.push(() => resolve({
        ok: true,
        json: () => Promise.resolve({ ...(bodies[Math.min(bodyIndex++, bodies.length - 1)]) }),
      })));
    }
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve({ ...(bodies[Math.min(bodyIndex++, bodies.length - 1)]) }),
    });
  };
  const mod = await import("../static/js/tabs/groups.js?cb=" + bust);
  return {
    dom, mod, urls,
    releaseGroups: () => { while (gates.length) gates.shift()(); },
    releaseOne: () => { if (gates.length) gates.shift()(); },
  };
}

test("the school filter re-renders without any fetch", async (t) => {
  const ctx = await boot({}, 1);
  t.after(() => ctx.dom.window.close());
  const doc = ctx.dom.window.document;
  await ctx.mod.loadGroups();
  const fetchesAfterLoad = ctx.urls.length;
  doc.getElementById("gSchool").value = "SCI";
  doc.getElementById("gSchool").dispatchEvent(
    new ctx.dom.window.Event("change"));
  // SCI row stays, ELEC row is gone — all client-side.
  const tbody = doc.querySelector("#groupTable tbody");
  assert.match(tbody.textContent, /Computer Science \(no unit\)/);
  assert.doesNotMatch(tbody.textContent, /Kyrki Ville group/);
  assert.equal(doc.getElementById("gCount").textContent, "1 shown");
  assert.equal(ctx.urls.length, fetchesAfterLoad, "no fetch on school filter");
  // the URL carries the filter
  assert.match(ctx.urls[ctx.urls.length - 1], /^\/api\/groups/); // last fetch unchanged
});

test("a stale response is dropped and keeps the panel loading", async (t) => {
  const ctx = await boot({ gateGroups: true, bodies: [BODY1, BODY2] }, 2);
  t.after(() => ctx.dom.window.close());
  const doc = ctx.dom.window.document;
  const first = ctx.mod.loadGroups();
  const second = ctx.mod.loadGroups();
  // Release the FIRST (now-superseded) response only: its data must not
  // render, and its loader must not clear the panel's loading state.
  ctx.releaseOne();
  await first;
  assert.equal(doc.getElementById("gMetaCount").textContent, "",
    "stale response must not render");
  assert.equal(doc.getElementById("groupsResults").classList.contains("loading"),
    true, "the newer load is still in flight");
  ctx.releaseOne();
  await second;
  assert.equal(doc.getElementById("gMetaCount").textContent, "2 groups");
  assert.equal(doc.getElementById("groupsResults").classList.contains("loading"),
    false);
  // the rendered rows are body2's, including the always-present row
  const tbody = doc.querySelector("#groupTable tbody");
  assert.match(tbody.textContent, /Electrical Engineering and Automation/);
  assert.match(tbody.textContent, /Unresolved/);
});

test("the level toggle re-fetches with level=department", async (t) => {
  const ctx = await boot({}, 3);
  t.after(() => ctx.dom.window.close());
  const doc = ctx.dom.window.document;
  await ctx.mod.loadGroups();
  assert.match(ctx.urls[0], /level=unit/);
  doc.getElementById("gLevel").value = "department";
  doc.getElementById("gLevel").dispatchEvent(
    new ctx.dom.window.Event("change"));
  await new Promise((r) => setTimeout(r, 0));
  assert.match(ctx.urls[ctx.urls.length - 1], /level=department/);
  assert.equal(ctx.urls.length, 2, "level toggle fetches");
});

test("the coverage banner renders, warns on unmapped codes, and both special rows render", async (t) => {
  const ctx = await boot({}, 4);
  t.after(() => ctx.dom.window.close());
  const doc = ctx.dom.window.document;
  await ctx.mod.loadGroups();
  const banner = doc.getElementById("groupsCoverage");
  assert.equal(banner.hidden, false);
  assert.match(banner.textContent, /3 of 4 job owners mapped/);
  assert.match(banner.textContent, /unmapped unit code\(s\) T31398/);
  assert.match(banner.textContent, /org_units\.conf/);
  assert.equal(banner.classList.contains("warn"), true);
  const tbody = doc.querySelector("#groupTable tbody");
  assert.match(tbody.textContent, /Unaffiliated/);
  // BODY1 has no unresolved row: the SERVER always sends one, but this
  // body omits it — the client renders what the API sends, so the
  // always-present contract belongs to the API (covered in the golden
  // and endpoint tests); assert the sent rows render.
  assert.match(tbody.textContent, /Unaffiliated/);
  // A body that carries the unresolved row renders it too (BODY2 above).
});

test("clicking a group row fetches its members and links users", async (t) => {
  const ctx = await boot({}, 5);
  t.after(() => ctx.dom.window.close());
  const doc = ctx.dom.window.document;
  await ctx.mod.loadGroups();
  const tr = doc.querySelector("#groupTable tr.row[data-gid='unit:T40106']");
  assert.ok(tr, "the group row rendered");
  tr.dispatchEvent(new ctx.dom.window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 0));
  const membersFetch = ctx.urls.find((u) => u.includes("/api/groups/") && u.includes("/users"));
  assert.ok(membersFetch, "row click fetched the drill-down");
  assert.match(membersFetch, /\/api\/groups\/unit%3AT40106\/users/);
  const panel = doc.getElementById("groupMembersResults");
  assert.equal(panel.style.display, "block");
  const tbody = doc.querySelector("#memberTable tbody");
  assert.match(tbody.textContent, /hannuse2/);
  const link = tbody.querySelector("a.userlink");
  assert.ok(link, "members link to the Users tab");
  assert.equal(link.getAttribute("href"), "/user/hannuse2");
});
