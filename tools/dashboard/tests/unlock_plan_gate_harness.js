/* Drive the REAL root-ceremony step runner against a set of unlock plans.
 *
 * The code under test is network-signon.mjs's `_ORG_ROOT_STEPS` +
 * `_reconcileOrgUnderRoot`. Nothing here fakes the gating: the harness hands
 * the runner a plan and records every URL the ceremony actually fetches, so
 * the proof is about what the ceremony DOES — which steps it attempts, and
 * what it declines to attempt without asking the server.
 */
"use strict";

const events = [];

globalThis.window = {
  console,
  fetch: async (url, options) => {
    const method = (options && options.method) || "GET";
    events.push(method + " " + String(url).split("?")[0]);
    if (String(url).startsWith("/api/network/serve-cert")) {
      // Enough to end the serve-cert step right after its probe: the point is
      // whether the step ran at all, not what it mints.
      return { ok: true, status: 200, json: async () => ({ required: false, status: "ok" }) };
    }
    if (String(url).startsWith("/api/network/membership-checkpoint/decision")) {
      return { ok: true, status: 200, json: async () => ({ ok: true, action: "up-to-date" }) };
    }
    if (String(url).startsWith("/api/network/rekey-policy")) {
      return { ok: false, status: 404, json: async () => ({}) };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  },
};
globalThis.fetch = globalThis.window.fetch;
globalThis.indexedDB = undefined;

const MINE = "aa".repeat(32);
const OTHER = "bb".repeat(32);

function ctxFor(plan) {
  return {
    slug: "acme",
    binding: {
      org_uuid: "11111111-1111-1111-1111-111111111111",
      root_pub: "cc".repeat(32),
      registry_url: "https://registry.invalid",
      // Far from expiry, so the binding step is a pure local no-op and any
      // fetch recorded below came from a step that was NOT supposed to run.
      binding_expires_at: "2099-01-01T00:00:00Z",
    },
    heads: { genesis_id: "dd".repeat(32) },
    genesisId: "dd".repeat(32),
    persona: { publicHex: MINE },
    seed: new Uint8Array(32),
    plan: plan,
    deriveSeed: async () => ({ publicHex: MINE, signingKey: null }),
  };
}

const PLANS = {
  // Nothing to do: a committed org already up to date, credential current.
  nothing_to_do: {
    committed_membership_org: true,
    serve_cert: { required: false, status: "ok", days_remaining: 25.0 },
    checkpoint: { needed: false, checkpointer_pubs: [MINE] },
    rekey: { due: false, reason: "no-interval-configured" },
  },
  // A local/personal store: no committed membership at all.
  personal: {
    committed_membership_org: false,
    serve_cert: { required: false, status: "ok", days_remaining: 25.0 },
    checkpoint: { needed: false, checkpointer_pubs: [] },
    rekey: { due: false, reason: "no-interval-configured" },
  },
  // A checkpoint IS due, but this persona holds no checkpoint scope.
  not_checkpointer: {
    committed_membership_org: true,
    serve_cert: { required: false, status: "ok", days_remaining: 25.0 },
    checkpoint: { needed: true, checkpointer_pubs: [OTHER] },
    rekey: { due: false, reason: "no-interval-configured" },
  },
  // Due, and this persona may publish it.
  checkpoint_due: {
    committed_membership_org: true,
    serve_cert: { required: false, status: "ok", days_remaining: 25.0 },
    checkpoint: { needed: true, checkpointer_pubs: [MINE, OTHER] },
    rekey: { due: false, reason: "no-interval-configured" },
  },
  // A credential inside its renewal window: still 'ok', still required.
  serve_due: {
    committed_membership_org: true,
    serve_cert: { required: true, status: "ok", days_remaining: 9.0 },
    checkpoint: { needed: false, checkpointer_pubs: [MINE] },
    rekey: { due: false, reason: "no-interval-configured" },
  },
};

(async () => {
  await import("../static/js/network-signon.mjs");
  const internals = globalThis.window.AutonomyNetworkSession._internals;

  const proof = {};
  for (const name of Object.keys(PLANS).concat(["no_plan"])) {
    events.length = 0;
    const plan = name === "no_plan" ? null : PLANS[name];
    const steps = await internals.reconcileOrgUnderRoot(ctxFor(plan));
    proof[name] = {
      fetched: events.slice(),
      steps: steps.map((s) => ({
        step: s.step, ok: s.ok, skipped: !!s.skipped, reason: s.reason || null,
      })),
    };
  }
  console.log(JSON.stringify(proof));
  // The signer module keeps handles open in a browser; exit explicitly rather
  // than waiting for an event loop that has no reason to drain under node.
  process.exit(0);
})().catch((e) => {
  console.error(e && e.stack || String(e));
  process.exit(1);
});
