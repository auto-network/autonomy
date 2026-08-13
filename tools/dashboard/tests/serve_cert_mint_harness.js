/* Runs the REAL network-signon.js provisionServeCert in Node (WebCrypto) so a
 * Python test can verify the minted cert with idkit. Shims just enough of the
 * browser: crypto (webcrypto), a fetch that feeds a Python-generated org armor
 * and captures the serve-cert POST body, localStorage, window.
 *
 * env: AUTONOMY_ARMOR (PEM org armor), AUTONOMY_PERSONAL_ARMOR,
 * AUTONOMY_GENESIS_ID, AUTONOMY_PW, AUTONOMY_ORG_UUID.
 * stdout: the captured POST body {org, cert, private_key} as JSON.
 */
"use strict";

const path = require("path");
const url = require("url");

if (!globalThis.crypto) globalThis.crypto = require("crypto").webcrypto;
globalThis.window = {};
globalThis.localStorage = { getItem: () => null, setItem: () => {} };

const ARMOR = process.env.AUTONOMY_ARMOR;
const PW = process.env.AUTONOMY_PW;
const ORG_UUID = process.env.AUTONOMY_ORG_UUID;
const ROOT_PUB = process.env.AUTONOMY_ROOT_PUB;
const MODE = process.env.AUTONOMY_MODE || "explicit";

let captured = null;
globalThis.fetch = async (url, opts) => {
  opts = opts || {};
  if (url.indexOf("/api/network/org-key") !== -1) {
    return { ok: true, json: async () => ({
      armored_private_key: ARMOR, root_pub: ROOT_PUB,
    }) };
  }
  if (url.indexOf("/api/network/binding") !== -1) {
    return { ok: true, status: 200, json: async () => ({
      org_uuid: ORG_UUID,
      root_pub: ROOT_PUB,
      registry_url: "https://registry.invalid",
    }) };
  }
  if (url.indexOf("/api/network/ledger/heads") !== -1) {
    return { ok: true, status: 200,
      json: async () => ({ genesis_id: process.env.AUTONOMY_GENESIS_ID }) };
  }
  if (url.indexOf("/api/identity/personal") !== -1) {
    return { ok: true, status: 200,
      json: async () => ({
        armored_private_key: process.env.AUTONOMY_PERSONAL_ARMOR,
      }) };
  }
  if (url.indexOf("/api/network/serve-cert") !== -1) {
    if ((opts.method || "GET").toUpperCase() === "GET") {
      return { ok: true, status: 200,
        json: async () => ({ required: true, status: "missing" }) };
    }
    captured = JSON.parse(opts.body);
    return { ok: true, json: async () => ({ ok: true }) };
  }
  return { ok: false, status: 404, json: async () => ({}) };
};
globalThis.window.fetch = globalThis.fetch;

(async () => {
  await import(url.pathToFileURL(
    path.join(__dirname, "..", "static", "js", "network-signon.mjs")).href);
  const session = globalThis.window.AutonomyNetworkSession;
  if (!session || typeof session.provisionServeCert !== "function") {
    console.error("network-signon.js did not expose provisionServeCert");
    process.exit(2);
  }
  if (MODE === "signon") {
    let stored = null;
    await session.configure({
      storage: {
        getSession: async () => stored,
        putSession: async (value) => { stored = value; },
        clearSession: async () => { stored = null; },
        getSubjectId: async () => null,
        setSubjectId: async () => {},
      },
      transport: { fetch: globalThis.fetch },
    });
    await session.signOn(PW, { org: null });
  } else {
    await session.provisionServeCert(PW, { org: null, orgUuid: ORG_UUID });
  }
  if (!captured) {
    console.error("provisionServeCert did not POST a serve-cert");
    process.exit(1);
  }
  // Force exit after the pipe flushes — the module's load-time _ready promise
  // (indexedDB probe) keeps the event loop alive otherwise.
  process.stdout.write(JSON.stringify(captured), () => process.exit(0));
})().catch((e) => {
  console.error("harness error:", (e && e.message) || e);
  process.exit(3);
});
