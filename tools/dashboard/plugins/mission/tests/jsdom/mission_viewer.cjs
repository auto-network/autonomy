// Mission viewer integration assertions (jsdom, no browser).
//
// Usage: node mission_viewer.cjs <document.html> <scenario>
// The document is a REAL composed mission screen (settings -> compose ->
// bytes); this script loads it with scripts enabled and asserts on the
// rendered DOM per scenario. Exits 0 + prints PASS on success.
"use strict";
const fs = require("fs");
const { JSDOM } = require("jsdom");

const [, , docPath, scenario] = process.argv;
if (!docPath || !scenario) {
  console.error("usage: mission_viewer.cjs <document.html> <scenario>");
  process.exit(2);
}

let failures = 0;
function check(name, cond) {
  if (!cond) {
    failures += 1;
    console.error("FAIL: " + name);
  }
}
function texts(root, sel) {
  return Array.from(root.querySelectorAll(sel), (e) => e.textContent.trim());
}

const html = fs.readFileSync(docPath, "utf8");
const dom = new JSDOM(html, {
  runScripts: "dangerously",
  url: "https://localhost:8080/api/mission/screen/test",
  pretendToBeVisual: true,
});
const { window } = dom;
const { document } = window;

function openPillar(name) {
  document.getElementById("tab-pillars").click();
  const btn = document.querySelector(".mc-psel-btn");
  if (btn) btn.click();
  const row = Array.from(document.querySelectorAll(".mc-psel-row")).find(
    (r) => r.textContent.includes(name));
  if (row) row.click();
  return document.querySelector(".mc-view.on");
}

const SCENARIOS = {
  full() {
    // ── chrome: the five mission tabs ──
    check("top tabs", texts(document, ".mc-tabs button").join("|") ===
      "Activity|Pillars|Blockers|Feed|Chat");

    // ── pillar view: subtabs + amber attention counts ──
    const v = openPillar("Relay");
    check("pillar view on", !!v);
    const subs = Array.from(v.querySelectorAll(".mc-subtabs button"), (b) => {
      const sup = b.querySelector("sup");
      return b.childNodes[0].textContent + (sup ? "^" + sup.textContent : "");
    });
    check("subtabs+counts",
      subs.join("|") === "News|Delivery^1|Tasks^1|Questions^1|Decisions^1");

    // ── news: newest first, full local stamp leading each entry ──
    v._showSection("News");
    const stamps = texts(v, '.mc-sec[data-sec="News"] .mc-newsstamp');
    const bodies = texts(v, '.mc-sec[data-sec="News"] .mc-status-prose .body');
    check("news newest first", bodies[0].includes("Newest update"));
    check("news stamp present", stamps.length === 2 && /2026/.test(stamps[0]));
    check("no charter on news",
      !v.querySelector('.mc-sec[data-sec="News"]').textContent
        .includes("One personal fleet"));

    // ── charter behind the info toggle ──
    const eye = document.querySelector(".pinfo");
    check("charter eye", !!eye);
    eye.click();
    check("charter opens", v.querySelector(".mc-charter").style.display !== "none"
      && v.querySelector(".mc-charter").textContent
          .includes("One personal fleet"));
    check("tabs hidden under charter",
      v.querySelector(".mc-subtabs").style.display === "none");
    eye.click();

    // ── delivery: legend words, ladder order, ready-to-confirm ──
    v._showSection("Delivery");
    const dl = texts(v, '.mc-sec[data-sec="Delivery"] .mc-flegend .fl .w2');
    check("delivery legend", dl.join("|") === "confirmed|in progress|pending");
    // Order doctrine: confirmed gates lead the arc; the rest follow
    // bead dependency topology (revoke's bead is complete -> depth 0,
    // the live exchange depends on a running bead -> deeper).
    const critTitles = texts(v, '.mc-sec[data-sec="Delivery"] .mc-crit .t');
    check("confirmed leads, topo orders the rest",
      critTitles[0].includes("second machine joins") &&
      critTitles[1].includes("revoked machine") &&
      critTitles[2].includes("live exchange"));
    check("ready to confirm",
      v.querySelector('.mc-sec[data-sec="Delivery"]').textContent
        .includes("ready to confirm"));

    // ── criterion page: provenance, evidence, work log, pager ──
    const crit = Array.from(v.querySelectorAll(".mc-crit")).find(
      (r) => r.textContent.includes("live exchange"));
    crit.click();
    let page = document.getElementById("mc-critpage");
    check("criterion page", !!page);
    // breadcrumbs always name section AND pillar (operator ruling)
    check("delivery breadcrumb names section + pillar",
      page.querySelector(".cback").textContent.includes("Delivery")
      && page.querySelector(".cback").textContent.includes("Relay"));
    check("worked meta", page.querySelector(".meta3").textContent
      .includes("worked"));
    check("work log entries",
      texts(page, ".qprog").some((t) => t.includes("reconnect fixed")));
    check("deliverable pager",
      page.querySelector(".cpos").textContent.startsWith("deliverable"));
    page.querySelector(".cback").click();
    check("back closes", !document.getElementById("mc-critpage"));

    // ── tasks: ladder legend always shows all four; epic chip; page ──
    v._showSection("Tasks");
    const tsec = v.querySelector('.mc-sec[data-sec="Tasks"]');
    const tl = texts(tsec, ".mc-flegend .fl .w2");
    check("task legend ladder order",
      tl.join("|") === "defined|specified|running|complete");
    check("epic acceptance chip", !!tsec.querySelector(".mc-bead .ep"));
    const beadRow = Array.from(tsec.querySelectorAll(".mc-bead")).find(
      (r) => r.textContent.includes("auto-run1"));
    beadRow.click();
    page = document.getElementById("mc-critpage");
    check("task page sections",
      texts(page, ".sh3").join("|").includes("Specification") &&
      texts(page, ".sh3").join("|").includes("Comments"));
    check("bd comment attributed",
      page.textContent.includes("clarified in chat") &&
      !page.textContent.includes("terminal:"));
    check("task pager", page.querySelector(".cpos").textContent
      .startsWith("task"));
    page.remove();

    // ── questions: three-way legend, blocking first + stop icon ──
    v._showSection("Questions");
    const qsec = v.querySelector('.mc-sec[data-sec="Questions"]');
    check("question legend",
      texts(qsec, ".mc-flegend .fl .w2").join("|") ===
        "blocking|open|answered");
    const qRows = qsec.querySelectorAll(".mc-bead");
    check("blocking row first with stop icon",
      qRows[0].dataset.qstate === "blocking" &&
      !!qRows[0].querySelector(".gx-stop"));
    qRows[0].click();
    page = document.getElementById("mc-critpage");
    check("conversation meta", page.querySelector(".meta3").textContent
      .includes("blocking"));
    check("progress entry styled", !!page.querySelector(".qprog"));
    check("reply composer with doctrine hint",
      page.querySelector(".qhint").textContent.includes("does not resolve"));
    page.remove();

    // ── decisions: key legend, favorite star round trip ──
    v._showSection("Decisions");
    const dsec = v.querySelector('.mc-sec[data-sec="Decisions"]');
    check("decision legend counts",
      texts(dsec, ".mc-flegend .fl").join("|").includes("2decided") &&
      texts(dsec, ".mc-flegend .fl").join("|").includes("1favorite"));
    const plain = Array.from(dsec.querySelectorAll(".mc-bead")).find(
      (r) => !r.classList.contains("d-fav"));
    plain.click();
    page = document.getElementById("mc-critpage");
    check("decision sections", texts(page, ".sh3").join("|") ===
      "The fork|Chosen|If wrong");
    page.querySelector(".dstar").click();
    check("star updates row", plain.classList.contains("d-fav"));
    check("star updates legend count",
      dsec.querySelector('[data-word="favorite"] b').textContent === "2");
    page.querySelector(".dstar").click();
    page.remove();

    // ── blockers: grouped by pillar, only pillars with blockers ──
    document.getElementById("tab-questions").click();
    const bview = document.querySelector(".mc-view.on");
    check("blockers grouped",
      texts(bview, "h2").length === 1 &&
      texts(bview, "h2")[0].includes("Relay"));
    check("blockers breadcrumb", (() => {
      bview.querySelector(".mc-bead").click();
      const label = document.querySelector("#mc-critpage .cback")
        .textContent;
      document.getElementById("mc-critpage").remove();
      return label.includes("Blockers");
    })());

    // ── feed: event derivation + two-row legend ──
    document.getElementById("tab-feed").click();
    const fview = document.querySelector(".mc-view.on");
    const lines = Array.from(fview.querySelectorAll(".fl-line"), (l) =>
      l.querySelector(".fl-lab").textContent + ":" +
      texts(l, ".fl .w2").join(","));
    check("feed legend rows",
      lines.join("|") ===
        "tasks:blocking,progress,finished|updates:news,decided");
    const feedTexts = texts(fview, ".mc-feed-row .t");
    check("feed has confirm event",
      feedTexts.some((t) => t.startsWith("confirmed:")));
    check("feed has answer event",
      feedTexts.some((t) => t.startsWith("answered:")));
    check("feed has work event",
      feedTexts.some((t) => t.includes("reconnect fixed")));

    // ── the ask text renders on the question page (it was stored
    //    but invisible until the operator noticed) ──
    const askView = openPillar("Relay");
    askView._showSection("Questions");
    const askRow = Array.from(askView.querySelectorAll(
      '.mc-sec[data-sec="Questions"] .mc-bead')).find(
      (r) => r.textContent.includes("Who provisions"));
    askRow.click();
    const askPage = document.getElementById("mc-critpage");
    check("ask text renders on the question page",
      !!askPage.querySelector(".mc-ask")
      && askPage.textContent.includes("installer works offline"));
    askPage.remove();

    // ── chat: attributed log, no tracking controls ──
    document.getElementById("tab-chat").click();
    const cview = document.querySelector(".mc-view.on");
    // the persona pub key resolves through the injected directory to
    // the member's chosen org display name; sessions render as-is
    check("chat attribution resolves persona -> display name",
      texts(cview, ".qe .qwho b").join("|") === "Jeremy|auto-relay");
    check("no raw persona hex on screen",
      !/[0-9a-f]{32}/.test(cview.textContent));
    check("no promotion controls", !cview.querySelector(".mc-promote"));
    check("chat hint agent-owns-graduation",
      cview.querySelector(".qhint").textContent
        .includes("pillar decides"));

    // ── overview card deep links: card->News, labels->their sections,
    //    ask badge->Questions (operator ruling) ──
    document.getElementById("tab-overview").click();
    const card = Array.from(document.querySelectorAll(".mc-psum")).find(
      (c) => c.textContent.includes("Relay"));
    card.click();
    let pv = document.querySelector(".mc-view.on");
    check("card tap lands on News",
      (pv.querySelector(".mc-sec.on") || {}).dataset
        && pv.querySelector(".mc-sec.on").dataset.sec === "News");
    document.getElementById("tab-overview").click();
    const lb = Array.from(card.querySelectorAll(".lb-link")).find(
      (e) => e.textContent === "delivery");
    lb.click();
    pv = document.querySelector(".mc-view.on");
    check("delivery label deep-links",
      pv.querySelector(".mc-sec.on").dataset.sec === "Delivery");
    document.getElementById("tab-overview").click();
    const badge = card.querySelector(".mc-mini");
    if (badge) {
      badge.click();
      pv = document.querySelector(".mc-view.on");
      check("ask badge deep-links to Questions",
        pv.querySelector(".mc-sec.on").dataset.sec === "Questions");
    }
  },

  empty() {
    check("tabs render", texts(document, ".mc-tabs button").length === 5);
    const v = openPillar("Solo");
    check("pillar renders", !!v);
    check("all subtabs off", Array.from(
      v.querySelectorAll(".mc-subtabs button")).every(
        (b) => b.classList.contains("off")));
    document.getElementById("tab-questions").click();
    check("no blockers message", document.querySelector(".mc-view.on")
      .textContent.includes("No blockers"));
    document.getElementById("tab-feed").click();
    check("feed empty message", document.querySelector(".mc-view.on")
      .textContent.includes("Nothing has happened yet"));
  },
};

const run = SCENARIOS[scenario];
if (!run) {
  console.error("unknown scenario: " + scenario);
  process.exit(2);
}
try {
  run();
} catch (err) {
  failures += 1;
  console.error("FAIL (exception): " + (err && err.stack || err));
}
if (failures) {
  console.error(failures + " assertion(s) failed");
  process.exit(1);
}
console.log("PASS " + scenario);
