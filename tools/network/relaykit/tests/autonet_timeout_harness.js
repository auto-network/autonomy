/* Proves the bootloader's connect+handshake is bounded (the "that hung" fix).
 *
 * Loads the REAL autonet.js in a minimal Node shim and drives the REAL
 * performHandshake against a transport that opens but never sends
 * SERVER_HELLO — the exact shape of a relay holding a viewer socket open
 * with no serving tunnel dialed in. Without withTimeout this awaits forever;
 * with it, the wrapped promise rejects fast. Exit 0 = bounded, nonzero = hung
 * or wrong failure.
 */
"use strict";

const vm = require("vm");
const { loadAutonetTestSource } = require("./autonet_test_source.cjs");

if (!globalThis.crypto) globalThis.crypto = require("crypto").webcrypto;
// readyState 'loading' so the file registers a DOMContentLoaded listener
// instead of auto-booting at load — we drive one function ourselves.
globalThis.window = {};
globalThis.document = { readyState: "loading", addEventListener: () => {} };

const src = loadAutonetTestSource();
vm.runInThisContext(src);

const autonet = globalThis.window.autonet;
if (!autonet || typeof autonet.withTimeout !== "function"
    || typeof autonet.performHandshake !== "function") {
  console.error("FAIL: autonet did not load with withTimeout/performHandshake");
  process.exit(2);
}

// Opens (send is a no-op) but recvBinary never settles — SERVER_HELLO never
// arrives. This is the hang.
const silentTransport = {
  send() {},
  close() {},
  recvBinary: () => new Promise(() => {}),
};

const started = Date.now();
autonet.withTimeout(
  autonet.performHandshake(silentTransport, {
    org: "org-uuid", token: "a".repeat(32), rootPub: "b".repeat(64),
  }),
  150, "handshake",
).then(
  () => { console.error("FAIL: handshake resolved against a silent relay"); process.exit(1); },
  (err) => {
    const ms = Date.now() - started;
    if (/timed out establishing channel/.test(String(err && err.message)) && ms < 3000) {
      console.log("PASS: handshake bounded after", ms, "ms —", err.message);
      process.exit(0);
    }
    console.error("FAIL: unexpected rejection after", ms, "ms:", err && err.message);
    process.exit(1);
  },
);

// Backstop: if nothing settled well past the timeout, the wrap is broken.
setTimeout(() => { console.error("FAIL: probe never settled — still hanging"); process.exit(3); }, 5000);
