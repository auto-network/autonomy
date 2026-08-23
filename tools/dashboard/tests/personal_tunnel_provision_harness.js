/* Runs the REAL network-signon.mjs provisionPersonalNetworkIdentity in Node
 * (WebCrypto) so a Python test can verify the personal-org registration
 * envelope + serving delegate with idkit — the "personal tunnel" ceremony that
 * brings a virgin system's own identity online as its own auto.network org.
 *
 * Shims just enough of the browser: crypto (webcrypto), a fetch that returns a
 * 'missing' serve-cert status (so a delegate is minted) and captures both the
 * register and serve-cert POST bodies, localStorage, window.
 *
 * env: AUTONOMY_SEED_HEX (32-byte personal root seed hex), AUTONOMY_ROOT_PUB,
 *      AUTONOMY_ORG_UUID.
 * stdout: { register: <POST body>, serve: <POST body>, serve_status_reads }.
 */
"use strict";

const path = require("path");
const url = require("url");

if (!globalThis.crypto) globalThis.crypto = require("crypto").webcrypto;
globalThis.window = {};
globalThis.localStorage = { getItem: () => null, setItem: () => {} };

const SEED_HEX = process.env.AUTONOMY_SEED_HEX;
const ROOT_PUB = process.env.AUTONOMY_ROOT_PUB;
const ORG_UUID = process.env.AUTONOMY_ORG_UUID;

function hexToBytes(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) {
    out[i] = parseInt(hex.substr(i * 2, 2), 16);
  }
  return out;
}

let registerBody = null;
let serveBody = null;
let serveStatusReads = 0;
globalThis.fetch = async (u, opts) => {
  opts = opts || {};
  if (u.indexOf("/api/network/register") !== -1) {
    registerBody = JSON.parse(opts.body);
    return { ok: true, json: async () => ({ ok: true, binding: {} }) };
  }
  if (u.indexOf("/api/network/serve-cert") !== -1) {
    if ((opts.method || "GET").toUpperCase() === "GET") {
      serveStatusReads += 1;
      return { ok: true, status: 200, json: async () => ({ status: "missing" }) };
    }
    serveBody = JSON.parse(opts.body);
    return { ok: true, json: async () => ({ ok: true }) };
  }
  return { ok: false, status: 404, json: async () => ({}) };
};
globalThis.window.fetch = globalThis.fetch;

(async () => {
  await import(url.pathToFileURL(
    path.join(__dirname, "..", "static", "js", "network-signon.mjs")).href);
  const session = globalThis.window.AutonomyNetworkSession;
  if (!session ||
      typeof session.provisionPersonalNetworkIdentity !== "function") {
    console.error(
      "network-signon.mjs did not expose provisionPersonalNetworkIdentity");
    process.exit(2);
  }
  await session.provisionPersonalNetworkIdentity({
    personalRootSeed: hexToBytes(SEED_HEX),
    orgUuid: ORG_UUID,
    rootPub: ROOT_PUB,
    serve: true,
  });
  if (!registerBody || !serveBody) {
    console.error("provisioning did not POST both register and serve-cert");
    process.exit(1);
  }
  // Force exit after the pipe flushes — the module's load-time _ready promise
  // (indexedDB probe) keeps the event loop alive otherwise.
  process.stdout.write(JSON.stringify({
    register: registerBody, serve: serveBody,
    serve_status_reads: serveStatusReads,
  }), () => process.exit(0));
})().catch((e) => {
  console.error("harness error:", (e && e.message) || e);
  process.exit(3);
});
