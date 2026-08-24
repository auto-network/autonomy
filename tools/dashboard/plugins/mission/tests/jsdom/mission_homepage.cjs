// Mission homepage fragment sweep (jsdom + vendored Alpine).
//
// The acceptance gate the fragment never had: renders page.html +
// page.js with mocked APIs and asserts the invariants every shipped
// regression violated — the legend and the list share one filter
// universe; the org defaults to where missions are; the Sessions
// screen populates; opening a mission produces a frame with a real
// src and a nonzero measured height.
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

const NOW = Date.now() / 1000;
const FIXTURES = {
  "/api/orgs": {orgs: [
    {org: {slug: "anchore"}, identity: {payload: {slug: "anchore",
      name: "Anchore", color: "#199e70", initial: "A"}}},
    {org: {slug: "autonomy"}, identity: {payload: {slug: "autonomy",
      name: "Autonomy Network", color: "#6C63FF", initial: "A"}}},
  ]},
  "/api/mission/missions": {missions: [
    {mission_id: "m1", org: "autonomy", name: "Multi-User Autonomy",
     status: "active",
     activity: {last_at: NOW - 3600, days: [], events: [NOW - 3600],
                previews: {}, blockers: 1, in_progress: 2,
                open_questions: 3}},
    {mission_id: "m2", org: "autonomy", name: "Second Mission",
     status: "paused", status_changed_at: new Date().toISOString(),
     activity: {last_at: NOW - 7200, days: [], events: [NOW - 7200],
                previews: {}, blockers: 0, in_progress: 0,
                open_questions: 0}},
  ]},
  "/api/mission/allocation": {missions: [
    {mission_id: "m1", org: "autonomy", name: "Multi-User Autonomy",
     status: "active", pillars: [
       {pillar_id: "relay", name: "Relay Network", color: "#3987e5",
        session: "auto-1", session_title: "Fleet tunnel alpha",
        live: true},
       {pillar_id: "crypto", name: "Crypto", color: "#8b6fd0",
        session: "", session_title: "", live: false}]},
  ]},
};

// shell scaffolding the fragment expects: header + topbar slot
const html = `<!doctype html><html><head></head><body>
<header><input id="global-search"><div id="app-topbar-slot"></div></header>
<main id="content">${fragment}</main>
</body></html>`;

const dom = new JSDOM(html, {
  runScripts: "dangerously",
  url: "https://localhost:8080/mission",
  pretendToBeVisual: true,
});
const { window } = dom;
window.localStorage.clear();
window.fetch = (url) => {
  const key = String(url).split("?")[0];
  if (key.startsWith("/api/mission/screen/")) {
    // the ?progress=1 stream shape: stage markers, doc marker, document
    const doc = "<!doctype html><html><body>screen</body></html>";
    const html = "<!--msn:16|Loading mission items — 7 of 78 settings-->"
      + "<!--msn:doc:" + doc.length + "-->" + doc;
    return Promise.resolve({
      ok: true, status: 200,
      headers: {get: () => null},
      body: null,                      // exercises the text() fallback
      text: () => Promise.resolve(html),
    });
  }
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
    const t = (sel) => Array.from(d.querySelectorAll(sel),
      (e) => e.textContent.trim());

    // org defaulted to where the missions are, not first-of-list
    const comp = window.Alpine.$data(
      d.querySelector('[data-testid="mission-fragment-root"]'));
    check("org defaults to mission org", comp.org === "autonomy");

    // toolbar claimed + selector teleported
    check("toolbar claimed",
      d.querySelector("header").classList.contains("app-topbar-active"));
    // icon-only selector: initial visible, full names only in the menu
    const slot = d.querySelector("#app-topbar-slot");
    check("org icon in slot", slot.textContent.includes("A"));
    comp.orgOpen = true;
    check("menu carries full names (deferred check below)", true);

    // THE invariant: legend total === visible rows
    const legendTotal = comp.legend().reduce((a, x) => a + x.n, 0);
    const rows = d.querySelectorAll(".msn-row").length;
    check("legend and list share one universe (" + legendTotal
      + " vs " + rows + ")", legendTotal === rows && rows === 2);

    // counts render only on the active mission
    check("counts on active only",
      d.querySelectorAll(".msn-ct").length === 3);

    // sessions screen populates with titles beside pillar names
    comp.screen = "sessions";
    setTimeout(() => {
      check("sessions rows", d.querySelectorAll(".msn-arow").length === 2);
      check("session title shown",
        t(".msn-arow .stitle").includes("Fleet tunnel alpha"));
      check("unassigned shown",
        t(".msn-arow .sid").includes("unassigned"));

      // opening a mission: src set + measured nonzero height
      comp.screen = "missions";
      const histBefore = window.history.length;
      comp.open(FIXTURES["/api/mission/missions"].missions[0]);
      // the interstitial is SYNCHRONOUS with the tap
      check("interstitial instant", comp.loading === true);
      setTimeout(() => {
        const frame = d.querySelector("iframe");
        check("frame exists", !!frame);
        check("frame content mounted", !!frame
          && typeof frame.srcdoc === "string" && frame.srcdoc.length > 0);
        // progress markers are narration, never document content
        check("markers stripped from srcdoc", !!frame
          && frame.srcdoc.startsWith("<!doctype")
          && !frame.srcdoc.includes("msn:"));
        check("interstitial bar rendered",
          !!d.querySelector(".msn-load-bar i") || comp.loading === false);
        const h = frame && parseInt(frame.style.height || "0", 10);
        check("frame measured height > 0 (" + h + ")", h > 100);
        // the URL NAMES the page but never grows history: the browser
        // back button stays a pure exit from the plugin
        check("url named via replaceState",
          window.location.pathname === "/mission/m1");
        check("no history entry added ("
          + histBefore + " -> " + window.history.length + ")",
          window.history.length === histBefore);
        // the inner screen reports its position; it lands in the hash
        window.dispatchEvent(new window.MessageEvent("message", {
          data: {type: "mission:where", view: "relay", section: "Delivery"},
        }));
        check("position mirrored into hash",
          window.location.hash === "#view=relay&tab=Delivery");
        check("no in-page back bar", !d.querySelector(".msn-list + div .border-b"));
        check("back chevron in toolbar",
          !!d.querySelector('#app-topbar-slot [aria-label="Back to missions"]'));
        check("mission title in toolbar",
          d.querySelector("#app-topbar-slot").textContent
            .includes("Multi-User Autonomy"));
        check("org menu names render", d.querySelector("#app-topbar-slot")
          .textContent.includes("Autonomy Network"));

        if (failures) {
          console.error(failures + " assertion(s) failed");
          process.exit(1);
        }
        console.log("PASS homepage");
        process.exit(0);
      }, 60);
    }, 60);
  } catch (err) {
    console.error("FAIL (exception): " + (err && err.stack || err));
    process.exit(1);
  }
}, 250);
