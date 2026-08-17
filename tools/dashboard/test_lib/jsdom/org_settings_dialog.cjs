// Organization settings dialog — container behaviour, browser-free.
//
// Category B: data -> render -> interact -> assert DOM. No layout assertions;
// the two responsive layouts are CSS and stay on a real browser. What IS
// testable here is the thing CSS cannot decide on its own: which pane the
// dialog says is showing, and that a screen the container has never heard of
// renders because it registered rather than because the container knows it.
//
// Exits 0/1.
const { JSDOM } = require("jsdom");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const assert = require("node:assert/strict");

const source = readFileSync(
  resolve(__dirname, "../../static/js/org-settings.js"), "utf8");

function boot({ narrow = false } = {}) {
  const dom = new JSDOM("<!DOCTYPE html><html><body></body></html>", {
    runScripts: "dangerously", pretendToBeVisual: true, url: "http://localhost/",
  });
  const { window } = dom;
  // The dialog asks which layout it is in exactly once, to decide whether to
  // open on the list or on a screen. Everything after that is CSS.
  window.matchMedia = (query) => ({
    matches: narrow && /max-width/.test(query),
    media: query, addListener() {}, removeListener() {},
    addEventListener() {}, removeEventListener() {},
  });
  const s = window.document.createElement("script");
  s.textContent = source;
  window.document.head.appendChild(s);
  return window;
}

const q = (w, sel) => w.document.querySelector(sel);

// A screen renders through a promise chain — every screen may fetch, so none
// of them resolve synchronously. Settling the microtask queue is what a
// reader waits for too; asserting before it is asserting on a pane that has
// not been filled yet.
const settle = () => new Promise((r) => setTimeout(r, 0));

async function main() {

// ── it ships with the workspaces screen ──────────────────────
{
  const w = boot();
  const ids = w.AutonomyOrgSettings.screens().map((s) => s.id);
  assert.ok(ids.includes("workspaces"), "workspaces screen is not registered");
}

// ── a screen it has never heard of renders ───────────────────
{
  const w = boot();
  w.AutonomyOrgSettings.register({
    id: "invented", label: "Invented", order: 1,
    render: () => {
      const node = w.document.createElement("div");
      node.setAttribute("data-testid", "invented-body");
      node.textContent = "hello";
      return node;
    },
  });
  w.AutonomyOrgSettings.open("acme");
  await settle();

  assert.ok(q(w, '[data-testid="orgset-rail-invented"]'), "no rail entry");
  // order:1 sorts ahead of the shipped screen, so it is the opening one and
  // renders without anything being selected by hand.
  const body = q(w, '[data-testid="invented-body"]');
  assert.ok(body, "a registered screen did not render");
  assert.equal(body.textContent, "hello");
}

// ── the dialog states which pane is up ───────────────────────
{
  const w = boot({ narrow: true });
  w.AutonomyOrgSettings.register({
    id: "a", label: "A", order: 1,
    render: () => w.document.createElement("div"),
  });
  w.AutonomyOrgSettings.open("acme");
  const dialog = q(w, ".orgset-dialog");

  // Narrow opens on the list: the list IS the first screen there, and
  // landing on a detail pane hides the way back to everything else.
  assert.equal(dialog.getAttribute("data-view"), "list");

  q(w, '[data-testid="orgset-rail-a"]').dispatchEvent(
    new w.Event("click", { bubbles: true }));
  assert.equal(dialog.getAttribute("data-view"), "detail");

  q(w, '[data-testid="orgset-back"]').dispatchEvent(
    new w.Event("click", { bubbles: true }));
  assert.equal(dialog.getAttribute("data-view"), "list",
    "back did not return to the list");
}

// ── wide opens on a screen, not on an empty pane ─────────────
{
  const w = boot({ narrow: false });
  w.AutonomyOrgSettings.register({
    id: "a", label: "A", order: 1,
    render: () => w.document.createElement("div"),
  });
  w.AutonomyOrgSettings.open("acme");

  assert.equal(q(w, ".orgset-dialog").getAttribute("data-view"), "detail");
}

// ── escape backs out before it closes, on narrow ─────────────
{
  const w = boot({ narrow: true });
  w.AutonomyOrgSettings.register({
    id: "a", label: "A", order: 1,
    render: () => w.document.createElement("div"),
  });
  w.AutonomyOrgSettings.open("acme");
  q(w, '[data-testid="orgset-rail-a"]').dispatchEvent(
    new w.Event("click", { bubbles: true }));

  const esc = new w.KeyboardEvent("keydown", { key: "Escape", bubbles: true });
  w.document.dispatchEvent(esc);
  assert.ok(q(w, '[data-testid="org-settings"]'),
    "escape closed the whole dialog from a detail screen");
  assert.equal(q(w, ".orgset-dialog").getAttribute("data-view"), "list");

  w.document.dispatchEvent(
    new w.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  assert.equal(q(w, '[data-testid="org-settings"]'), null,
    "escape from the list did not close");
}

// ── a screen that fails says so, and does not blank the dialog ──
{
  const w = boot();
  w.AutonomyOrgSettings.register({
    id: "bad", label: "Bad", order: 1,
    render: () => Promise.reject(new Error("the server said no")),
  });
  w.AutonomyOrgSettings.open("acme");
  await settle();

  const err = q(w, ".orgset-error");
  assert.ok(err, "a failing screen rendered nothing at all");
  assert.match(err.textContent, /the server said no/);
  assert.ok(q(w, ".orgset-rail"), "the rail went away with the screen");
}

// ── an answer that arrives after the reader moved on is dropped ──
{
  const w = boot();
  let release;
  w.AutonomyOrgSettings.register({
    id: "slow", label: "Slow", order: 1,
    render: () => new Promise((r) => { release = () => r(
      Object.assign(w.document.createElement("div"),
                    { textContent: "stale" })); }),
  });
  w.AutonomyOrgSettings.register({
    id: "fast", label: "Fast", order: 2,
    render: () => Object.assign(w.document.createElement("div"),
                                { textContent: "current" }),
  });
  w.AutonomyOrgSettings.open("acme");

  // Move to the other screen while the first is still in flight, then let it
  // finish. A late answer landing in the pane puts a stale body under a fresh
  // heading, which reads as current and is not.
  q(w, '[data-testid="orgset-rail-fast"]').dispatchEvent(
    new w.Event("click", { bubbles: true }));
  await settle();
  release();
  await settle();

  assert.match(q(w, ".orgset-pane").textContent, /current/);
  assert.doesNotMatch(q(w, ".orgset-pane").textContent, /stale/,
    "a screen the reader had left rendered over the one they chose");
}

console.log("PASS: org settings dialog — registration, push navigation, "
            + "escape, failure isolation, stale-render drop");
}

main().catch((err) => { console.error(err); process.exit(1); });
