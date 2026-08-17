// Browser-free migration of the behavioral-sweep pure-logic checks for the
// production window.applyTurnCorrection handler (test_behavioral_sweep.py
// TURN_CORRECTION_STALE_ORDERING + TURN_CORRECTION_TWO_STORE_CONVERGE). The
// function is pure — it mutates a plain store object and returns a bool — so it
// needs no browser, no layout, no fetch. Here it runs against the REAL
// static/js/lib/session-store.js loaded into jsdom (which supplies `document`
// so the script's alpine:init listener registers without firing).
//
// Exits 0 if every assertion holds, 1 otherwise. Requires jsdom (require via
// NODE_PATH; see agents/Dockerfile).
const { JSDOM } = require("jsdom");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const assert = require("node:assert");

const storeSrc = resolve(__dirname, "../../static/js/lib/session-store.js");
const dom = new JSDOM("<!DOCTYPE html><body></body>",
  { runScripts: "dangerously", url: "http://localhost/" });
const { window } = dom;
const script = window.document.createElement("script");
script.textContent = readFileSync(storeSrc, "utf8");
window.document.body.appendChild(script);   // registers applyTurnCorrection; does NOT fire alpine:init

const apply = window.applyTurnCorrection;
assert.strictEqual(typeof apply, "function", "window.applyTurnCorrection must be defined by session-store.js");

// ── stale-ordering: terminal state is not regressed by a late older row ──
{
  const s = { _turnCorrections: {} };
  apply(s, { target_message_id: "m", status: "accepted", updated_at: 2, corrected_text: "b" });
  const staleRefused = apply(s, { target_message_id: "m", status: "pending", updated_at: 1, corrected_text: "b" });
  assert.strictEqual(staleRefused, false, "a stale (older) row must be refused");
  assert.strictEqual(s._turnCorrections.m.status, "accepted", "terminal status must survive a stale row");
  const newerApplied = apply(s, { target_message_id: "m", status: "dismissed", updated_at: 3, corrected_text: "b" });
  assert.strictEqual(newerApplied, true, "a genuinely newer terminal row must advance");
  assert.strictEqual(s._turnCorrections.m.status, "dismissed", "newer row must update status");
  const equalApplied = apply(s, { target_message_id: "m", status: "dismissed", updated_at: 3, corrected_text: "b" });
  assert.strictEqual(equalApplied, true, "an equal-updated_at row (optimistic rollback) must still apply");
}

// ── two stores converge on the same committed row ──
{
  const a = { _turnCorrections: {} };
  const b = { _turnCorrections: {} };
  const committed = { session_uuid: "s", target_message_id: "m1", status: "pending",
    original_sha256: "h", corrected_text: "converged text", mode: null, reason: null,
    confidence: null, created_at: 1, updated_at: 1 };
  const ra = apply(a, committed);
  const rb = apply(b, committed);
  assert.ok(ra === true && rb === true, "both stores must report applied");
  assert.ok(a._turnCorrections.m1 && b._turnCorrections.m1, "both stores must hold the row");
  assert.strictEqual(JSON.stringify(a._turnCorrections), JSON.stringify(b._turnCorrections),
    "independent stores must converge to identical state");
}

console.log("PASS: applyTurnCorrection stale-ordering + two-store convergence");
