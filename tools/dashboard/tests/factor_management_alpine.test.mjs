/* JSDOM integration smoke for the Manage-credentials panel (the design's
 * Alpine component, verbatim, with real hooks).
 *
 * Drives the REAL rendered panel — vendored Alpine 3.15.12 in jsdom — against
 * REAL crypto: a v3 factor-policy armor built with buildFactorPolicyArmor, a
 * virtual WebAuthn authenticator (ceremony/authenticator-node.js) behind a
 * navigator.credentials adapter, and the real ceremony modules under every
 * handler. The server is a thin in-process stub that mirrors the
 * preview/commit contract; each committed armor is PROVEN by opening it with
 * the factors the target state must accept and refusing the ones it must not.
 * (The exhaustive transition matrix against the real Python backend is the
 * follow-up suite; this file is the fast regression gate.)
 *
 *   node --test tools/dashboard/tests/factor_management_alpine.test.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const here = path.dirname(fileURLToPath(import.meta.url));
const { JSDOM } = createRequire(import.meta.url)('jsdom');

import { VirtualAuthenticator } from '../static/js/ceremony/authenticator-node.js';
import { deriveEncapsulationKeypair, bytesToHex } from '../static/js/ceremony/primitives.js';
import { prfOutputFromResults, prfEvalExtension } from '../static/js/ceremony/enrollment.js';
import {
  FACTOR_RECIPIENT_PURPOSE, canonicalExpression, createPasswordFactor,
  openPasswordFactor, parseFactorPolicyArmor, buildFactorPolicyArmor,
  openFactorPolicyArmor,
} from '../static/js/ceremony/root-factor-policy.js';

const PW = 'correct-horse-battery';
const PW2 = 'new-armor-password-9';
const IT = 10000;
const RP = 'localhost';
const ORIGIN = 'https://localhost/';

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

async function mintRoot() {
  const kp = await crypto.subtle.generateKey('Ed25519', true, ['sign', 'verify']);
  const pk8 = new Uint8Array(await crypto.subtle.exportKey('pkcs8', kp.privateKey));
  const rawPub = new Uint8Array(await crypto.subtle.exportKey('raw', kp.publicKey));
  return { seed: pk8.slice(-32), rootPub: bytesToHex(rawPub) };
}

// ── the in-process server (mirrors the preview/commit contract) ────────────
const SERVER = {
  root: null,
  state: null,        // { generation, factors, access, policy }
  armor: null,
  passkeyRows: [],    // dashboard passkey registrations
  commits: [],
};

function applyOps(state, operations) {
  const factors = new Map(state.factors.map((f) => [f.factor_id, JSON.parse(JSON.stringify(f))]));
  const access = new Set(state.access);
  let policy = state.policy;
  for (const op of operations) {
    if (op.op === 'enroll_password' || op.op === 'enroll_passkey') {
      if (factors.has(op.factor.factor_id)) throw new Error('already enrolled');
      factors.set(op.factor.factor_id, op.factor);
      if (op.access) access.add(op.factor.factor_id);
    } else if (op.op === 'change_password') {
      factors.set(op.factor_id, op.factor);
    } else if (op.op === 'add_passkey_recipient') {
      factors.get(op.factor_id).recipients.push(op.recipient);
    } else if (op.op === 'remove_passkey_recipient') {
      const f = factors.get(op.factor_id);
      f.recipients = f.recipients.filter((r) => r.recipient_public_key !== op.recipient_public_key);
    } else if (op.op === 'remove_factor') {
      factors.delete(op.factor_id); access.delete(op.factor_id);
    } else if (op.op === 'set_access') {
      if (op.enabled) access.add(op.factor_id); else access.delete(op.factor_id);
    } else if (op.op === 'set_root_policy') {
      policy = canonicalExpression(op.policy);
    } else throw new Error('unknown op ' + op.op);
  }
  return {
    generation: state.generation + 1,
    factors: [...factors.values()].sort((a, b) => a.factor_id.localeCompare(b.factor_id)),
    access: [...access].sort(),
    policy,
  };
}

function viewFrom(state) {
  const memberIds = new Set((function walk(n, acc) {
    if (n.op === 'factor') acc.push(n.factor_id);
    else n.children.forEach((c) => walk(c, acc));
    return acc;
  }(state.policy, [])));
  const anyOne = state.policy.op !== 'and';
  return {
    version: 1,
    armor_version: 3,
    generation: state.generation,
    root_pub: SERVER.root.rootPub,
    root_policy: state.policy,
    migration_required: false,
    factors: state.factors.map((f) => {
      const member = memberIds.has(f.factor_id);
      const row = {
        factor_id: f.factor_id,
        type: f.type,
        label: f.type === 'password' ? 'Password' : 'Passkey',
        purpose: null,
        access: state.access.includes(f.factor_id) ? 'enabled' : 'disabled',
        root_role: member ? (anyOne ? 'individual' : 'mfa-member') : 'none',
        capabilities: {},
      };
      if (f.type === 'password') {
        row.kdf = { name: 'PBKDF2', hash: 'SHA-256', iterations: f.protector.kdf.iterations };
      } else {
        const reg = SERVER.passkeyRows.find((p) => p.credential_id === f.credential_id) || {};
        Object.assign(row, {
          credential_id: f.credential_id,
          rp_id: RP,
          transports: reg.transports || ['internal'],
          created_at: '2026-08-01T00:00:00Z',
          backup_eligible: !!reg.backed_up,
          backed_up: !!reg.backed_up,
          recipients: f.recipients,
        });
      }
      return row;
    }),
  };
}

function jsonResponse(data) { return { ok: true, json: async () => data }; }

async function router(url, opts) {
  const u = String(url);
  const body = opts && opts.body ? JSON.parse(opts.body) : null;
  if (u.includes('/api/identity/status')) {
    return jsonResponse({
      rp_id: RP,
      personal_identity: { display_name: 'Jeremy Spilman' },
      passkeys: SERVER.passkeyRows,
    });
  }
  if (u.includes('/api/identity/personal')) {
    return jsonResponse({ armored_private_key: SERVER.armor });
  }
  if (u.includes('/factor-policy/preview')) {
    try {
      if (body.base_generation !== SERVER.state.generation) {
        return jsonResponse({ ok: false, error: 'factor policy changed while you were editing' });
      }
      const p = applyOps(SERVER.state, body.operations);
      return jsonResponse({
        ok: true, base_generation: SERVER.state.generation, generation: p.generation,
        root_policy: p.policy, factors: p.factors, access: p.access, change_count: 1,
      });
    } catch (e) { return jsonResponse({ ok: false, error: e.message }); }
  }
  if (u.includes('/factor-policy/commit')) {
    try {
      if (body.base_generation !== SERVER.state.generation) {
        return jsonResponse({ ok: false, error: 'stale generation' });
      }
      const p = applyOps(SERVER.state, body.operations);
      const cand = await parseFactorPolicyArmor(body.candidate_armor);
      assert.equal(cand.generation, p.generation, 'candidate encodes the projected generation');
      assert.equal(JSON.stringify(cand.policy), JSON.stringify(canonicalExpression(p.policy)),
        'candidate encodes the projected policy');
      SERVER.state = p;
      SERVER.armor = body.candidate_armor;
      SERVER.commits.push({ operations: body.operations, armor: body.candidate_armor });
      return jsonResponse({ ok: true, ...viewFrom(SERVER.state) });
    } catch (e) { return jsonResponse({ ok: false, error: e.message }); }
  }
  if (u.includes('/api/identity/factor-policy')) {
    return jsonResponse(viewFrom(SERVER.state));
  }
  if (u.includes('/metadata')) return jsonResponse({ ok: true });
  if (u.includes('/passkey/register-options')) {
    return jsonResponse({
      ok: true, rp_id: RP, origin: ORIGIN, nonce: 'n-' + SERVER.passkeyRows.length,
      options: {
        challenge: bytesToB64u(crypto.getRandomValues(new Uint8Array(16))),
        user: { id: bytesToB64u(new Uint8Array(8)) },
        excludeCredentials: SERVER.passkeyRows.map((p) => ({ id: p.credential_id })),
      },
    });
  }
  if (u.includes('/passkey/register')) {
    SERVER.passkeyRows.push({
      credential_id: body.credential.rawId, rp_id: RP,
      transports: body.credential.response.transports, label: body.label,
    });
    return jsonResponse({ ok: true });
  }
  if (u.includes('/ceremony-error')) return jsonResponse({ ok: true });
  throw new Error('unrouted fetch: ' + u);
}

// ── navigator.credentials adapter over the virtual authenticator ───────────
// One VirtualAuthenticator = one device. The adapter always talks to the
// CURRENT device, so a test can move the "browser" between devices — the
// iCloud case: the same credential synced to two devices, each with its OWN
// PRF secret (the operator's iPhone/MacBook reality).
const authenticator = new VirtualAuthenticator();
let currentDevice = authenticator;
function switchDevice(auth) { currentDevice = auth; }
function cloneCredentialToDevice(fromAuth, credIdB64u) {
  const src = fromAuth.credentials.get(credIdB64u);
  const dev = new VirtualAuthenticator();
  dev.credentials.set(credIdB64u, {
    privateKey: src.privateKey,               // the credential syncs…
    hmacSecret: crypto.getRandomValues(new Uint8Array(32)),   // …its PRF does not
    signCount: 0,
    rpId: src.rpId,
  });
  return dev;
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
      ...(sim.response.attestationObject
        ? { attestationObject: toBuf(sim.response.attestationObject) } : {}),
      ...(sim.response.authenticatorData && typeof sim.response.authenticatorData === 'string'
        ? { authenticatorData: toBuf(sim.response.authenticatorData) } : {}),
      ...(sim.response.getAuthenticatorData
        ? { getAuthenticatorData: () => toBuf(sim.response.getAuthenticatorData()) } : {}),
      getTransports: () => ['internal'],
      ...(sim.response.signature ? { signature: toBuf(sim.response.signature) } : {}),
    },
  };
}

function installWebAuthn(win) {
  win.PublicKeyCredential = function PublicKeyCredential() {};
  const credentials = {
    async create({ publicKey }) {
      for (const ex of publicKey.excludeCredentials || []) {
        const id = typeof ex.id === 'string' ? ex.id : bytesToB64u(ex.id);
        if (currentDevice.credentials.has(id)) {
          const err = new Error('credential already registered');
          err.name = 'InvalidStateError';
          throw err;
        }
      }
      const sim = await currentDevice.createCredential({
        rpId: RP,
        origin: ORIGIN,
        challenge: bytesToB64u(publicKey.challenge),
        prf: publicKey.extensions && publicKey.extensions.prf,
        evalAtCreate: true,
      });
      return browserCredential(sim);
    },
    async get({ publicKey }) {
      const allow = publicKey.allowCredentials || [];
      let chosen = null;
      for (const c of allow) {
        const id = typeof c.id === 'string' ? c.id : bytesToB64u(c.id);
        if (currentDevice.credentials.has(id)) { chosen = id; break; }
      }
      if (!chosen) { const e = new Error('no credential'); e.name = 'NotAllowedError'; throw e; }
      const sim = await currentDevice.getAssertion({
        rpId: RP,
        origin: ORIGIN,
        challenge: bytesToB64u(publicKey.challenge),
        credentialId: chosen,
        prf: publicKey.extensions && publicKey.extensions.prf,
      });
      return browserCredential(sim);
    },
  };
  Object.defineProperty(win.navigator, 'credentials', { value: credentials, configurable: true });
}

// ── boot: jsdom + Alpine + the panel ───────────────────────────────────────
let panel;
let dom;
// No wall-clock sleeps: poll on event-loop turns (setImmediate), so a wait
// resolves the same turn its condition lands and only real work (crypto,
// promise chains) spends time. Time-bounded + loud so a dead wait fails at
// its own line instead of surfacing as a null three lines later.
const flush = () => new Promise((r) => setImmediate(r));
async function until(fn, what = 'condition', ms = 15000) {
  const t0 = Date.now();
  for (;;) {
    const v = fn(); if (v) return v;
    if (Date.now() - t0 > ms) throw new Error('timed out waiting for ' + what);
    await flush();
  }
}
function root() { return document.querySelector('.fui-cred'); }
function q(sel) { const r = root(); return r ? r.querySelector(sel) : null; }
function qa(sel) { const r = root(); return r ? [...r.querySelectorAll(sel)] : []; }
function visible(el) {
  for (let n = el; n && n.style; n = n.parentElement) {
    if (n.style.display === 'none') return false;
  }
  return true;
}
function setInput(el, value) {
  el.value = value;
  el.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
}

test.before(async () => {
  // identity: one password factor + one passkey factor (this device), OR policy
  SERVER.root = await mintRoot();
  const pw = await createPasswordFactor(SERVER.root.rootPub, 'pw.main', PW, IT);
  pw.seed.fill(0);
  const cred = await authenticator.createCredential({
    rpId: RP, origin: ORIGIN, challenge: 'c0', prf: prfEvalExtension().prf, evalAtCreate: true,
  });
  const prf = prfOutputFromResults(cred.clientExtensionResults);
  const recip = await deriveEncapsulationKeypair(new Uint8Array(prf), FACTOR_RECIPIENT_PURPOSE);
  const pk = {
    factor_id: 'pk.mac',
    type: 'passkey',
    credential_id: cred.rawId,
    recipients: [{ recipient_public_key: recip.publicKeyHex, label: 'This Mac', created_at: '2026-08-01T00:00:00Z' }],
  };
  SERVER.passkeyRows = [{ credential_id: cred.rawId, rp_id: RP, transports: ['internal'], label: 'This Mac' }];
  SERVER.state = {
    generation: 1,
    factors: [pw.factor, pk].sort((a, b) => a.factor_id.localeCompare(b.factor_id)),
    access: ['pk.mac', 'pw.main'],
    policy: canonicalExpression({
      op: 'or',
      children: [{ op: 'factor', factor_id: 'pw.main' }, { op: 'factor', factor_id: 'pk.mac' }],
    }),
  };
  SERVER.armor = await buildFactorPolicyArmor({
    rootSeed: SERVER.root.seed, rootPub: SERVER.root.rootPub,
    generation: 1, factors: SERVER.state.factors,
    access: SERVER.state.access, policy: SERVER.state.policy,
  });

  dom = new JSDOM('<!doctype html><html><body></body></html>', {
    url: 'https://localhost/', pretendToBeVisual: true, runScripts: 'dangerously',
  });
  global.window = dom.window;
  global.document = dom.window.document;
  global.HTMLElement = dom.window.HTMLElement;
  global.getComputedStyle = dom.window.getComputedStyle;
  Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true });
  installWebAuthn(dom.window);
  global.fetch = router;
  dom.window.fetch = router;
  global.sessionStorage = dom.window.sessionStorage;

  panel = await import('../static/js/factor-management.js');
  await panel.open({});
  const alpine = fs.readFileSync(
    path.join(here, '..', 'static', 'vendor', 'alpine-3.15.12.min.js'), 'utf8',
  );
  dom.window.eval(alpine);
  await until(() => qa('.row').length >= 2, 'initial rows');
});

test('the panel renders one row per factor slot from the live view', async () => {
  assert.equal(qa('.row').length, 2);
  const words = qa('.authcell .ac-lbl').map((e) => e.textContent);
  assert.deepEqual(words.sort(), ['Full authority', 'Full authority']);
  assert.ok(!visible(q('.commitbar')) || q('.commitbar') === null, 'no commit bar before edits');
});

test('demoting a passkey stages one change and offers the commit bar', async () => {
  const cells = qa('.authcell');
  const pkCell = cells[1];   // second row = passkeys group (password renders first)
  pkCell.click();
  await until(() => qa('.ac-lbl').some((e) => e.textContent === 'Unlock only'), 'demoted label');
  await until(() => q('.commitbar') && visible(q('.commitbar')), 'commit bar');
  assert.match(q('.commitbar .btn-primary').textContent, /Commit\s*1/);
});

test('commit authorizes on the CURRENT policy and writes an armor that proves the new one', async () => {
  q('.commitbar .btn-primary').click();
  await until(() => q('input[autocomplete=current-password]'), 'authorize screen');
  // gen-1 policy is OR(password, passkey): both openers offered
  assert.ok(q('.pkbtn'), 'passkey opener offered');
  // the ceremony states in words exactly what it is signing
  await until(() => q('.authchanges'), 'change description shown');
  assert.match(q('.authchanges').textContent, /Full authority → Unlock only/);
  setInput(q('input[autocomplete=current-password]'), PW);
  await until(() => { const b = qa('.authrow .btn-primary').pop(); return b && !b.disabled; }, 'Authorize enabled');
  qa('.authrow .btn-primary').pop().click();
  await until(() => SERVER.commits.length === 1, 'first commit posted');
  assert.equal(SERVER.commits.length, 1);
  const committed = SERVER.commits[0];
  assert.deepEqual(committed.operations.map((o) => o.op), ['set_root_policy']);

  // crypto proof: the new armor opens with the password factor…
  const envelope = await parseFactorPolicyArmor(committed.armor);
  assert.equal(envelope.generation, 2);
  assert.deepEqual(envelope.policy, { op: 'factor', factor_id: 'pw.main' });
  const pwFactor = envelope.factors.find((f) => f.factor_id === 'pw.main');
  const seed = await openPasswordFactor(envelope.root_pub, pwFactor, PW);
  const opened = await openFactorPolicyArmor(committed.armor, { 'pw.main': seed });
  assert.equal(opened.rootPub, SERVER.root.rootPub);
  opened.seed.fill(0);

  // …and REFUSES the demoted passkey alone.
  const asrt = await authenticator.getAssertion({
    rpId: RP, origin: ORIGIN, challenge: 'c9',
    credentialId: SERVER.passkeyRows[0].credential_id, prf: prfEvalExtension().prf,
  });
  const pkSeed = prfOutputFromResults(asrt.clientExtensionResults);
  await assert.rejects(
    () => openFactorPolicyArmor(committed.armor, { 'pk.mac': new Uint8Array(pkSeed) }),
  );

  // the panel reloaded onto the committed generation
  const comp = dom.window.Alpine.$data(document.querySelector('.fui-cred'));
  await until(() => comp.generation === 2 && !comp.loading, 'reload onto generation 2');
  await until(() => qa('.ac-lbl').some((e) => e.textContent === 'Unlock only'), 'demoted label after reload');
  assert.ok(!visible(q('.commitbar')) || !q('.commitbar'), 'commit bar cleared after commit');
});

test('a staged password change commits and re-keys the armor', async () => {
  // Change → type → Continue → re-enter → Confirm (the design's inline flow)
  await until(() => qa('.lnk.lchange').some((b) => b.textContent.trim() === 'Change'), 'Change link');
  // keep the staged-change KDF cheap in tests (the server sets the real floor)
  dom.window.Alpine.$data(document.querySelector('.fui-cred')).minIterations = IT;
  const change = qa('.lnk.lchange').find((b) => b.textContent.trim() === 'Change');
  change.click();
  await until(() => q('.inlineinput') && visible(q('.inlineinput')), 'inline password input');
  setInput(qa('.inlineinput')[0], PW2);
  await until(() => !q('.inlinego').disabled, 'Continue enabled');
  q('.inlinego').click();
  await until(() => qa('.inlineinput')[1] && visible(qa('.inlineinput')[1]), 'confirm input');
  setInput(qa('.inlineinput')[1], PW2);
  await until(() => !q('.inlinego').disabled, 'Confirm enabled');
  q('.inlinego').click();
  await until(() => q('.inlineok'), 'inline ok');
  await until(() => q('.commitbar') && visible(q('.commitbar')), 'commit bar (change staged)');

  q('.commitbar .btn-primary').click();
  await until(() => q('input[autocomplete=current-password]'), 'authorize screen (2nd commit)');
  // gen-2 policy is password-only: no passkey opener on the authorize screen
  assert.equal(q('.pkbtn'), null);
  setInput(q('input[autocomplete=current-password]'), PW);   // the CURRENT password authorizes
  await until(() => { const b = qa('.authrow .btn-primary').pop(); return b && !b.disabled; }, 'Authorize enabled (2nd)');
  qa('.authrow .btn-primary').pop().click();
  await until(() => SERVER.commits.length === 2, 'second commit posted');

  const committed = SERVER.commits[1];
  assert.deepEqual(committed.operations.map((o) => o.op), ['change_password']);
  const envelope = await parseFactorPolicyArmor(committed.armor);
  assert.equal(envelope.generation, 3);
  const pwFactor = envelope.factors.find((f) => f.factor_id === 'pw.main');
  // the NEW password opens the new generation; the old one no longer does
  const seed = await openPasswordFactor(envelope.root_pub, pwFactor, PW2);
  (await openFactorPolicyArmor(committed.armor, { 'pw.main': seed })).seed.fill(0);
  await assert.rejects(() => openPasswordFactor(envelope.root_pub, pwFactor, PW));
});

test('the solver blocks deleting the last full-authority factor in the DOM', async () => {
  await until(() => qa('.lnk.ldelete').some((b) => b.textContent.trim() === 'Delete'), 'Delete link');
  const del = qa('.lnk.ldelete').find((b) => b.textContent.trim() === 'Delete');
  assert.ok(del.classList.contains('locked'), 'delete renders locked');
  del.click();
  await flush();
  assert.ok(q('.warnbubble'), 'warning bubble shown');
  assert.equal(SERVER.commits.length, 2, 'nothing new committed');
});

// ── the ceremony walk: MFA round-trip and the two-device iCloud arc ────────
async function pkSeedFor(device, credId) {
  const asrt = await device.getAssertion({
    rpId: RP, origin: ORIGIN, challenge: 'walk', credentialId: credId, prf: prfEvalExtension().prf,
  });
  return new Uint8Array(prfOutputFromResults(asrt.clientExtensionResults));
}
function comp() { return dom.window.Alpine.$data(document.querySelector('.fui-cred')); }
async function authorizeWithPassword(pw) {
  await until(() => q('input[autocomplete=current-password]'), 'authorize screen');
  setInput(q('input[autocomplete=current-password]'), pw);
  await until(() => { const b = qa('.authrow .btn-primary').pop(); return b && !b.disabled; }, 'Authorize enabled');
  qa('.authrow .btn-primary').pop().click();
}

test('walk: re-promote the passkey and enable MFA — the committed armor is a real AND', async () => {
  const credId = SERVER.passkeyRows[0].credential_id;
  await until(() => comp().generation === 3 && !comp().loading, 'gen 3 loaded');
  await until(() => qa('.ac-lbl').some((e) => e.textContent === 'Unlock only'), 'demoted passkey row');
  // promote the passkey back to full…
  const pkCell = qa('.authcell').find((c) => c.querySelector('.ac-lbl').textContent === 'Unlock only');
  pkCell.click();
  await until(() => qa('.ac-lbl').filter((e) => e.textContent === 'Full authority').length === 2, 'both full');
  // …and enable MFA (any) through the real setup screen
  q('.mfacard .addbtn').click();
  await until(() => q('.modeseg'), 'MFA setup screen');
  const enable = qa('.authrow .btn-primary').pop();
  assert.equal(enable.disabled, false, 'any-mode MFA enableable');
  enable.click();
  await until(() => q('.commitbar') && visible(q('.commitbar')), 'commit bar (MFA staged)');

  q('.commitbar .btn-primary').click();
  await authorizeWithPassword(PW2);   // gen-3 policy is password-only
  await until(() => SERVER.commits.length === 3, 'MFA commit posted');

  const committed = SERVER.commits[2];
  const envelope = await parseFactorPolicyArmor(committed.armor);
  assert.equal(envelope.policy.op, 'and', 'root policy is an AND');
  const pwFactor = envelope.factors.find((f) => f.factor_id === 'pw.main');
  const pwSeed = await openPasswordFactor(envelope.root_pub, pwFactor, PW2);
  const pkSeed = await pkSeedFor(authenticator, credId);
  // each factor ALONE is refused; both together open
  await assert.rejects(() => openFactorPolicyArmor(committed.armor, { 'pw.main': new Uint8Array(pwSeed) }));
  await assert.rejects(() => openFactorPolicyArmor(committed.armor, { 'pk.mac': new Uint8Array(pkSeed) }));
  const opened = await openFactorPolicyArmor(committed.armor, {
    'pw.main': pwSeed, 'pk.mac': pkSeed,
  });
  assert.equal(opened.rootPub, SERVER.root.rootPub);
  opened.seed.fill(0);
  await until(() => comp().generation === 4 && !comp().loading, 'gen 4 loaded');
});

test('walk: under MFA the last password of its class cannot be removed', async () => {
  await until(() => qa('.lnk.ldelete').some((b) => b.textContent.trim() === 'Delete'), 'Delete link');
  const del = qa('.lnk.ldelete').find((b) => b.textContent.trim() === 'Delete');
  assert.ok(del.classList.contains('locked'), 'delete locked under MFA');
  del.click();
  await flush();
  assert.ok(q('.warnbubble'), 'refusal explains itself');
  assert.equal(comp().changeCount, 0, 'nothing staged');
  comp().warnAt = null;
});

test('walk: disabling MFA authorizes with BOTH factors of the current policy', async () => {
  qa('.lnk.ldisable').find((b) => b.textContent.trim() === 'Disable').click();
  await until(() => !comp().mfaOn, 'MFA staged off');
  // promote the passkey too, so the OR keeps both factors
  const pkCell = qa('.authcell').find((c) => c.querySelector('.ac-lbl').textContent !== 'Full authority');
  pkCell.click();
  await until(() => q('.commitbar') && visible(q('.commitbar')), 'commit bar (disable staged)');

  q('.commitbar .btn-primary').click();
  await until(() => q('input[autocomplete=current-password]'), 'authorize screen (AND)');
  assert.ok(q('.pkbtn'), 'passkey half offered');
  // password half…
  setInput(q('input[autocomplete=current-password]'), PW2);
  await until(() => { const b = qa('.authrow .btn-primary').pop(); return b && !b.disabled; }, 'Authorize enabled');
  qa('.authrow .btn-primary').pop().click();
  await until(() => comp().password === '', 'password half accepted');
  assert.equal(SERVER.commits.length, 3, 'AND not satisfied by the password alone');
  // the accepted half shows its green state; its input is gone, in either order
  await until(() => qa('.inlineok').some((e) => e.textContent.includes('Password verified')), 'password ✓ shown');
  assert.equal(q('input[autocomplete=current-password]'), null, 'password input retired');
  assert.ok(q('.pkbtn'), 'passkey still offered');
  // …and the passkey half settles it
  q('.pkbtn').click();
  await until(() => SERVER.commits.length === 4, 'disable-MFA commit posted');

  const envelope = await parseFactorPolicyArmor(SERVER.commits[3].armor);
  assert.equal(envelope.policy.op, 'or', 'back to any-one');
  const pwFactor = envelope.factors.find((f) => f.factor_id === 'pw.main');
  const seed = await openPasswordFactor(envelope.root_pub, pwFactor, PW2);
  (await openFactorPolicyArmor(SERVER.commits[3].armor, { 'pw.main': seed })).seed.fill(0);
  await until(() => comp().generation === 5 && !comp().loading, 'gen 5 loaded');
});

test('walk: on a second device the synced credential is detected as not enrolled', async () => {
  const credId = SERVER.passkeyRows[0].credential_id;
  SERVER.passkeyRows[0].backed_up = true;   // iCloud-synced
  const deviceB = cloneCredentialToDevice(authenticator, credId);
  switchDevice(deviceB);
  await comp().load();
  await until(() => !comp().loading, 'reloaded on device B');

  // stage any edit, then try to authorize with the passkey from device B
  const pwCell = qa('.authcell').find((c) => c.querySelector('svg use').getAttribute('xlink:href') === '#i-key');
  pwCell.click();   // full → unlock (allowed: the passkey holds full)
  await until(() => q('.commitbar') && visible(q('.commitbar')), 'commit bar');
  q('.commitbar .btn-primary').click();
  await until(() => q('.pkbtn'), 'authorize screen offers the passkey');
  q('.pkbtn').click();
  await until(() => comp().toast.includes('not enrolled to authorize'), 'PRF mismatch detected');
  // the failed tap surfaces the requirement list: which factor, which devices
  await until(() => q('.mfadyn'), 'missing-factor explanation shown');
  assert.match(q('.mfadyn').textContent, /enrolled on: .*This Mac/, 'lists the enrolled device');
  assert.ok(!q('.mfadyn').textContent.includes('can’t authorize'),
    'not a dead end — the password route remains');

  // cancel out and drop the staged edit
  q('.scrhead .back').click();
  await until(() => comp().cur === 'credentials', 'back to the list');
  comp().cancelChanges();
  await until(() => comp().changeCount === 0, 'staged edit dropped');
  globalThis.__deviceB = deviceB;
});

test('walk: the design\'s +Add pivot repairs the second device, third device still refused', async () => {
  const credId = SERVER.passkeyRows[0].credential_id;
  const deviceB = globalThis.__deviceB;
  // the design's path: "+ Add" in Passkeys — its ceremony finds the synced
  // credential with no local slot and pivots to enrolling THIS device's slot
  // ("the user only ever sees success + a new row"); no row badge exists
  const pkHead = qa('.grouphead').find((h) => h.querySelector('.grouplbl').textContent.trim().toLowerCase() === 'passkeys');
  pkHead.querySelector('.addbtn').click();
  await until(() => q('.pkbtn'), 'add-passkey screen');
  q('.pkbtn').click();
  await until(() => comp().passkeys.some((k) => k._addRecipient), 'device slot staged');
  await until(() => q('.commitbar') && visible(q('.commitbar')), 'commit bar');
  q('.commitbar .btn-primary').click();
  await authorizeWithPassword(PW2);
  await until(() => SERVER.commits.length === 5, 'enroll commit posted');

  const committed = SERVER.commits[4];
  const envelope = await parseFactorPolicyArmor(committed.armor);
  const pkFactor = envelope.factors.find((f) => f.factor_id === 'pk.mac');
  assert.equal(pkFactor.recipients.length, 2, 'two device slots on one credential');
  // device B's PRF now opens the armor…
  const seedB = await pkSeedFor(deviceB, credId);
  (await openFactorPolicyArmor(committed.armor, { 'pk.mac': seedB })).seed.fill(0);
  // …device A's still does…
  const seedA = await pkSeedFor(authenticator, credId);
  (await openFactorPolicyArmor(committed.armor, { 'pk.mac': seedA })).seed.fill(0);
  // …and an unenrolled third device with the same synced credential is refused.
  const deviceC = cloneCredentialToDevice(authenticator, credId);
  const seedC = await pkSeedFor(deviceC, credId);
  await assert.rejects(() => openFactorPolicyArmor(committed.armor, { 'pk.mac': seedC }));
});

test('walk: a stashed login detection greets with the name-this-device dialog and enrolls under one password', async () => {
  const credId = SERVER.passkeyRows[0].credential_id;
  // a fourth device signed in with the synced credential; unlock.js stashed
  // the mismatch (public data only) — simulate exactly that stash
  const deviceD = cloneCredentialToDevice(authenticator, credId);
  const prfD = await pkSeedFor(deviceD, credId);
  const recipD = await deriveEncapsulationKeypair(new Uint8Array(prfD), FACTOR_RECIPIENT_PURPOSE);
  dom.window.sessionStorage.setItem('autonomy.factor.pending-slot', JSON.stringify({
    factor_id: 'pk.mac', credential_id: credId,
    recipient_public_key: recipD.publicKeyHex, label: 'New device',
  }));
  switchDevice(deviceD);
  await comp().load();
  await until(() => comp().cur === 'newdevice', 'name-this-device dialog');
  assert.equal(comp().top.enrolled, false, 'needs the one root proof');
  assert.equal(comp().deviceName, 'New device');

  const nameInput = q('input[type=text]');
  setInput(nameInput, 'Kitchen iPad');
  await until(() => { const b = qa('.authrow .btn-primary').pop(); return b && !b.disabled; }, 'Enroll factor enabled');
  const enrollBtn = qa('.authrow .btn-primary').pop();
  assert.equal(enrollBtn.textContent, 'Enroll factor');
  enrollBtn.click();
  // the STANDARD root ceremony control takes over — same authorize screen
  await authorizeWithPassword(PW2);
  await until(() => SERVER.commits.length === 6, 'device-slot commit posted');
  await until(() => comp().cur === 'credentials', 'landed on the factor list');

  const committed = SERVER.commits[5];
  assert.deepEqual(committed.operations.map((o) => o.op), ['add_passkey_recipient']);
  const envelope = await parseFactorPolicyArmor(committed.armor);
  const pkFactor = envelope.factors.find((f) => f.factor_id === 'pk.mac');
  const slot = pkFactor.recipients.find((r) => r.recipient_public_key === recipD.publicKeyHex);
  assert.equal(slot.label, 'Kitchen iPad', 'named right in the armor');
  // the new device's PRF now opens the root
  const seedD = await pkSeedFor(deviceD, credId);
  (await openFactorPolicyArmor(committed.armor, { 'pk.mac': seedD })).seed.fill(0);
  assert.equal(dom.window.sessionStorage.getItem('autonomy.factor.pending-slot'), null, 'stash consumed');
  // the list shows it as its own device row, sib-tied to the credential
  await until(() => qa('.row').some((r) => r.textContent.includes('Kitchen iPad')), 'device row rendered');
});

test('walk: a slot already enrolled at login only asks for its name', async () => {
  const credId = SERVER.passkeyRows[0].credential_id;
  const envelope = await parseFactorPolicyArmor(SERVER.armor);
  const anySlot = envelope.factors.find((f) => f.factor_id === 'pk.mac').recipients[0];
  dom.window.sessionStorage.setItem('autonomy.factor.slot-enrolled', JSON.stringify({
    factor_id: 'pk.mac', recipient_public_key: anySlot.recipient_public_key, label: 'New device',
  }));
  await comp().load();
  await until(() => comp().cur === 'newdevice', 'dialog opens');
  assert.equal(comp().top.enrolled, true, 'no password asked');
  assert.equal(q('input[autocomplete=current-password]'), null, 'no password field');
  setInput(q('input[type=text]'), 'Named at login');
  await until(() => { const b = qa('.authrow .btn-primary').pop(); return b && !b.disabled; }, 'OK enabled');
  assert.equal(qa('.authrow .btn-primary').pop().textContent, 'OK');
  qa('.authrow .btn-primary').pop().click();
  await until(() => comp().cur === 'credentials', 'back on the list');
  assert.equal(dom.window.sessionStorage.getItem('autonomy.factor.slot-enrolled'), null, 'flag consumed');
  assert.equal(SERVER.commits.length, 6, 'renaming is metadata, not a generation');
});

test('walk: an autofilled password auto-submits the ceremony', async () => {
  // stage one edit so the commit bar offers a ceremony (policy is OR: password alone settles)
  await until(() => qa('.authcell').length >= 2, 'rows present');
  const pwCell = qa('.authcell').find((c) => c.querySelector('svg use').getAttribute('xlink:href') === '#i-key');
  pwCell.click();   // full → unlock (the passkey holds full)
  await until(() => q('.commitbar') && visible(q('.commitbar')), 'commit bar');
  const commitsBefore = SERVER.commits.length;
  q('.commitbar .btn-primary').click();
  await until(() => q('input[autocomplete=current-password]'), 'authorize screen');
  // the browser autofills: value lands and the autofill animation hook fires
  const input = q('input[autocomplete=current-password]');
  setInput(input, PW2);
  const ev = new dom.window.Event('animationstart', { bubbles: true });
  Object.defineProperty(ev, 'animationName', { value: 'fui-afstart' });
  input.dispatchEvent(ev);
  // no Authorize click: the ceremony runs to completion on its own
  await until(() => SERVER.commits.length === commitsBefore + 1, 'auto-submitted commit posted');
  await until(() => comp().cur === 'credentials', 'landed back on the list');
});
