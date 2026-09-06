// Backup page fragment sweep (jsdom + vendored Alpine).
//
// Reworked with the operator's 2026-09-06 design review: asserts the
// plain-language promises — a tier table with headers, an overall
// explanation in words, the destinations panel with credential state,
// one contents table from the newest complete run, config fields with
// descriptions, and the topbar claimed as "Backup".
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
        stale_after_seconds: 10800, last_success_key: "hourly:20260906-020000",
        offsite: "complete", total_bytes: 2612312145,
        last_run: { key: "hourly:20260906-030000", verdict: "failed",
                    failures: ["beads/blindhash: Unknown MySQL server host 'dolt'"] } },
      { tier: "daily", status: "ok", age_seconds: 32000,
        stale_after_seconds: 259200, last_success_key: "daily:20260906-030000",
        offsite: "complete", total_bytes: 2612312145, last_run: null },
    ],
    last_drill: null,
    running_drill: null,
    config: { staleness_multiple: 3.0 },
    destinations: {
      local_root: "/opt/autonomy/data/backups",
      data_root: "/opt/autonomy/data",
      provider: "b2", bucket: "autonomy",
      repository: "rclone:b2:autonomy/restic",
      credentials: "ok",
      repo_bytes: 48318382080,
      vault_rows: ["backup.restic-password"],
    },
  },
  "/api/backup/runs": { runs: [
    { key: "hourly:20260906-020000", verdict: "complete", store_count: 32,
      beads_databases: 3, total_bytes: 2612312145, offsite: "complete",
      origin: "host", failures: [],
      stores: [
        { name: "orgs/autonomy.db", action: "sqlite", status: "ok",
          bytes: 1557696512 },
        { name: "tls.key", action: "copy", status: "ok", bytes: 241 },
      ]},
    { key: "hourly:20260906-030000", verdict: "failed", store_count: 31,
      beads_databases: 2, total_bytes: 1054615633, offsite: "unknown",
      origin: "host",
      failures: ["beads/blindhash: Unknown MySQL server host 'dolt'"],
      stores: [] },
  ]},
  "/api/backup/drills": { drills: [] },
  "/api/backup/config": {
    config: { staleness_multiple: 3.0, offsite_provider: "b2" },
    fields: {
      staleness_multiple: {
        description: "A tier is stale when age exceeds this multiple",
        type: "number" },
      offsite_provider: {
        description: "Offsite provider", type: "string",
        enum: ["", "b2", "r2", "s3"] },
    },
    editable: true,
  },
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

    // Topbar claimed with the app name, never someone else's leftovers.
    check("topbar claimed",
      d.querySelector("header").classList.contains("app-topbar-active"));
    check("topbar titled Backup",
      d.querySelector("#app-topbar-slot").textContent.includes("Backup"));

    // Hero: verdict + a WHY in words.
    const overall = d.querySelector('[data-testid="backup-overall"]');
    check("hero shows FAILING", overall
      && overall.textContent.trim() === "FAILING");
    const why = d.querySelector('[data-testid="backup-overall-why"]');
    check("hero explains why", why
      && why.textContent.includes("hourly:")
      && why.textContent.includes("blindhash"));

    // Tier table with headers and one labeled row per schedule.
    const tierTable = d.querySelector('[data-testid="backup-tier-table"]');
    check("tier table has headers", tierTable
      && tierTable.textContent.includes("Last good backup")
      && tierTable.textContent.includes("Offsite copy"));
    check("hourly row", !!d.querySelector('[data-testid="backup-tier-hourly"]'));
    check("daily row", !!d.querySelector('[data-testid="backup-tier-daily"]'));

    // Destinations: where, credentials state, provider size.
    const dest = d.querySelector('[data-testid="backup-destinations"]');
    check("local destination shown", dest
      && dest.textContent.includes("/opt/autonomy/data/backups"));
    check("repository shown", dest
      && dest.textContent.includes("rclone:b2:autonomy/restic"));
    check("provider total shown", dest
      && dest.textContent.includes("total stored"));
    check("credentials in words",
      d.querySelector('[data-testid="backup-credentials"]')
        .textContent.includes("sealed in the vault"));

    // Failures pinned first with the reason rendered.
    check("failed run pinned first",
      comp.runStream.length === 2 && comp.runStream[0].verdict === "failed");
    check("failure reason rendered",
      d.querySelector('[data-testid="backup-run-stream"]')
        .textContent.includes("Unknown MySQL server host"));

    // One contents table from the newest COMPLETE run — no tier toggle.
    check("contents from newest complete run",
      comp.latestContents().key === "hourly:20260906-020000");
    check("contents table lists stores",
      d.querySelector('[data-testid="backup-store-table"]')
        .textContent.includes("orgs/autonomy.db"));
    check("no tier toggle survives", !d.querySelector(".bk-tier-btn"));

    // Drill: explained, runnable, honest when never run.
    check("drill explained in words",
      d.body.textContent.includes("proof the backups can"));
    check("run drill button enabled",
      !d.querySelector('[data-testid="backup-drill-run"]').disabled);
    check("undrilled state explicit",
      d.querySelector('[data-testid="backup-drill-panel"]')
        .textContent.includes("Never drilled"));

    // Config: descriptions + editable controls.
    const config = d.querySelector('[data-testid="backup-config"]');
    check("config field described", config
      && config.textContent.includes("stale when age exceeds"));
    check("config enum renders select",
      !!config.querySelector("select#bk-cfg-offsite_provider"));
    check("config save button",
      !!d.querySelector('[data-testid="backup-config-save"]'));

    if (failures === 0) console.log("PASS backup_page");
    process.exit(failures === 0 ? 0 : 1);
  } catch (err) {
    console.error("HARNESS ERROR: " + (err && err.stack || err));
    process.exit(1);
  }
}, 300);
