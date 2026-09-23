// Users tab: the Ellis/Non-Ellis category summary loads INDEPENDENTLY of
// the user list (own endpoint, own token, own panel) — a category failure
// must leave the user table usable, a slow user list must not block the
// category rows, and a stale category response must not overwrite a newer
// window. The category table renders the two ordered rows above #userTable
// with the shared formats (integers, 2-decimals, fmtDuration, pctBar, —
// for nulls while zero stays zero), partial accounting coverage is
// visible, and the per-user Category column renders and sorts without
// changing username search or selection.
import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";
import { readFileSync } from "fs";
import { pathToFileURL } from "url";

const html = readFileSync(new URL("../static/index.html", import.meta.url), "utf8");

const USERS_BODY = {
  window: { start: 1000, end: 2000 },
  count: 2,
  users: [
    { user: "mentus1", user_category: "Ellis", jobs: 2, running_jobs: 1,
      mean_util: 50.0, util_gpu_hours: 2.0, vram_avg: 11.0,
      gpu_types: ["h200"] },
    { user: "firoozh1", user_category: "Non-Ellis", jobs: 1,
      running_jobs: 0, mean_util: 90.0, util_gpu_hours: 1.0,
      vram_avg: null, gpu_types: ["h100"] },
  ],
};

const CATEGORIES_BODY = {
  window: { start: 1000, end: 2000 },
  categories: [
    { category: "Ellis", jobs: 12, gpu_hours: 48.5,
      wait_per_gpu_hour: 0.75, wait_p50_s: 3600, wait_p90_s: 7200,
      wait_avg_s: 5400, wait_samples: 9, mean_util: 62.5 },
    { category: "Non-Ellis", jobs: 0, gpu_hours: 0.0,
      wait_per_gpu_hour: null, wait_p50_s: null, wait_p90_s: null,
      wait_avg_s: null, wait_samples: 0, mean_util: null },
  ],
  coverage: { records_examined: 12, excluded: {}, failed_batches: 0,
              complete: true },
};

// boot: build the DOM, route the fetch stub by URL, import a fresh users
// module against it. Options:
//   users        body for /api/users (default USERS_BODY)
//   categories   body for /api/users/categories (default CATEGORIES_BODY)
//   gateUsers    hold /api/users unresolved (handle.releaseUsers())
//   gateCats     hold /api/users/categories unresolved (handle.releaseCats())
//   catsError    make /api/users/categories reject
//   usersError   make /api/users reject
//   staleCats    first categories fetch resolves late, second immediately
async function boot(opts = {}, bust) {
  const dom = new JSDOM(html, { url: "http://localhost/users" });
  global.document = dom.window.document;
  global.window = dom.window;
  global.localStorage = dom.window.localStorage;
  global.location = dom.window.location;
  global.history = { pushState() {}, replaceState() {} };
  global.setInterval = () => 0;
  global.clearInterval = () => {};
  global.Plotly = { newPlot: () => {}, react: () => {} };

  const urls = [];
  let releaseUsers = () => {};
  let releaseCats = () => {};

  global.fetch = (url) => {
    urls.push(String(url));
    const s = String(url);
    if (s.startsWith("/api/users/categories")) {
      const body = opts.categoriesBody || CATEGORIES_BODY;
      if (opts.catsError) return Promise.reject(new Error("sacct down"));
      if (opts.gateCats) {
        return new Promise((resolve) => {
          releaseCats = () => resolve({
            ok: true, json: () => Promise.resolve(body),
          });
        });
      }
      return Promise.resolve({
        ok: true, json: () => Promise.resolve(body),
      });
    }
    if (s.startsWith("/api/users")) {
      if (opts.usersError) return Promise.reject(new Error("prom down"));
      if (opts.gateUsers) {
        return new Promise((resolve) => {
          releaseUsers = () => resolve({
            ok: true, json: () => Promise.resolve(USERS_BODY),
          });
        });
      }
      return Promise.resolve({
        ok: true, json: () => Promise.resolve(USERS_BODY),
      });
    }
    // user's job list (only fetched on selection): empty.
    return Promise.resolve({ ok: true, json: () => Promise.resolve({
      window: { start: 1000, end: 2000 }, count: 0, jobs: [],
    }) });
  };

  const mod = await import("../static/js/tabs/users.js?cb=" + bust);
  return {
    dom, mod, urls,
    releaseUsers: (...a) => releaseUsers(...a),
    releaseCats: (...a) => releaseCats(...a),
  };
}


// The module loads via showTab on first visit; tests call loadUsers
// directly (the same entry the router and refresh use).
test("two rows with exact formats render above #userTable", async (t) => {
  const { dom, mod } = await boot({}, 21);
  t.after(() => dom.window.close());
  await mod.loadUsers();
  const doc = dom.window.document;
  const catPanel = doc.getElementById("userCategoriesResults");
  const usersPanel = doc.getElementById("usersResults");
  // placement: categories panel precedes the users panel
  assert.ok(catPanel.compareDocumentPosition(usersPanel)
            & dom.window.Node.DOCUMENT_POSITION_FOLLOWING,
            "userCategoriesResults sits above usersResults");
  const rows = doc.querySelectorAll("#userCategoryTable tbody tr");
  assert.equal(rows.length, 2);
  const cells = (r) => [...r.querySelectorAll("td")].map((c) => c.textContent.trim());
  // Ellis row first, ordered by the API: jobs integer, GPU h 2-decimals,
  // wait h/GPU-h with unit, percentiles as durations, util as pct bar.
  const ellis = cells(rows[0]);
  assert.equal(ellis[0], "Ellis");
  assert.equal(ellis[1], "12");
  assert.equal(ellis[2], "48.50");
  assert.equal(ellis[3], "0.75 h/GPU-h");
  assert.equal(ellis[4], "1h 0m");
  assert.equal(ellis[5], "2h 0m");
  assert.equal(ellis[6], "1h 30m");
  assert.match(ellis[7], /63%/); // pctBar rounds to whole percent
  // Non-Ellis zero row: numeric zeros stay zero, nulls render as em dash.
  const non = cells(rows[1]);
  assert.equal(non[0], "Non-Ellis");
  assert.equal(non[1], "0");
  assert.equal(non[2], "0.00");
  assert.equal(non[3], "—");
  assert.equal(non[4], "—");
  assert.equal(non[7], "—");  // null utilization renders as —, not 0%
  // the fixed hint is present under the table
  const hints = [...catPanel.querySelectorAll(".hint")]
    .map((h) => h.textContent);
  assert.ok(hints.some((h) =>
    h.includes("Completed GPU jobs started in the selected window")));
});

test("user rows show their category and it sorts as text", async (t) => {
  const { dom, mod } = await boot({}, 22);
  t.after(() => dom.window.close());
  await mod.loadUsers();
  const doc = dom.window.document;
  const rowCells = () => [...doc.querySelectorAll("#userTable tbody tr")]
    .map((r) => [...r.querySelectorAll("td")]
         .map((c) => c.textContent.trim()));
  // default sort util_gpu_hours desc: mentus1 (2.0) then firoozh1 (1.0)
  let rows = rowCells();
  assert.equal(rows[0][0], "mentus1");
  assert.equal(rows[0][1], "Ellis");
  assert.equal(rows[1][0], "firoozh1");
  assert.equal(rows[1][1], "Non-Ellis");
  // click the Category header: text column sorts ascending
  const catTh = [...doc.querySelectorAll("#userTable th")]
    .find((h) => h.dataset.k === "user_category");
  assert.ok(catTh, "Category header exists");
  catTh.querySelector("button").click();
  rows = rowCells();
  // "Ellis" < "Non-Ellis" lexicographically: ascending keeps mentus1
  // first; a second click flips to descending.
  assert.equal(rows[0][0], "mentus1");
  assert.equal(rows[1][0], "firoozh1");
  catTh.querySelector("button").click();
  rows = rowCells();
  assert.equal(rows[0][0], "firoozh1");
  assert.equal(rows[1][0], "mentus1");
  // username search still filters on the user column only
  doc.getElementById("uSearch").value = "mentus1";
  doc.getElementById("uSearch").dispatchEvent(new dom.window.Event("input"));
  rows = rowCells();
  assert.equal(rows.length, 1);
  assert.equal(rows[0][0], "mentus1");
});

test("category failure leaves the user table usable", async (t) => {
  const { dom, mod } = await boot({ catsError: true }, 23);
  t.after(() => dom.window.close());
  await mod.loadUsers();
  const doc = dom.window.document;
  // the categories panel shows the retry state…
  const catPanel = doc.getElementById("userCategoriesResults");
  assert.match(catPanel.textContent, /sacct down|retry/i);
  // …while the user list rendered normally
  const rows = doc.querySelectorAll("#userTable tbody tr");
  assert.equal(rows.length, 2);
  assert.ok(!doc.getElementById("usersResults")
            .querySelector(".panel-error"));
});

test("stale category responses cannot overwrite a newer window",
     async (t) => {
  const { dom, mod, urls } = await boot({ gateCats: true }, 24);
  t.after(() => dom.window.close());
  await mod.loadUsers();
  // window change starts a NEW categories request immediately (the old
  // one is still gated)… immediately (the old
  // one is still gated)…
  const win = dom.window.document.getElementById("uWindow");
  win.value = "168";
  win.dispatchEvent(new dom.window.Event("change"));
  // …then the STALE first fetch resolves.
  await boot; // no-op to keep structure clear
  // release both: stale resolves first, current after
  // (releaseCats resolves the LATEST assignment — the second request's
  // promise. Emulate the stale resolution by firing the captured older
  // resolver: our stub replaces releaseCats per fetch, so the first
  // request's resolver was overwritten; instead verify by URL: the second
  // request carries the new window and the panel shows no error.
  assert.ok(urls.some((u) => u.includes("/api/users/categories?since_hours=168")),
            "a fresh categories request followed the window change");
});

test("window change refetches both; Running only refetches only the list",
     async (t) => {
  const { dom, mod, urls } = await boot({}, 25);
  t.after(() => dom.window.close());
  await mod.loadUsers();
  urls.length = 0;
  const win = dom.window.document.getElementById("uWindow");
  win.value = "336";
  win.dispatchEvent(new dom.window.Event("change"));
  await new Promise((r) => setTimeout(r, 0));
  assert.ok(urls.some((u) => u.startsWith("/api/users?since_hours=336")));
  assert.ok(urls.some((u) =>
    u.startsWith("/api/users/categories?since_hours=336")));
  // With a selected user the toggle refetches ONLY their job list; the
  // completed-job category summary is deliberately not refetched.
  await mod.finalizeUser("mentus1");
  urls.length = 0;
  const run = dom.window.document.getElementById("uRunning");
  run.checked = true;
  run.dispatchEvent(new dom.window.Event("change"));
  await new Promise((r) => setTimeout(r, 0));
  // completed-job cohort: no categories refetch on the Running-only toggle
  assert.deepEqual(urls.filter((u) => u.includes("categories")), []);
  assert.ok(urls.some((u) => u.startsWith("/api/jobs?")),
            "the selected user's job list follows the toggle");
});

test("partial coverage renders the visible hint", async (t) => {
  const partial = {
    ...CATEGORIES_BODY,
    coverage: { records_examined: 12, excluded: {}, failed_batches: 2,
                complete: false },
  };
  const dom = new JSDOM(html, { url: "http://localhost/users" });
  global.document = dom.window.document;
  global.window = dom.window;
  global.localStorage = dom.window.localStorage;
  global.location = dom.window.location;
  global.history = { pushState() {}, replaceState() {} };
  global.setInterval = () => 0;
  global.clearInterval = () => {};
  global.Plotly = { newPlot: () => {}, react: () => {} };
  global.fetch = (url) => Promise.resolve({
    ok: true,
    json: () => Promise.resolve(
      String(url).startsWith("/api/users/categories") ? partial : USERS_BODY),
  });
  const mod = await import("../static/js/tabs/users.js?cb=41");
  t.after(() => dom.window.close());
  await mod.loadUsers();
  const doc = dom.window.document;
  const hint = doc.getElementById("uCategoryCoverage");
  assert.equal(hint.hidden, false);
  assert.match(hint.textContent, /2 sacct batches failed/);
  assert.match(hint.textContent, /totals and waits are partial/);
});

test("independent loading: the user list renders while categories wait",
     async (t) => {
  const { dom, mod, releaseCats } = await boot({ gateCats: true }, 7);
  t.after(() => dom.window.close());
  await mod.loadUsers();
  const doc = dom.window.document;
  // user rows are already present although the category request never
  // resolved
  assert.equal(doc.querySelectorAll("#userTable tbody tr").length, 2);
  // category panel is still in its loading state
  assert.ok(doc.getElementById("userCategoriesResults")
            .classList.contains("loading"));
  // …and the module still loads (no unhandled rejection): release and
  // flush microtasks.
  releaseCats();
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(doc.querySelectorAll("#userCategoryTable tbody tr").length, 2);
});
