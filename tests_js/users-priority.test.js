// Functional tests for the Users tab's priority: the Fairshare column
// renders each user's factor (— when unknown, never 0) and warns when
// sshare could not be read; finalizing a user fetches their priority
// card (associations, pending jobs with rank); an unreadable source
// shows as unavailable; a superseded priority response is dropped; and
// clearing the selection hides the card. Node's own test runner + jsdom
// against the app's real index.html, timers stubbed as in
// status.test.js.
import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";
import { readFileSync } from "fs";

const html = readFileSync(new URL("../static/index.html", import.meta.url), "utf8");

function user(name, overrides) {
  return {
    user: name, jobs: 1, running_jobs: 1, mean_util: 50,
    util_gpu_hours: 1.0, vram_avg: null, gpu_types: ["h200"],
    fairshare: null, fairshare_account: null, ...overrides,
  };
}

const USERS = {
  window: { start: 1000, end: 2000 }, count: 2, fairshare_available: true,
  users: [
    user("alice", { fairshare: 0.125761, fairshare_account: "aalto_users" }),
    user("dave", { util_gpu_hours: 0.5 }),
  ],
};

function priority(name, overrides) {
  return {
    user: name, default_account: "aalto_users",
    fairshare_available: true, pending_available: true,
    associations: [
      { account: "cs", raw_shares: 1, norm_shares: 0.001171, raw_usage: 0,
        effective_usage: 0, fairshare: 1, primary: false },
      { account: "aalto_users", raw_shares: 30, norm_shares: 0.000477,
        raw_usage: 41268, effective_usage: 0.000001, fairshare: 0.125761,
        primary: true },
    ],
    pending: [
      { jobid: "101", partition: "gpu-h200", account: "aalto_users",
        qos: "normal", priority: 5000, age: 1000, fairshare: 2500,
        jobsize: 0, partition_factor: 10000, qos_factor: 0, nice: 0,
        rank: 2, queue_length: 3 },
      { jobid: "101", partition: "gpu-h100", account: "aalto_users",
        qos: "normal", priority: 3000, age: 1000, fairshare: 2500,
        jobsize: 0, partition_factor: 10000, qos_factor: 0, nice: 0,
        rank: 1, queue_length: 2 },
    ],
    ...overrides,
  };
}

// boot: the DOM at /users, the fetch stub routed by URL, a fresh users
// module. opts.users is the /api/users body; opts.priority(name) the
// /api/users/{name}/priority body; opts.gatePriority holds priority
// responses until release() resolves them, newest first.
async function boot(opts = {}, bust = "") {
  const dom = new JSDOM(html, { url: "http://localhost/users" });
  global.document = dom.window.document;
  global.window = dom.window;
  global.localStorage = dom.window.localStorage;
  global.location = dom.window.location;
  global.history = { pushState() {}, replaceState() {} };
  global.Event = dom.window.Event;
  global.setInterval = () => 0;
  global.clearInterval = () => {};
  global.Plotly = { newPlot: () => {}, react: () => {} };
  const urls = [];
  const gates = [];
  global.fetch = (url) => {
    url = String(url);
    urls.push(url);
    const ok = (body) => Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
    const m = url.match(/^\/api\/users\/([^/]+)\/priority$/);
    if (m) {
      const body = (opts.priority || priority)(decodeURIComponent(m[1]));
      if (opts.gatePriority) {
        return new Promise((resolve) => gates.push(() => resolve({
          ok: true, json: () => Promise.resolve(body) })));
      }
      return ok(body);
    }
    if (url.startsWith("/api/users")) return ok(opts.users || USERS);
    if (url.startsWith("/api/jobs")) return ok({ jobs: [] });
    return ok({});
  };
  const mod = await import("../static/js/tabs/users.js?cb=" + bust);
  return {
    dom, mod, urls, doc: dom.window.document,
    release: () => { while (gates.length) gates.pop()(); },
  };
}

const flush = async () => {
  for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
};

function cells(doc, tableId) {
  return [...doc.querySelectorAll("#" + tableId + " tbody tr")].map(
    (tr) => [...tr.querySelectorAll("td")].map((td) => td.textContent.trim()));
}

test("the Fairshare column shows the factor, — when unknown", async (t) => {
  const { dom, mod, doc } = await boot({}, "column");
  t.after(() => dom.window.close());
  await mod.loadUsers();
  const rows = cells(doc, "userTable");
  assert.equal(rows[0][0], "alice");
  assert.equal(rows[0][6], "0.126");
  assert.equal(rows[1][6], "—");
  const td = doc.querySelectorAll("#userTable tbody tr")[0].querySelectorAll("td")[6];
  assert.equal(td.title, "account aalto_users");
  assert.equal(doc.getElementById("uFairshareUnavailable").hidden, true);
});

test("an unreadable sshare warns above the user table", async (t) => {
  const { dom, mod, doc } = await boot(
    { users: { ...USERS, fairshare_available: false } }, "unavailable");
  t.after(() => dom.window.close());
  await mod.loadUsers();
  assert.equal(doc.getElementById("uFairshareUnavailable").hidden, false);
});

test("finalizing a user fetches and renders their priority card", async (t) => {
  const { dom, mod, doc, urls } = await boot({}, "card");
  t.after(() => dom.window.close());
  await mod.loadUsers();
  mod.finalizeUser("alice");
  await flush();
  assert.ok(urls.includes("/api/users/alice/priority"));
  assert.equal(doc.getElementById("userPriorityResults").style.display, "block");
  assert.equal(doc.getElementById("userPriorityTitle").textContent,
               "Priority · alice · default account aalto_users");
  assert.equal(doc.getElementById("userPriorityNote").hidden, true);
  // most usage first: the primary association leads
  assert.deepEqual(cells(doc, "userAssocTable"), [
    ["aalto_users", "0.05%", "<0.01%", "0.126", "yes"],
    ["cs", "0.12%", "0.00%", "1.000", ""],
  ]);
  const pending = cells(doc, "userPendingTable");
  assert.equal(pending.length, 2);
  assert.deepEqual(pending[0].slice(0, 5),
                   ["101", "gpu-h200", "normal", "5,000", "2 / 3"]);
  assert.equal(doc.getElementById("userPendingCount").textContent, "1 job");
});

test("an unreadable sprio reads as unavailable, not as no pending jobs", async (t) => {
  const { dom, mod, doc } = await boot({
    priority: (name) => priority(name, { pending_available: false, pending: null }),
  }, "sprio-down");
  t.after(() => dom.window.close());
  await mod.loadUsers();
  mod.finalizeUser("alice");
  await flush();
  const note = doc.getElementById("userPriorityNote");
  assert.equal(note.hidden, false);
  assert.match(note.textContent, /sprio could not be read/);
  assert.deepEqual(cells(doc, "userPendingTable"),
                   [["Pending-job priorities unavailable."]]);
});

test("a superseded priority response is dropped", async (t) => {
  const { dom, mod, doc, release } = await boot({ gatePriority: true }, "supersede");
  t.after(() => dom.window.close());
  await mod.loadUsers();
  mod.finalizeUser("alice");
  mod.finalizeUser("dave");
  release();  // dave's answer lands first, alice's stale one after it
  await flush();
  assert.match(doc.getElementById("userPriorityTitle").textContent, /^Priority · dave/);
});

test("clearing the selection hides the priority card", async (t) => {
  const { dom, mod, doc } = await boot({}, "clear");
  t.after(() => dom.window.close());
  await mod.loadUsers();
  mod.finalizeUser("alice");
  await flush();
  mod.finalizeUser("");
  assert.equal(doc.getElementById("userPriorityResults").style.display, "none");
});
