// Functional tests for the Groups tab: the school filter runs entirely
// client-side (no fetch), a superseded response is dropped by its token,
// the level toggle re-fetches server-side (group → department), the
// coverage banner renders the professor-group buckets and warns on
// lookup failures, pre-v2 level=unit deep links still land on the group
// level, and the Unaffiliated/Unresolved rows render even when empty.
// The member drill-down fetches on row click, shows each member's
// membership kind and extra groups, and links members (and the group
// leader's name) to the Users tab.
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
    group_id: gid, group_name: name, leader: null, leader_name: null,
    unit_codes: [], dept_code: null, dept_name: null,
    school_code: null, school_name: null, users: 1, jobs: 2,
    running_jobs: 1, mean_util: 50, util_gpu_hours: 1.0, gpu_hours: 2.0,
    vram_avg: null, low_eff_jobs: 0, top_users: [],
    ...overrides,
  };
}

const BODY1 = {
  window: { start: 1000, end: 2000 }, level: "group", count: 3,
  schools: [
    { code: "T3", short: "SCI", full: "School of Science" },
    { code: "T4", short: "ELEC", full: "School of Electrical Engineering" },
  ],
  coverage: { users: 4, in_prof_group: 2, dept_only: 1, unaffiliated: 1,
              unresolved: 0, failed: 0 },
  groups: [
    group("dept:T313", "Computer Science, no professor group",
          { school_code: "SCI", mean_util: 92.5 }),
    group("unaffiliated", "Unaffiliated", { mean_util: 85 }),
    group("kyrkiv1", "Kyrki Ville",
          { leader: "kyrkiv1", leader_name: "Kyrki Ville",
            unit_codes: ["T40106"], school_code: "ELEC", mean_util: 50 }),
  ],
};

const BODY2 = {
  ...BODY1,
  level: "department", count: 2,
  groups: [
    group("dept:T410", "Department of Electrical Engineering and Automation",
          { school_code: "ELEC" }),
    group("unresolved", "Unresolved",
          { users: 0, jobs: 0, running_jobs: 0, mean_util: 0 }),
  ],
};

const MEMBERS_BODY = {
  group_id: "kyrkiv1", group_name: "Kyrki Ville", level: "group",
  window: { start: 1000, end: 2000 }, count: 1,
  users: [{
    user: "hannuse2", jobs: 2, running_jobs: 1, mean_util: 50,
    util_gpu_hours: 1.0, vram_avg: 11, gpu_types: ["h200"],
    group: "kyrkiv1", membership: "everyone",
    dept_code: "T410", school_code: "ELEC", own_dept: "T411",
    extra_groups: ["backstt1"], status: "group",
  }],
};

// A benign body for the routes a leader-link click triggers on the way
// to the Users tab (loadUsers, the selected user's job list) — enough
// shape that those renders succeed and no promise chain rejects.
const BENIGN = {
  window: { start: 1000, end: 2000 }, count: 0, users: [], jobs: [],
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
    if (u.startsWith("/api/groups/")) {
      return Promise.resolve({
        ok: true, json: () => Promise.resolve({ ...MEMBERS_BODY }),
      });
    }
    if (!u.startsWith("/api/groups?")) {
      return Promise.resolve({
        ok: true, json: () => Promise.resolve({ ...BENIGN }),
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
  assert.match(tbody.textContent, /Computer Science, no professor group/);
  assert.doesNotMatch(tbody.textContent, /Kyrki Ville/);
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
  assert.match(ctx.urls[0], /level=group/);
  doc.getElementById("gLevel").value = "department";
  doc.getElementById("gLevel").dispatchEvent(
    new ctx.dom.window.Event("change"));
  await new Promise((r) => setTimeout(r, 0));
  assert.match(ctx.urls[ctx.urls.length - 1], /level=department/);
  assert.equal(ctx.urls.length, 2, "level toggle fetches");
});

test("a pre-v2 level=unit deep link lands on the professor-group level", async (t) => {
  const ctx = await boot({}, 7);
  t.after(() => ctx.dom.window.close());
  const doc = ctx.dom.window.document;
  ctx.mod.prefillFromUrl(new URLSearchParams("level=unit"));
  assert.equal(doc.getElementById("gLevel").value, "group");
  await ctx.mod.loadGroups();
  assert.match(ctx.urls[0], /level=group/);
});

test("the coverage banner shows the professor-group buckets and warns only on failures", async (t) => {
  const ctx = await boot({}, 4);
  t.after(() => ctx.dom.window.close());
  const doc = ctx.dom.window.document;
  await ctx.mod.loadGroups();
  const banner = doc.getElementById("groupsCoverage");
  assert.equal(banner.hidden, false);
  assert.match(banner.textContent, /2 of 4 job owners in a professor group/);
  assert.match(banner.textContent, /1 department-only/);
  assert.match(banner.textContent, /1 unaffiliated/);
  // department-only users are the honest bucket, not a warning
  assert.equal(banner.classList.contains("warn"), false);
  // lookup failures tint the banner: their activity is in no row
  const failed = {
    ...BODY1,
    coverage: { ...BODY1.coverage, failed: 2 },
  };
  const ctx2 = await boot({ bodies: [failed] }, 10);
  t.after(() => ctx2.dom.window.close());
  await ctx2.mod.loadGroups();
  const banner2 = ctx2.dom.window.document.getElementById("groupsCoverage");
  assert.match(banner2.textContent, /2 lookup failures/);
  assert.match(banner2.textContent, /missing from every figure/);
  assert.equal(banner2.classList.contains("warn"), true);
});

test("both special rows render when the server sends them", async (t) => {
  const ctx = await boot({}, 8);
  t.after(() => ctx.dom.window.close());
  const doc = ctx.dom.window.document;
  await ctx.mod.loadGroups();
  const tbody = doc.querySelector("#groupTable tbody");
  assert.match(tbody.textContent, /Unaffiliated/);
  // BODY1 has no unresolved row: the SERVER always sends one, but this
  // body omits it — the client renders what the API sends, so the
  // always-present contract belongs to the API (covered in the golden
  // and endpoint tests). BODY2 carries it.
  const ctx2 = await boot({ bodies: [BODY2] }, 9);
  t.after(() => ctx2.dom.window.close());
  await ctx2.mod.loadGroups();
  assert.match(ctx2.dom.window.document.querySelector("#groupTable tbody")
    .textContent, /Unresolved/);
});

test("clicking a group row fetches its members with kind and extra groups", async (t) => {
  const ctx = await boot({}, 5);
  t.after(() => ctx.dom.window.close());
  const doc = ctx.dom.window.document;
  await ctx.mod.loadGroups();
  const tr = doc.querySelector("#groupTable tr.row[data-gid='kyrkiv1']");
  assert.ok(tr, "the group row rendered");
  tr.dispatchEvent(new ctx.dom.window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 0));
  const membersFetch = ctx.urls.find((u) => u.includes("/api/groups/") && u.includes("/users"));
  assert.ok(membersFetch, "row click fetched the drill-down");
  assert.match(membersFetch, /\/api\/groups\/kyrkiv1\/users/);
  const panel = doc.getElementById("groupMembersResults");
  assert.equal(panel.style.display, "block");
  const tbody = doc.querySelector("#memberTable tbody");
  assert.match(tbody.textContent, /hannuse2/);
  assert.match(tbody.textContent, /everyone/, "membership kind renders");
  assert.match(tbody.textContent, /backstt1/, "extra groups render as chips");
  const link = tbody.querySelector("a.userlink");
  assert.ok(link, "members link to the Users tab");
  assert.equal(link.getAttribute("href"), "/user/hannuse2");
});

test("the leader cell links to the Users tab and clicking it does not drill down", async (t) => {
  const ctx = await boot({}, 6);
  t.after(() => ctx.dom.window.close());
  const doc = ctx.dom.window.document;
  await ctx.mod.loadGroups();
  const tr = doc.querySelector("#groupTable tr.row[data-gid='kyrkiv1']");
  const leaderLink = tr.querySelector("td a.userlink");
  assert.ok(leaderLink, "the leader cell is a link");
  assert.equal(leaderLink.getAttribute("href"), "/user/kyrkiv1");
  assert.equal(leaderLink.textContent, "Kyrki Ville",
    "the leader's display name labels the link");
  const fetchesBefore = ctx.urls.length;
  leaderLink.dispatchEvent(
    new ctx.dom.window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 0));
  // openUser routes to the Users tab (which may fetch there), but the
  // drill-down must NOT fire
  assert.equal(
    ctx.urls.slice(fetchesBefore).some((u) => u.includes("/api/groups/")),
    false, "clicking the leader's name must not fetch the drill-down");
  // a click elsewhere in the row still drills down
  tr.dispatchEvent(new ctx.dom.window.MouseEvent("click", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 0));
  assert.ok(ctx.urls.find((u) => u.includes("/api/groups/kyrkiv1/users")),
    "row click fetched the drill-down");
});
