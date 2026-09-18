// Functional tests for the Users tab's contact history and utilization
// chart: the Contacts column joins /api/users/contacts per user
// (Unavailable / Never / count · last date), the selected user's contact
// table renders the full escaped history newest-first, both chart views
// derive from one activity response without refetching, contact markers
// only cover the response window and collapse same-day contacts, and a
// refresh/window change reloads an open selection with stale responses
// suppressed. Node's own test runner + jsdom against the app's real
// index.html; fetch and Plotly are stubbed (see queue.test.js for the
// original boot pattern).
import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";
import { readFileSync } from "fs";

const pageHtml = readFileSync(
  new URL("../static/index.html", import.meta.url), "utf8");

const USERS_BODY = {
  window: { start: 1000, end: 2000 },
  count: 2,
  users: [
    { user: "alice", jobs: 2, running_jobs: 1, mean_util: 55.5,
      util_gpu_hours: 10.0, vram_avg: 30.0, gpu_types: ["h200"] },
    { user: "bob", jobs: 1, running_jobs: 0, mean_util: 10.0,
      util_gpu_hours: 1.0, vram_avg: null, gpu_types: ["h100"] },
  ],
};

const CONTACTS_BODY = {
  available: true,
  warning: null,
  skipped_rows: 0,
  contacts: [
    { date: "2024-03-02", username: "alice", message: "second <b>note</b>" },
    { date: "2024-03-01", username: "alice", message: "same day again" },
    { date: "2024-03-01", username: "alice", message: "first note" },
    { date: "2024-02-20", username: "bob", message: "bob talked" },
  ],
};

const ACTIVITY_BODY = {
  user: "alice",
  window: { start: 1709251200, end: 1709856000 }, // 2024-03-01 .. 2024-03-08 UTC
  step: 120,
  aggregate: [[1709251200, 40], [1709337600, 60]],
  jobs: [
    { jobid: "1", values: [[1709251200, 40], [1709337600, 60]] },
    { jobid: "2", values: [[1709251200, 45], [1709337600, 55]] },
  ],
};


// boot: DOM, fetch stub routed by URL, fresh users module per test.
//   users         /api/users body
//   contacts      /api/users/contacts body (or contactsError to reject)
//   activity      /api/users/<u>/activity body
//   activityGate  unresolved activity placeholder; releaseActivity(i)
//                 resolves the i-th gated activity request (FIFO)
//   contactsError / usersError / activityError — reject the given request
async function boot(opts, bust) {
  const dom = new JSDOM(pageHtml, { url: "http://localhost/users" });
  global.document = dom.window.document;
  global.window = dom.window;
  global.localStorage = dom.window.localStorage;
  global.location = dom.window.location;
  global.history = { pushState() {}, replaceState() {} };
  global.setInterval = () => 0;
  global.clearInterval = () => {};
  const plotCalls = [];
  global.Plotly = {
    newPlot: () => {},
    react: (el, traces, layout) => { plotCalls.push({ el, traces, layout }); },
  };

  const urls = [];
  const releaseActivity = [];
  let releaseJobs = () => {};

  global.fetch = (url) => {
    urls.push(String(url));
    const u = String(url);
    if (u.startsWith("/api/users/contacts")) {
      if (opts.contactsError) return Promise.reject(new Error("down"));
      return Promise.resolve({ ok: true,
        json: () => Promise.resolve(opts.contacts || CONTACTS_BODY) });
    }
    if (/^\/api\/users\/([^/]+)\/activity/.test(u)) {
      if (opts.activityError) return Promise.reject(new Error("prom down"));
      // Distinguish the two gated requests by the user in the URL, so a
      // released stale payload is recognizable on arrival.
      const activityFor = (u.match(/\/api\/users\/([^/]+)\/activity/) || [])[1];
      const userName = decodeURIComponent(activityFor);
      if (opts.activityErrorFor === userName) {
        return Promise.reject(new Error("prom down"));
      }
      if (opts.activityBodies && opts.activityBodies[userName] === null) {
        return Promise.reject(new Error("no activity"));
      }
      const body = opts.activityBodies && opts.activityBodies[userName]
        ? opts.activityBodies[userName]
        : (opts.activity || ACTIVITY_BODY);
      if (opts.activityGate) {
        // One resolver per request: selecting a second user while the
        // first is still gated must NOT overwrite the first resolver,
        // or the test would release the newest request instead of the
        // stale one it means to release.
        return new Promise((resolve) => {
          releaseActivity.push(() => resolve({ ok: true,
            json: () => Promise.resolve(body) }));
        });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
    }
    if (u.startsWith("/api/jobs?")) {
      if (opts.jobsGate) {
        return new Promise((resolve) => {
          releaseJobs = () => resolve({ ok: true,
            json: () => Promise.resolve({ window: { start: 1000, end: 2000 },
              count: 1, total_candidates: 1,
              jobs: [{ jobid: "stale", name: "stale", state: "RUNNING",
                       gpu_group: "h200", nodes: [], gpus: 1, start: "",
                       mean_util: 1, gpu_hours_eff: 1 }] }) });
        });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({
        window: { start: 1000, end: 2000 }, count: 0, jobs: [],
        total_candidates: 0 }) });
    }
    if (u.startsWith("/api/users")) {
      if (opts.usersError) return Promise.reject(new Error("list down"));
      return Promise.resolve({ ok: true,
        json: () => Promise.resolve(opts.users || USERS_BODY) });
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
  };

  const mod = await import("../static/js/tabs/users.js?cb=" + bust);
  return { dom, mod, urls, plotCalls,
           releaseActivity: (i = 0) => releaseActivity[i] && releaseActivity[i](),
           releaseJobs: (...a) => releaseJobs(...a) };
}

async function bootLoaded(opts, bust) {
  const ctx = await boot(opts, bust);
  await ctx.mod.loadUsers();
  return ctx;
}

test("contacts join renders Unavailable, Never, and count · last date", async () => {
  const { dom, mod } = await bootLoaded({}, 11);
  const doc = dom.window.document;
  await mod.finalizeUser(""); // ensure no selection side effects in table
  const rows = doc.querySelectorAll("#userTable tbody tr");
  const text = doc.querySelector("#userTable tbody").textContent;
  assert.equal(rows.length, 2);
});

test("unavailable history shows Unavailable and null-sorts last", async () => {
  const { dom } = await bootLoaded({
    contacts: { available: false, warning: "Garage Diary local path is unavailable.",
                skipped_rows: 0, contacts: [] },
  }, 12);
  const doc = dom.window.document;
  const text = doc.querySelector("#userTable tbody").textContent;
  assert.match(text, /Unavailable/);
  assert.equal((text.match(/Never/g) || []).length, 0);
});

test("available history with no records shows Never", async () => {
  const { dom } = await bootLoaded({
    contacts: { available: true, warning: null, skipped_rows: 0,
                contacts: [] },
  }, 13);
  const text = dom.window.document.querySelector("#userTable tbody").textContent;
  assert.equal((text.match(/Never/g) || []).length, 2);
});

test("selected user's contact table renders full escaped history newest first", async () => {
  const { dom, mod } = await bootLoaded({}, 14);
  const doc = dom.window.document;
  mod.finalizeUser("alice");
  await new Promise((r) => setTimeout(r, 0));
  const tbody = doc.querySelector("#userContactsTable tbody");
  const dates = [...tbody.querySelectorAll("tr td:first-child")]
    .map((td) => td.textContent);
  assert.deepEqual(dates, ["2024-03-02", "2024-03-01", "2024-03-01"]);
  // Raw HTML must be escaped, not injected.
  const cell = [...tbody.querySelectorAll("tr td.msg-cell")]
    .map((td) => td.textContent);
  assert.deepEqual(cell, ["second <b>note</b>", "same day again", "first note"]);
  assert.equal(tbody.querySelectorAll("b").length, 0,
    "message HTML must render as text, never as elements");
});

test("unavailable source renders warning and no No-recorded-contacts line", async () => {
  const { dom, mod } = await bootLoaded({
    contacts: { available: false, warning: "Garage Diary Git refresh failed.",
                skipped_rows: 0, contacts: [] },
  }, 15);
  const doc = dom.window.document;
  mod.finalizeUser("alice");
  await new Promise((r) => setTimeout(r, 0));
  const status = doc.getElementById("userContactsStatus");
  assert.match(status.textContent, /Contact history unavailable/);
  assert.match(status.textContent, /Garage Diary Git refresh failed\./);
  const tbody = doc.querySelector("#userContactsTable tbody");
  assert.match(tbody.textContent, /Contact history unavailable\./);
});

test("skipped rows surface in the status line", async () => {
  const { dom, mod } = await bootLoaded({
    contacts: { available: true, warning: null, skipped_rows: 7,
                contacts: CONTACTS_BODY.contacts },
  }, 16);
  const doc = dom.window.document;
  mod.finalizeUser("alice");
  await new Promise((r) => setTimeout(r, 0));
  const status = doc.getElementById("userContactsStatus");
  assert.match(status.textContent, /Skipped 7 malformed or unjoinable/);
});

test("overall and per-job views render from one response without refetch", async () => {
  const { dom, mod, urls, plotCalls } = await bootLoaded({}, 17);
  const doc = dom.window.document;
  mod.finalizeUser("alice");
  await new Promise((r) => setTimeout(r, 0));
  const activityFetches = urls.filter((u) => u.includes("/activity")).length;
  assert.equal(activityFetches, 1);
  assert.equal(plotCalls.length, 1);
  const overall = plotCalls[0];
  // One aggregate line plus the in-window contact marker trace.
  assert.equal(
    overall.traces.filter((t) => t.name !== "contact").length, 1,
    "overall view: one aggregate line");
  assert.equal(overall.traces[0].name, "overall utilization");

  // Switch view: re-render from the same data, no second fetch.
  doc.getElementById("uUtilView").value = "jobs";
  doc.getElementById("uUtilView").dispatchEvent(new dom.window.Event("change"));
  assert.equal(urls.filter((u) => u.includes("/activity")).length, 1,
    "view switch must not refetch");
  const perJob = plotCalls[plotCalls.length - 1];
  const jobTraces = perJob.traces.filter((t) => t.name !== "contact");
  assert.deepEqual(jobTraces.map((t) => t.name), ["job 1", "job 2"]);
});

test("in-window contacts chart as grouped markers; out-of-window excluded", async () => {
  const { dom, mod, plotCalls } = await bootLoaded({}, 18);
  const doc = dom.window.document;
  mod.finalizeUser("alice");
  await new Promise((r) => setTimeout(r, 0));
  const call = plotCalls[plotCalls.length - 1];
  // 2024-03-02 + two same-day 2024-03-01 contacts are in-window
  // (window 2024-03-01..2024-03-08); bob's 2024-02-20 is not (and not
  // alice's anyway). Two shapes (one per distinct date) + one marker trace.
  const shapes = call.layout.shapes;
  assert.equal(shapes.length, 2);
  const markerTrace = call.traces.find((t) => t.name === "contact");
  assert.ok(markerTrace, "distinct marker trace present");
  assert.equal(markerTrace.x.length, 2, "same-day contacts collapse");
  // No inferred contact time: the marker/shape x values are the ISO
  // calendar date strings themselves, not epoch instants.
  assert.deepEqual([...markerTrace.x].sort(), ["2024-03-01", "2024-03-02"]);
  assert.deepEqual(
    shapes.map((s) => s.x0).sort(), ["2024-03-01", "2024-03-02"]);
  assert.deepEqual(shapes.map((s) => s.x1), shapes.map((s) => s.x0));
  // Hover text lists both same-day messages, escaped.
  const joined = markerTrace.text.join("\n");
  assert.match(joined, /2024-03-01 — same day again \| first note/);
  assert.match(joined, /2024-03-02 — second &lt;b&gt;note&lt;\/b&gt;/);
  assert.equal(call.layout.yaxis.range[1], 105);
});

test("a contact on a partial first window day still charts (Helsinki bounds)", async () => {
  // Window starts 2024-03-01T22:00Z = 2024-03-02 00:00 Helsinki but ends
  // 2024-03-02T09:30Z = 12:30 Helsinki. Noon-UTC comparison would drop
  // 2024-03-02's marker only if the window end preceded it; here the
  // end is fine but the START is past noon UTC — under the old epoch
  // check the 2024-03-02 contact (noon UTC) predates winStart and was
  // wrongly excluded. Helsinki-date bounds must keep it. An 2024-03-01
  // contact is genuinely outside and must stay excluded.
  const winStart = Date.parse("2024-03-01T22:00:00Z") / 1000;
  const winEnd = Date.parse("2024-03-02T09:30:00Z") / 1000;
  const { dom, mod, plotCalls } = await bootLoaded({
    contacts: {
      available: true, warning: null, skipped_rows: 0,
      contacts: [
        { date: "2024-03-02", username: "alice", message: "boundary day" },
        { date: "2024-03-01", username: "alice", message: "day before" },
      ],
    },
    activity: {
      user: "alice", window: { start: winStart, end: winEnd }, step: 120,
      aggregate: [[winStart + 600, 50]],
      jobs: [{ jobid: "1", values: [[winStart + 600, 50]] }],
    },
  }, 25);
  const doc = dom.window.document;
  mod.finalizeUser("alice");
  await new Promise((r) => setTimeout(r, 0));
  const call = plotCalls[plotCalls.length - 1];
  const markerTrace = call.traces.find((t) => t.name === "contact");
  assert.ok(markerTrace, "boundary contact must chart");
  assert.equal(markerTrace.x.length, 1);
  assert.deepEqual(markerTrace.x, ["2024-03-02"]);
  assert.match(markerTrace.text[0], /2024-03-02 — boundary day/);
  assert.equal(call.layout.shapes.length, 1);
  assert.deepEqual(call.layout.shapes.map((s) => s.x0), ["2024-03-02"]);
});

test("empty activity renders the empty state, not a chart", async () => {
  const { dom, mod, plotCalls } = await bootLoaded({
    activity: { user: "alice", window: { start: 1, end: 2 }, step: 120,
                aggregate: [], jobs: [] },
  }, 19);
  const doc = dom.window.document;
  mod.finalizeUser("alice");
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(plotCalls.length, 0, "no chart for an empty series");
  assert.equal(doc.getElementById("userUtilEmpty").style.display, "");
  assert.equal(doc.getElementById("userUtilPlot").style.display, "none");
});

test("refresh reloads list, contacts, and an open selection's activity", async () => {
  const { dom, mod, urls } = await bootLoaded({}, 20);
  const doc = dom.window.document;
  mod.finalizeUser("alice");
  await new Promise((r) => setTimeout(r, 0));
  urls.length = 0;
  doc.getElementById("uRefresh").click();
  await new Promise((r) => setTimeout(r, 0));
  assert.ok(urls.some((u) => u.startsWith("/api/users?since_hours=24")));
  assert.ok(urls.some((u) => u.startsWith("/api/users/contacts")));
  assert.ok(urls.some((u) => u.includes("/activity")));
  assert.ok(urls.some((u) => u.startsWith("/api/jobs?")));
});

test("window change keeps the selection and reloads everything", async () => {
  const { dom, mod, urls } = await bootLoaded({}, 21);
  const doc = dom.window.document;
  mod.finalizeUser("alice");
  await new Promise((r) => setTimeout(r, 0));
  urls.length = 0;
  doc.getElementById("uWindow").value = "168";
  doc.getElementById("uWindow").dispatchEvent(new dom.window.Event("change"));
  await new Promise((r) => setTimeout(r, 0));
  assert.ok(urls.some((u) => u === "/api/users?since_hours=168"));
  assert.ok(urls.some((u) => u.includes("since_hours=168") && u.includes("/activity")));
  // Selection survived: banner still names alice.
  assert.equal(mod.userActivityData.user, "alice");
});

test("stale activity responses never overwrite the newest selection", async () => {
  const { dom, mod, releaseActivity } = await boot({
    activityGate: true,
    activityBodies: {
      alice: { user: "alice", window: { start: 1, end: 2 }, step: 120,
               aggregate: [[1, 11]], jobs: [] },
      bob: { user: "bob", window: { start: 1, end: 2 }, step: 120,
             aggregate: [[1, 99]], jobs: [] },
    },
  }, 22);
  const doc = dom.window.document;
  // Start loading alice's activity (gated), then select bob: the older
  // request must be discarded when it finally resolves.
  const p1 = mod.finalizeUser("alice");
  await new Promise((r) => setTimeout(r, 0));
  mod.finalizeUser("bob");
  await new Promise((r) => setTimeout(r, 0));
  // Per-request resolvers: index 0 = alice (stale), index 1 = bob.
  releaseActivity(1);
  await new Promise((r) => setTimeout(r, 0));
  // The bob response carries value 99 — a stale alice acceptance would
  // either keep 11 or overwrite it; 99 proves bob's payload landed.
  assert.equal(mod.userActivityData.user, "bob");
  assert.equal(mod.userActivityData.aggregate[0][1], 99);
  releaseActivity(0); // alice's stale response arrives last
  await Promise.all([p1, new Promise((r) => setTimeout(r, 0))]);
  // The stale alice payload (user "alice", value 11) must not replace
  // bob's already-rendered selection.
  assert.equal(mod.userActivityData.user, "bob");
  assert.equal(mod.userActivityData.aggregate[0][1], 99);
  const title = doc.getElementById("userActivityTitle").textContent;
  assert.match(title, /bob/);
});

test("contacts fetch failure degrades to Unavailable without failing the list", async () => {
  const { dom } = await bootLoaded({ contactsError: true }, 23);
  const doc = dom.window.document;
  const text = doc.querySelector("#userTable tbody").textContent;
  assert.match(text, /Unavailable/);
  // The list itself still rendered.
  assert.match(text, /alice/);
});

test("switching users clears the previous user's chart while loading", async () => {
  const { dom, mod } = await bootLoaded({
    activityBodies: {
      alice: { user: "alice", window: { start: 1, end: 2 }, step: 120,
               aggregate: [[1, 42]], jobs: [] },
      bob: null, // bob's activity request fails
    },
    activityErrorFor: "bob",
  }, 27);
  const doc = dom.window.document;
  mod.finalizeUser("alice"); // chart renders for alice
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(mod.userActivityData.user, "alice");
  assert.equal(doc.getElementById("userUtilPlot").style.display, "");
  // Select bob: the old chart must vanish immediately (loading state),
  // and bob's failing response must leave only the error — never
  // alice's chart under bob's title.
  mod.finalizeUser("bob");
  assert.equal(mod.userActivityData, null);
  assert.equal(doc.getElementById("userUtilPlot").style.display, "none");
  await new Promise((r) => setTimeout(r, 0));
  assert.match(doc.getElementById("userActivityTitle").textContent, /bob/);
  const plot = doc.getElementById("userUtilPlot");
  assert.ok(!plot.data || plot.data.length === 0,
    "no prior user's traces may remain");
});

test("markers rebuild when refreshed contacts arrive after the chart", async () => {
  // Chart renders from stale contacts (no markers), then a refreshed
  // contacts payload with a new in-window contact arrives: the graph
  // must pick up the marker without another activity fetch.
  const { dom, mod, plotCalls, urls } = await boot({
    contacts: { available: true, warning: null, skipped_rows: 0,
                contacts: [] },
  }, 28);
  const doc = dom.window.document;
  await mod.loadUsers();
  mod.finalizeUser("alice"); // activity renders with zero markers
  await new Promise((r) => setTimeout(r, 0));
  const before = plotCalls.length;
  assert.equal(plotCalls[plotCalls.length - 1].layout.shapes.length, 0);
  // Simulate a refresh whose contacts now include an in-window contact.
  global.fetch = (u) => {
    const url = String(u);
    if (url.startsWith("/api/users/contacts")) {
      return Promise.resolve({ ok: true, json: async () => ({
        available: true, warning: null, skipped_rows: 0,
        contacts: [
          { date: "2024-03-03", username: "alice",
            message: "fresh marker" },
        ] }) });
    }
    if (url.startsWith("/api/users?")) {
      return Promise.resolve({ ok: true,
        json: async () => USERS_BODY });
    }
    if (/\/activity/.test(url)) {
      return Promise.resolve({ ok: true,
        json: async () => ACTIVITY_BODY });
    }
    return Promise.resolve({ ok: true, json: async () => ({}) });
  };
  await mod.loadUsers();
  await new Promise((r) => setTimeout(r, 0));
  const call = plotCalls[plotCalls.length - 1];
  assert.ok(plotCalls.length > before, "chart rerendered on fresh contacts");
  assert.equal(call.layout.shapes.length, 1, "new marker appears");
  const marker = call.traces.find((t) => t.name === "contact");
  assert.match(marker.text[0], /fresh marker/);
  assert.equal(urls.filter((u) => u.includes("/activity")).length, 1,
    "no activity refetch for marker rebuild");
});

test("clearing the selection discards an in-flight jobs response", async () => {
  const { dom, mod, releaseJobs } = await boot({ jobsGate: true }, 26);
  const doc = dom.window.document;
  // Start alice's jobs fetch (gated), then clear the selection: the
  // stale jobs response that resolves afterwards must not populate the
  // hidden jobs table.
  mod.finalizeUser("alice");
  await new Promise((r) => setTimeout(r, 0));
  const clearDone = mod.finalizeUser(""); // invalidates both tokens
  await new Promise((r) => setTimeout(r, 0));
  releaseJobs();
  await Promise.all([clearDone, new Promise((r) => setTimeout(r, 0))]);
  assert.equal(doc.getElementById("userJobsResults").style.display, "none");
  const rows = doc.querySelectorAll("#userJobsTable tbody tr");
  assert.equal(rows.length, 0, "stale jobs response must not render");
  // The banner is toggled off (us-name keeps the last name by design;
  // the .on class is what makes it visible).
  assert.equal(doc.getElementById("userSelectedBanner").classList.contains("on"),
    false, "selection banner must be hidden after clearing");
});

test("theme rerender registration covers an open user chart", async () => {
  const { dom, plotCalls } = await boot({}, 24);
  const doc = dom.window.document;
  // Import the tab through the bare URL router.js resolves, so both see
  // ONE module instance (the ?cb= imports above create fresh instances).
  const mod = await import("../static/js/tabs/users.js");
  await mod.loadUsers();
  mod.finalizeUser("alice");
  await new Promise((r) => setTimeout(r, 0));
  const before = plotCalls.length;
  const router = await import("../static/js/core/router.js");
  router.rerenderAllPlots();
  assert.ok(plotCalls.length > before, "user chart re-rendered on theme change");
});
