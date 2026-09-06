// Backup page fragment sweep (jsdom + vendored Alpine).
//
// Renders page.html + page.js with mocked APIs and asserts the §0
// ordering promises: the hero states the verdict, failures pin to the
// top of the run stream, the store table shows the newest run's rows,
// and a never-drilled deployment says so instead of showing nothing.
"use strict";
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const ROOT = path.resolve(__dirname, "..", "..");
const REPO = path.resolve(ROOT, "..", "..", "..", "..");
const fragment = fs.readFileSync(path.join(ROOT, "page.html"), "utf8");
const pageJs = fs.readFileSync(path.join(ROOT, "page.js"), "utf8");
const alpine = fs.readFileSync(path.join(
  REPO, "tools", "dashboard", "static", "vendor",
  "alpine-3.15.12.min.js"), "utf8");

let failures = 0;
function check(name, cond) {
  if (!cond) { failures += 1; console.error("FAIL: " + name); }
}

const FIXTURES = {
  "/api/backup/summary": {
    overall: "failing",
    tiers: [
      { tier: "hourly", status: "failing", age_seconds: 4200,
        stale_after_seconds: 10800, last_success_key: "hourly:20260906-001110",
        offsite: "complete", total_bytes: 2612312145 },
      { tier: "daily", status: "ok", age_seconds: 32000,
        stale_after_seconds: 259200, last_success_key: "daily:20260906-030000",
        offsite: "complete", total_bytes: 2612312145 },
    ],
    last_drill: null,
    running_drill: null,
    config: { staleness_multiple: 3.0, schedule_owner: "cron" },
  },
  "/api/backup/runs": { runs: [
    { key: "hourly:20260906-020000", verdict: "complete", store_count: 24,
      beads_databases: 3, total_bytes: 2612312145, offsite: "complete",
      origin: "host", failures: [],
      stores: [
        { name: "orgs/autonomy.db", action: "sqlite", status: "ok",
          bytes: 1557696512 },
        { name: "tls.key", action: "copy", status: "ok", bytes: 241 },
      ]},
    { key: "hourly:20260906-030000", verdict: "failed", store_count: 23,
      beads_databases: 2, total_bytes: 1054615633, offsite: "unknown",
      origin: "host",
      failures: ["beads/blindhash: Unknown MySQL server host 'dolt'"],
      stores: [] },
  ]},
  "/api/backup/drills": { drills: [] },
};

const html = `<!doctype html><html><head></head><body>
<header><input id="global-search"><div id="app-topbar-slot"></div></header>
<main id="content">${fragment}</main>
</body></html>`;

const dom = new JSDOM(html, {
  runScripts: "dangerously",
  url: "https://localhost:8080/backup",
  pretendToBeVisual: true,
});
const { window } = dom;
window.fetch = (url) => {
  const key = String(url).split("?")[0];
  const body = FIXTURES[key];
  return Promise.resolve({
    ok: !!body, status: body ? 200 : 404,
    json: () => Promise.resolve(body || {}),
  });
};
window.eval(pageJs);
window.eval(alpine);

setTimeout(() => {
  try {
    const d = window.document;
    const root = d.querySelector('[data-testid="backup-fragment-root"]');
    check("fragment mounted", !!root);
    const comp = window.Alpine.$data(root);

    // Hero answers "am I safe now" with the worst verdict.
    const overall = d.querySelector('[data-testid="backup-overall"]');
    check("hero shows FAILING", overall
      && overall.textContent.trim() === "FAILING");

    // Both tiers render with their own status marks.
    check("hourly tier row", !!d.querySelector(
      '[data-testid="backup-tier-hourly"]'));
    check("daily tier row", !!d.querySelector(
      '[data-testid="backup-tier-daily"]'));

    // Failures pin first in the run stream despite being newer/older.
    check("failed run pinned first",
      comp.runStream.length === 2
      && comp.runStream[0].verdict === "failed");
    const stream = d.querySelector('[data-testid="backup-run-stream"]');
    check("failure reason rendered", stream
      && stream.textContent.includes("Unknown MySQL server host"));

    // Store table serves the NEWEST hourly run (the failed one has no
    // stores; newest by stamp is 030000 which is the failed one — its
    // empty stores fall through to the placeholder).
    check("store table exists", !!d.querySelector(
      '[data-testid="backup-store-table"]'));
    comp.storeTier = "daily";
    check("tier toggle empties table for absent tier",
      comp.storeRows.length === 0);
    comp.storeTier = "hourly";
    check("newest hourly run selected for stores",
      comp.storeRows.length === 0);  // 030000 (failed, no stores) is newest

    // Never drilled: the panel says so, in words.
    const drill = d.querySelector('[data-testid="backup-drill-panel"]');
    check("undrilled state is explicit", drill
      && drill.textContent.includes("Never drilled"));

    // Config renders the policy in force.
    const config = d.querySelector('[data-testid="backup-config"]');
    check("config lists staleness_multiple", config
      && config.textContent.includes("staleness_multiple"));

    if (failures === 0) console.log("PASS backup_page");
    process.exit(failures === 0 ? 0 : 1);
  } catch (err) {
    console.error("HARNESS ERROR: " + (err && err.stack || err));
    process.exit(1);
  }
}, 300);
