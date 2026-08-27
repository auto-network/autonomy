/* The unlock page's REAL login flow in jsdom — the trigger coverage that was
 * missing while three wiring defects shipped in this file.
 *
 * Drives the actual tools/dashboard/static/js/unlock.js (one mechanical
 * transform only: `import(` → `__dynImport(`, because jsdom cannot evaluate
 * dynamic import inside classic scripts; __dynImport resolves the same
 * specifiers against the same directory, so the same real ceremony modules
 * load). The fixture is the operator's exact post-migration state: a v3
 * armor whose password holds individual root authority and whose passkey is
 * ENROLLED (credential registered) with ZERO device slots and NO root
 * authority. The virtual authenticator holds that credential with its own
 * PRF secret — the new-device situation.
 *
 * Proven end-to-end from the login button:
 *   1. the WebAuthn get() REQUESTS the PRF evaluation in the login gesture;
 *   2. the access login POST succeeds;
 *   3. detection runs EVEN THOUGH no passkey holds root authority (the gate
 *      regression the operator hit live), and the pending-slot stash is
 *      written with the correctly derived recipient public key — the exact
 *      payload the factor panel's first-device dialog consumes.
 *
 *   node --test tools/dashboard/tests/unlock_login.test.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';

const here = path.dirname(fileURLToPath(import.meta.url));
const { JSDOM } = createRequire(import.meta.url)('jsdom');

import { VirtualAuthenticator } from '../static/js/ceremony/authenticator-node.js';
import { deriveEncapsulationKeypair, bytesToHex } from '../static/js/ceremony/primitives.js';
import { prfOutputFromResults, prfEvalExtension } from '../static/js/ceremony/enrollment.js';
import {
  canonicalExpression, createPasswordFactor, buildFactorPolicyArmor,
  FACTOR_RECIPIENT_PURPOSE,
} from '../static/js/ceremony/root-factor-policy.js';

const RP = 'localhost';
const ORIGIN = 'https://localhost/';
const JS_DIR = path.join(here, '..', 'static', 'js');

function b64uToBytes(s) {
  let b = s.replace(/-/g, '+').replace(/_/g, '/'); while (b.length % 4) b += '=';
  const bin = atob(b); const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
  return out;
}
function bytesToB64u(bytes) {
  const v = new Uint8Array(bytes); let bin = '';
  for (let i = 0; i < v.length; i += 1) bin += String.fromCharCode(v[i]);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}
function toBuf(x) {
  const b = typeof x === 'string' ? b64uToBytes(x) : new Uint8Array(x);
  return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength);
}

// ── fixture: the operator's live post-migration identity ───────────────────
const authenticator = new VirtualAuthenticator();
const SERVER = { armor: null, state: null, credId: null, posts: [], getRequests: [] };

async function buildFixture() {
  const kp = await crypto.subtle.generateKey('Ed25519', true, ['sign', 'verify']);
  const rootSeed = new Uint8Array(await crypto.subtle.exportKey('pkcs8', kp.privateKey)).slice(-32);
  const rootPub = bytesToHex(new Uint8Array(await crypto.subtle.exportKey('raw', kp.publicKey)));
  const pw = await createPasswordFactor(rootPub, 'legacy.password', 'unused-here', 10000);
  pw.seed.fill(0);
  const cred = await authenticator.createCredential({
    rpId: RP, origin: ORIGIN, challenge: 'c0', prf: prfEvalExtension().prf, evalAtCreate: true,
  });
  SERVER.credId = cred.rawId;
  const pk = {
    factor_id: 'pk.mac', type: 'passkey', credential_id: cred.rawId, recipients: [],
  };
  SERVER.state = {
    generation: 1,
    factors: [pw.factor, pk].sort((a, b) => a.factor_id.localeCompare(b.factor_id)),
    access: ['legacy.password', 'pk.mac'],
    policy: canonicalExpression({ op: 'factor', factor_id: 'legacy.password' }),
  };
  SERVER.armor = await buildFactorPolicyArmor({
    rootSeed, rootPub, generation: 1,
    factors: SERVER.state.factors, access: SERVER.state.access, policy: SERVER.state.policy,
  });
  SERVER.rootPub = rootPub;
}

function view() {
  return {
    version: 1, armor_version: 3, generation: 1, root_pub: SERVER.rootPub,
    root_policy: SERVER.state.policy, migration_required: false,
    factors: SERVER.state.factors.map((f) => (f.type === 'password'
      ? {
        factor_id: f.factor_id, type: 'password', label: 'Password', access: 'enabled',
        root_role: 'individual', kdf: { name: 'PBKDF2', hash: 'SHA-256', iterations: 10000 },
      }
      : {
        factor_id: f.factor_id, type: 'passkey', label: 'This device', access: 'enabled',
        root_role: 'none', credential_id: f.credential_id, rp_id: RP,
        transports: ['internal'], created_at: '2026-08-01T00:00:00Z',
        backup_eligible: true, backed_up: true, recipients: f.recipients,
      })),
  };
}

const ok = (data) => ({ ok: true, status: 200, json: async () => data });
async function router(url, opts) {
  const u = String(url);
  const body = opts && opts.body ? JSON.parse(opts.body) : null;
  if (u.includes('/api/identity/status')) {
    return ok({
      personal_identity: { display_name: 'Jeremy Spilman' },
      rp_id: RP,
      passkeys_for_host: 1,
      passkeys: [{ credential_id: SERVER.credId, rp_id: RP, transports: ['internal'], label: 'This device' }],
    });
  }
  if (u.includes('/api/identity/personal')) return ok({ armored_private_key: SERVER.armor });
  if (u.includes('/api/identity/factor-policy')) return ok(view());
  if (u.includes('/unlock/passkey/options')) {
    return ok({
      ok: true,
      options: {
        challenge: bytesToB64u(crypto.getRandomValues(new Uint8Array(16))),
        allowCredentials: [{ type: 'public-key', id: SERVER.credId }],
        rpId: RP,
      },
    });
  }
  if (u.includes('/unlock/passkey')) { SERVER.posts.push(body); return ok({ ok: true }); }
  if (u.includes('/unlock/vault-keys') || u.includes('/api/session')) return ok({ ok: true });
  return ok({ ok: false, error: 'unrouted: ' + u });
}

function browserCredential(sim) {
  return {
    id: sim.id,
    rawId: toBuf(sim.rawId),
    type: sim.type,
    authenticatorAttachment: sim.authenticatorAttachment,
    getClientExtensionResults() {
      const out = JSON.parse(JSON.stringify(sim.clientExtensionResults || {}));
      if (out.prf && out.prf.results && typeof out.prf.results.first === 'string') {
        out.prf.results.first = toBuf(out.prf.results.first);
      }
      return out;
    },
    response: {
      clientDataJSON: toBuf(sim.response.clientDataJSON),
      authenticatorData: toBuf(sim.response.authenticatorData),
      signature: toBuf(sim.response.signature),
      userHandle: null,
    },
  };
}

// ── the harness: run the REAL unlock.js against a jsdom window ─────────────
async function bootUnlock(sourcePath, { search = '' } = {}) {
  const dom = new JSDOM(
    '<!doctype html><html><body><div class="unlock-shell"><div id="unlock-card" class="unlock-card"></div></div></body></html>',
    { url: 'https://localhost/unlock' + search, pretendToBeVisual: true },
  );
  const win = dom.window;
  const credentials = {
    async get({ publicKey }) {
      SERVER.getRequests.push({
        prfRequested: !!(publicKey.extensions && publicKey.extensions.prf),
        allow: (publicKey.allowCredentials || []).length,
      });
      const sim = await authenticator.getAssertion({
        rpId: RP, origin: ORIGIN,
        challenge: bytesToB64u(publicKey.challenge),
        credentialId: bytesToB64u(publicKey.allowCredentials[0].id),
        prf: publicKey.extensions && publicKey.extensions.prf,
      });
      return browserCredential(sim);
    },
  };
  Object.defineProperty(win.navigator, 'credentials', { value: credentials, configurable: true });
  win.PublicKeyCredential = function PublicKeyCredential() {};
  win.AutonomyNetworkSession = { _internals: {} };
  win.AutonomyNetworkIdentity = { _internals: {} };
  win.fetch = router;

  let src = fs.readFileSync(sourcePath, 'utf8');
  // jsdom cannot evaluate dynamic import in classic scripts; route the same
  // specifiers to the same real modules through the test's ESM loader
  src = src.replace(/\bimport\(/g, '__dynImport(');
  const dynImport = (spec) => import(pathToFileURL(path.join(JS_DIR, spec)).href);
  const nav = { target: null };
  const location = {
    href: 'https://localhost/unlock' + search,
    search,
    assign(t) { nav.target = t; },
    replace(t) { nav.target = t; },
  };
  const run = new Function(
    'window', 'document', 'navigator', 'sessionStorage', 'fetch', 'crypto',
    'location', '__dynImport', 'PublicKeyCredential',
    src,
  );
  run(
    win, win.document, win.navigator, win.sessionStorage, router, crypto,
    location, dynImport, win.PublicKeyCredential,
  );
  return { dom, win, nav };
}

const flush = () => new Promise((r) => setImmediate(r));
async function until(fn, what = 'condition', ms = 15000) {
  const t0 = Date.now();
  for (;;) {
    const v = fn(); if (v) return v;
    if (Date.now() - t0 > ms) throw new Error('timed out waiting for ' + what);
    await flush();
  }
}

test('passkey login on a slotless (post-migration) device: PRF requested, access granted, pending slot stashed', async () => {
  await buildFixture();
  // UNLOCK_JS overrides the target source — used to prove this harness turns
  // red against the gate regression that shipped (see the fix commit)
  const { win } = await bootUnlock(process.env.UNLOCK_JS || path.join(JS_DIR, 'unlock.js'));
  const card = win.document.getElementById('unlock-card');
  const button = await until(
    () => card.querySelector('#unlock-primary'),
    'unlock button rendered',
  );
  assert.match(button.textContent, /Face|passkey/i, 'the primary unlock is the passkey ceremony');
  button.click();
  await until(() => SERVER.posts.length >= 1, 'access login POST');

  // 1. the login gesture requested the PRF evaluation
  assert.equal(SERVER.getRequests.length, 1, 'one WebAuthn assertion');
  assert.equal(SERVER.getRequests[0].prfRequested, true, 'PRF requested in the login get()');

  // 2. the access login succeeded (server saw the assertion)
  assert.ok(SERVER.posts[0].credential, 'assertion posted');

  // 3. detection ran DESPITE the passkey holding no root authority, and the
  //    stash carries the correctly derived recipient for THIS device's PRF
  const raw = await until(
    () => win.sessionStorage.getItem('autonomy.factor.pending-slot'),
    'pending-slot stash written',
  );
  const pending = JSON.parse(raw);
  assert.equal(pending.factor_id, 'pk.mac');
  assert.equal(pending.credential_id, SERVER.credId);
  const asrt = await authenticator.getAssertion({
    rpId: RP, origin: ORIGIN, challenge: 'verify', credentialId: SERVER.credId,
    prf: prfEvalExtension().prf,
  });
  const prf = new Uint8Array(prfOutputFromResults(asrt.clientExtensionResults));
  const expected = (await deriveEncapsulationKeypair(prf, FACTOR_RECIPIENT_PURPOSE)).publicKeyHex;
  assert.equal(pending.recipient_public_key, expected,
    'stash holds the v3 recipient derived from this device\'s PRF');
  assert.equal(pending.label, 'New device');
});

test('a login that detects a pending enrollment lands on the shell home, never the last session', async () => {
  SERVER.posts.length = 0; SERVER.getRequests.length = 0;
  await buildFixture();
  const { win, nav } = await bootUnlock(process.env.UNLOCK_JS || path.join(JS_DIR, 'unlock.js'),
    { search: '?next=/session/auto-1234' });
  const card = win.document.getElementById('unlock-card');
  const button = await until(() => card.querySelector('#unlock-primary'), 'unlock button rendered');
  button.click();
  await until(() => win.sessionStorage.getItem('autonomy.factor.pending-slot'), 'pending-slot stash');
  await until(() => nav.target !== null, 'post-login navigation');
  // the first-device dialog lives in the shell's profile drawer — an immersive
  // session surface has no profile control, so the greeting could never show
  assert.equal(nav.target, '/', 'redirect suppressed in favor of the shell home');
});
