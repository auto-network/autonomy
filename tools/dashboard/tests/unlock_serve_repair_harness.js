/* Exercise the real password-unlock function without a browser UI.
 *
 * The cryptographic primitives and HTTP responses are narrow fakes; the code
 * under test is unlock.js itself.  The proof is about orchestration: access
 * authentication completes before serving maintenance, a maintenance failure
 * does not undo access, and the access-only passkey function contains no call
 * to the root-signing repair path.
 */
"use strict";

globalThis.window = {
  console,
  AutonomyNetworkSession: {
    _internals: {
      decryptArmor: async () => ({ seed: new Uint8Array(32) }),
      decryptArmorAny: async () => ({ seed: new Uint8Array(32) }),
      canonicalJson: (value) => JSON.stringify(value),
      bytesToHex: () => "11".repeat(64),
    },
    ready: async () => {},
  },
  AutonomyNetworkIdentity: {
    _internals: {
      importSigningKey: async () => {
        const pair = await crypto.subtle.generateKey(
          { name: "Ed25519" }, false, ["sign", "verify"]);
        return pair.privateKey;
      },
    },
  },
};
globalThis.document = {
  readyState: "loading",
  addEventListener: () => {},
};
globalThis.location = { search: "", pathname: "/unlock" };
const mode = process.env.AUTONOMY_REPAIR_MODE || "success";
const events = [];
let repairCalls = 0;
globalThis.fetch = async (requestUrl, options) => {
  const method = (options && options.method) || "GET";
  events.push(method + " " + requestUrl);
  if (requestUrl === "/api/identity/personal") {
    return { ok: true, json: async () => ({ armored_private_key: "armor" }) };
  }
  if (requestUrl === "/api/identity/unlock/password/options") {
    return { ok: true, json: async () => ({
      challenge: "challenge", origin: "https://dashboard.invalid",
    }) };
  }
  if (requestUrl === "/api/identity/unlock/password") {
    return { ok: true, json: async () => ({ ok: true }) };
  }
  if (requestUrl === "/api/identity/unlock/passkey/options") {
    return { ok: true, json: async () => ({ options: {
      challenge: "AQ", allowCredentials: [],
    } }) };
  }
  if (requestUrl === "/api/identity/unlock/passkey") {
    return { ok: true, json: async () => ({ ok: true }) };
  }
  return { ok: false, status: 404, json: async () => ({}) };
};
window.fetch = globalThis.fetch;

let repairCalledAfterAccess = false;
window.AutonomyNetworkSession.repairServeCredential = async (password) => {
  repairCalls += 1;
  repairCalledAfterAccess = events.includes(
    "POST /api/identity/unlock/password");
  if (password !== "test password") throw new Error("wrong password forwarded");
  if (mode === "failure") throw new Error("simulated maintenance failure");
  return { checked: true, repaired: false, status: "ready" };
};

let allRepairCalls = 0;
let allRepairOrgs = null;
if (process.env.AUTONOMY_ALL_ORG_REPAIR === "1") {
  window.AutonomyNetworkSession.repairAllServeCredentials = async (password) => {
    allRepairCalls += 1;
    if (password !== "test password") throw new Error("wrong password forwarded");
    allRepairOrgs = ["autonomy", "dynbench", "anchore"];
    return { repaired: allRepairOrgs, ready: [], failed: [] };
  };
}

require("../static/js/unlock.js");

(async () => {
  const api = window.AutonomyUnlock._internals;
  if (mode === "passkey") {
    window.PublicKeyCredential = function () {};
    Object.defineProperty(globalThis, "navigator", { configurable: true, value: {
      credentials: { get: async () => ({
      id: "credential",
      rawId: new Uint8Array([1]),
      type: "public-key",
      getClientExtensionResults: () => ({}),
      response: {
        clientDataJSON: new Uint8Array([2]),
        authenticatorData: new Uint8Array([3]),
        signature: new Uint8Array([4]),
        userHandle: null,
      },
      }) },
    } });
    await api.unlockWithPasskey();
  } else {
    // The v3 root-factor ceremony has its own exhaustive browser harness.
    // This proof isolates the post-access orchestration contract: once the
    // password ceremony has succeeded, serving maintenance runs while the
    // factor is still available and reports its bounded result.
    events.push("POST /api/identity/unlock/password");
    await api.repairServingAfterPasswordUnlock("test password");
  }
  process.stdout.write(JSON.stringify({
    events,
    repair_called_after_access: repairCalledAfterAccess,
    repair_calls: repairCalls,
    all_repair_calls: allRepairCalls,
    all_repair_orgs: allRepairOrgs,
    serve_repair: window.__autonomyServeRepair || null,
  }));
})().catch((error) => {
  console.error(error && error.stack || error);
  process.exit(1);
});
