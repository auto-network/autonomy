/* Exercise the real phased serving maintenance and access-only passkey path.
 * Local signing uses real crypto. HTTP is a fixture which rejects any call
 * before root cleanup. Maintenance failure must not undo completed access.
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
let ceremonySeed = null;
globalThis.fetch = async (requestUrl, options) => {
  if (ceremonySeed && ceremonySeed.some(Boolean)) throw new Error('network before root cleanup');
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
  if (requestUrl === '/api/network/serve-cert') {
    repairCalls += 1;
    repairCalledAfterAccess = events.includes('POST /api/identity/unlock/password');
    return { ok: mode !== 'failure', json: async () => mode === 'failure'
      ? { ok: false, error: 'simulated maintenance failure' } : { ok: true } };
  }
  if (['/api/identity/unlock/vault-keys', '/api/network/unlock-report',
       '/api/identity/ceremony-error'].includes(requestUrl)) {
    return { ok: true, json: async () => ({ ok: true }) };
  }
  return { ok: false, status: 404, json: async () => ({}) };
};
window.fetch = globalThis.fetch;

let repairCalledAfterAccess = false;
let allRepairCalls = 0;
let allRepairOrgs = null;

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
    const phases = await import('../static/js/ceremony/signon-phases.js');
    const { deriveAuditedRecipient } = await import('../static/js/ceremony/vault-unlock.js');
    const { sealToEncapsulationKey } = await import('../static/js/ceremony/sealing.js');
    const { bytesToHex } = await import('../static/js/ceremony/primitives.js');
    await import('../static/js/network-signon.mjs');
    ceremonySeed = crypto.getRandomValues(new Uint8Array(32));
    const audited = await deriveAuditedRecipient(ceremonySeed);
    const orgs = process.env.AUTONOMY_ALL_ORG_REPAIR === '1'
      ? ['autonomy', 'dynbench', 'anchore'] : ['autonomy'];
    const inputs = {
      vault: { root_pub: 'aa'.repeat(32), recovery_genesis_id: null,
        inventory: { anchors: [], classes: [{ governance: { form: 'root-reachable' } }] } },
      runtime: { enabled: false }, completion: null, personal_serve: {},
      organizations: orgs.map((slug, i) => ({ slug,
        genesis_id: String(i + 1).repeat(64),
        org_uuid: '8a2d6c7a-498c-42ba-a4a6-b3b27a024bac',
        serve_cert: { required: true }, checkpoint_work: null,
        storage_delegate: { organization: slug, remint_below_ms: 30 * 86400000,
          delegate_metadata: { key_exists: true, expires_at: Date.now() + 90 * 86400000,
            key_reference: 'stored-' + slug } },
      })),
    };
    const encrypted = { sealed: bytesToHex(await sealToEncapsulationKey(
      new TextEncoder().encode(JSON.stringify(inputs)), audited.publicKeyHex,
      'autonomy/identity/sign-in-preparation/v1')) };
    let prepared;
    try {
      prepared = await phases.prepareSignon(ceremonySeed, encrypted, window.AutonomyNetworkSession);
    } finally { ceremonySeed.fill(0); }
    // Login proof and all maintenance are submitted only after cleanup.
    events.push("POST /api/identity/unlock/password");
    await phases.submitSignon(prepared, globalThis.fetch);
    allRepairOrgs = orgs;
    allRepairCalls = 1;
  }
  process.stdout.write(JSON.stringify({
    events,
    repair_called_after_access: repairCalledAfterAccess,
    repair_calls: repairCalls,
    all_repair_calls: allRepairCalls,
    all_repair_orgs: allRepairOrgs,
    serve_repair: window.__autonomyServeRepair || null,
  }));
  process.exit(0);
})().catch((error) => {
  console.error(error && error.stack || error);
  process.exit(1);
});
