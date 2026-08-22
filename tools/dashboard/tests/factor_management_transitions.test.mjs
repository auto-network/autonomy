/* Fast JSDOM integration test for the factor-management panel.
 *
 * Drives the REAL panel UI (tools/dashboard/static/js/factor-management.js) in
 * JSDOM against the REAL re-arm crypto (ceremony/primitives.js) with node's
 * webcrypto — no browser, no agent-browser. For each starting armor state it
 * performs a transition exactly as the operator would (clicking the rendered
 * controls, typing passwords, presenting passkeys), captures the armor the panel
 * POSTs, and asserts it opens with the factors the legal target state must have
 * and refuses the ones it must not. This is the coverage that proves every state
 * transition the UI offers lands somewhere legal.
 *
 *   node --test tools/dashboard/tests/factor_management_transitions.test.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const { JSDOM } = createRequire(import.meta.url)('jsdom');
import {
  encryptArmor, encryptArmorCombined, addPasskeyFactor, removePasswordFactor,
  decryptArmor, decryptArmorWithPasskey, decryptArmorWithCombined,
  parseArmor, bytesToHex,
} from '../static/js/ceremony/primitives.js';
import { deriveProvisioningKey } from '../static/js/ceremony/enrollment.js';

const IT = 10000;

// A credential id in WebAuthn/the armor is base64url; getPrf decodes it. We key
// the deterministic PRF on a plain-ascii LABEL and store base64url(label) as the
// credential id everywhere, so the id round-trips through getPrf's b64u decode
// back to the label the mock authenticator answers for.
function enc(label) {
  return btoa(label).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}
// ── deterministic PRF per LABEL (stable across the whole test) ──────────────
function prfFor(label) {
  const b = new Uint8Array(32);
  for (let i = 0; i < 32; i += 1) b[i] = (label.charCodeAt(i % label.length) + i * 7) & 0xff;
  return b;
}
async function provPub(label) { return (await deriveProvisioningKey(prfFor(label))).publicKeyHex; }
async function pkRow(label) {
  return { credential_id: enc(label), provisioning_public_key: await provPub(label) };
}

// ── mint a fresh Ed25519 root (seed + public hex) with real crypto ──────────
async function mintRoot() {
  const kp = await crypto.subtle.generateKey('Ed25519', true, ['sign', 'verify']);
  const pk8 = new Uint8Array(await crypto.subtle.exportKey('pkcs8', kp.privateKey));
  const rawPub = new Uint8Array(await crypto.subtle.exportKey('raw', kp.publicKey));
  return { seed: pk8.slice(-32), rootPub: bytesToHex(rawPub) };
}

// ── starting-armor builders ────────────────────────────────────────────────
async function aPassword(root, pw) { return encryptArmor(root.seed, root.rootPub, pw, IT); }
async function aBoth(root, pw, cred) {
  const a = await encryptArmor(root.seed, root.rootPub, pw, IT);
  return addPasskeyFactor(a, pw, enc(cred), await provPub(cred));
}
async function aPasskeyOnly(root, pw, cred) {
  return removePasswordFactor(await aBoth(root, pw, cred), pw);
}
async function aMfa(root, pw, cred) {
  return encryptArmorCombined(root.seed, root.rootPub, pw, enc(cred), await provPub(cred), IT);
}

// ── one in-memory dashboard, swapped per test ───────────────────────────────
let SERVER = null;
function makeServer({ armor, rootPub, passkeys, rpId = 'localhost' }) {
  return {
    armor, rootPub, rpId,
    passkeys: passkeys.map((p) => ({ ...p })),
    posts: [],           // every armor the panel re-armed
    createdCredId: null, // last enrolled credential id
  };
}

function jsonResponse(obj, ok = true) {
  return { ok, status: ok ? 200 : 400, json: async () => obj };
}

async function router(url, opts = {}) {
  const method = (opts.method || 'GET').toUpperCase();
  const S = SERVER;
  if (url === '/api/identity/status') {
    return jsonResponse({
      rp_id: S.rpId,
      personal_identity: { display_name: 'Jeremy' },
      passkeys: S.passkeys.map((p) => ({
        credential_id: p.credential_id,
        label: p.label || 'This device',
        transports: p.transports || ['internal'],
        provisioning_public_key: p.provisioning_public_key || null,
        rp_id: p.rp_id || S.rpId,
        created_at: p.created_at || '2026-08-20T00:00:00Z',
      })),
    });
  }
  if (url === '/api/identity/personal' && method === 'GET') {
    return jsonResponse({ armored_private_key: S.armor, created_at: '2026-07-19T02:34:10Z' });
  }
  if (url === '/api/identity/personal/armor' && method === 'POST') {
    const body = JSON.parse(opts.body);
    S.armor = body.armored_private_key;
    S.posts.push(body);
    return jsonResponse({ ok: true });
  }
  if (url === '/api/identity/passkey/register-options' && method === 'POST') {
    return jsonResponse({
      ok: true, rp_id: S.rpId, origin: `https://${S.rpId}`, nonce: 'ab'.repeat(32),
      options: { challenge: 'AAAA', user: { id: 'AAAA' }, excludeCredentials: [] },
    });
  }
  if (url === '/api/identity/passkey/register' && method === 'POST') {
    const body = JSON.parse(opts.body);
    const st = body.statement;
    S.createdCredId = st.credential_id;
    S.passkeys.push({
      credential_id: st.credential_id, label: body.label,
      transports: st.transports || ['internal'],
      provisioning_public_key: st.provisioning_public_key || null, rp_id: st.rp_id,
    });
    return jsonResponse({ ok: true });
  }
  if (url.startsWith('/api/identity/passkey/') && method === 'DELETE') {
    const id = decodeURIComponent(url.split('/').pop());
    S.passkeys = S.passkeys.filter((p) => p.credential_id !== id);
    return jsonResponse({ ok: true });
  }
  throw new Error(`unrouted fetch: ${method} ${url}`);
}

// ── WebAuthn mock: PRF get returns the deterministic PRF of the selected (or
//    sole root) credential; create() returns a synthetic attestation. ────────
function b64uToStr(b64u) {
  let b = b64u.replace(/-/g, '+').replace(/_/g, '/'); while (b.length % 4) b += '=';
  return atob(b);
}
function credIdFromAllow(allow) {
  if (allow && allow.length) {
    const raw = allow[0].id; // Uint8Array of the credential id bytes (ascii)
    return String.fromCharCode(...new Uint8Array(raw));
  }
  return SERVER.passkeys[0] && SERVER.passkeys[0].credential_id;
}
function extResultsFor(prf) {
  const buf = prf.buffer.slice(prf.byteOffset, prf.byteOffset + prf.byteLength);
  return { prf: { enabled: true, results: { first: buf } } };
}
function installWebAuthn(win) {
  win.PublicKeyCredential = function () {};
  const credentials = {
    async get({ publicKey }) {
      const id = credIdFromAllow(publicKey.allowCredentials);
      const prf = prfFor(id);
      return { getClientExtensionResults: () => extResultsFor(prf) };
    },
    async create() {
      const id = `enrolled-${SERVER.passkeys.length + 1}`;
      SERVER._pendingCred = id;
      const prf = prfFor(id);
      // synthetic authenticatorData: rpIdHash[32] flags[1] signCount[4]
      // aaguid[16] credIdLen[2] credId cose  — attestedCredential slices it.
      const credBytes = new TextEncoder().encode(id);
      const cose = new Uint8Array([0xa1, 0x01, 0x02]); // any bytes; server canonicalizes
      const authData = new Uint8Array(55 + credBytes.length + cose.length);
      authData[53] = (credBytes.length >> 8) & 0xff; authData[54] = credBytes.length & 0xff;
      authData.set(credBytes, 55); authData.set(cose, 55 + credBytes.length);
      const rawId = credBytes;
      return {
        id, rawId, type: 'public-key', authenticatorAttachment: 'platform',
        getClientExtensionResults: () => extResultsFor(prf),
        response: {
          getAuthenticatorData: () => authData,
          getTransports: () => ['internal', 'hybrid'],
          clientDataJSON: new TextEncoder().encode('{}'),
          attestationObject: new Uint8Array([0]),
        },
      };
    },
  };
  Object.defineProperty(win.navigator, 'credentials', { value: credentials, configurable: true });
}

// ── globals ────────────────────────────────────────────────────────────────
let panel;
test.before(async () => {
  const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'https://localhost/' });
  global.window = dom.window;
  global.document = dom.window.document;
  global.HTMLElement = dom.window.HTMLElement;
  // Keep node's atob/btoa (primitives.js encodes latin1 binary strings that
  // jsdom's stricter implementation rejects); only wire the DOM globals.
  global.getComputedStyle = dom.window.getComputedStyle;
  Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true });
  installWebAuthn(dom.window);
  global.fetch = (url, opts) => router(url, opts);
  panel = await import('../static/js/factor-management.js');
});

// ── DOM helpers ─────────────────────────────────────────────────────────────
const tick = () => new Promise((r) => setTimeout(r, 0));
async function settle(n = 4) { for (let i = 0; i < n; i += 1) await tick(); }
async function until(fn, n = 60) { for (let i = 0; i < n && !fn(); i += 1) await tick(); return fn(); }
function overlay() { const o = document.querySelectorAll('.fui-overlay'); return o[o.length - 1] || null; }
function q(sel) { const r = overlay(); return r ? r.querySelector(sel) : null; }
function qa(sel) { const r = overlay(); return r ? [...r.querySelectorAll(sel)] : []; }
function warnText() { const w = q('.warn'); return w ? w.textContent : ''; }
function clickPactByText(text) {
  const row = qa('.pact').find((e) => e.textContent.includes(text));
  assert.ok(row, `pact "${text}" present`); row.click();
}
async function openPanel() {
  [...document.querySelectorAll('.fui-overlay')].forEach((e) => e.remove());
  panel.open({});
  await until(() => q('.krow') || q('.pact'));
}
// Walk the gather (Authorizing) screen: present passkeys and type the password,
// waiting for each async step to advance the DOM before the next.
async function gather(pw) {
  for (let guard = 0; guard < 8; guard += 1) {
    await settle();
    if (!q('.st')) return;                        // left the prog screen
    const sheetBtn = q('.sheet .shbtn');
    if (sheetBtn) { sheetBtn.click(); await until(() => !q('.sheet')); continue; }
    const input = (q('.st.busy') && q('input.oin')) || null;
    if (input) {
      input.value = pw;
      const busyNow = q('.st.busy');
      q('.btn.flat').click();
      await until(() => q('.st.busy') !== busyNow || !q('.st'));
      continue;
    }
    return;
  }
}
// Click the primary (non-flat) button and wait for the panel to re-arm.
async function saveAndAwaitPost() {
  const before = SERVER.posts.length;
  qa('.btn').filter((x) => !x.classList.contains('flat')).pop().click();
  await until(() => SERVER.posts.length > before);
}

// ── armor assertions (real crypto) ──────────────────────────────────────────
async function opensWithPassword(armor, pw) {
  try { await decryptArmor(armor, pw); return true; } catch { return false; }
}
async function opensWithPasskey(armor, cred) {
  try { await decryptArmorWithPasskey(armor, prfFor(cred)); return true; } catch { return false; }
}
async function opensWithCombined(armor, pw, cred) {
  try { await decryptArmorWithCombined(armor, pw, prfFor(cred)); return true; } catch { return false; }
}
function factorTypes(armor) { return parseArmor(armor).factors.map((f) => f.type).sort(); }

// ═══════════════════════════════ transitions ═══════════════════════════════

test('MFA → password + passkey (Change password: the operator bug)', async () => {
  const root = await mintRoot(); const cred = 'cred-mfa-a';
  const armor = await aMfa(root, 'oldpw', cred);
  SERVER = makeServer({
    armor, rootPub: root.rootPub,
    passkeys: [await pkRow(cred)],
  });
  await openPanel();
  q('.krow .chg').click();          // Password row → Change
  await gather('oldpw');            // Authorizing: present passkey + type current pw
  const [a, b] = qa('input.oin'); a.value = 'newpw'; b.value = 'newpw';
  qa('.btn').filter((x) => !x.classList.contains('flat')).pop().click();
  await until(() => SERVER.posts.length > 0, 120);
  assert.ok(SERVER.posts.length, 'a re-arm was posted; warn=' + warnText());
  const out = SERVER.armor;
  assert.deepEqual(factorTypes(out), ['passkey', 'password']);
  assert.ok(await opensWithPassword(out, 'newpw'), 'new password opens alone');
  assert.ok(await opensWithPasskey(out, cred), 'passkey opens alone');
  assert.ok(!(await opensWithPassword(out, 'oldpw')), 'old combined password no longer opens');
});

test('MFA → password + passkey (authority editor: uncheck Multi-Factor)', async () => {
  const root = await mintRoot(); const cred = 'cred-mfa-b';
  const armor = await aMfa(root, 'pw', cred);
  SERVER = makeServer({
    armor, rootPub: root.rootPub,
    passkeys: [await pkRow(cred)],
  });
  await openPanel();
  q('.krow [data-p]').click();      // password authority badge → editor (reachable under MFA now)
  await gather('pw');
  assert.ok(q('.pick'), 'authority editor rendered');
  const both = qa('.pick').find((r) => r.textContent.includes('Multi-Factor'));
  assert.ok(both, 'Multi-Factor row present'); both.click();     // uncheck the pair
  qa('.btn').pop().click();          // Save
  await until(() => SERVER.posts.length > 0, 120);
  assert.ok(SERVER.posts.length, 'a re-arm was posted; warn=' + warnText());
  const out = SERVER.armor;
  assert.deepEqual(factorTypes(out), ['passkey', 'password']);
  assert.ok(await opensWithPassword(out, 'pw'));
  assert.ok(await opensWithPasskey(out, cred));
});

test('password + passkey → MFA (authority editor: check Multi-Factor)', async () => {
  const root = await mintRoot(); const cred = 'cred-both-a';
  const armor = await aBoth(root, 'pw', cred);
  SERVER = makeServer({
    armor, rootPub: root.rootPub,
    passkeys: [await pkRow(cred)],
  });
  await openPanel();
  q('.krow [data-p]').click();      // password authority badge → editor
  await gather('pw');
  assert.ok(q('.pick'), 'authority editor rendered');
  const both = qa('.pick').find((r) => r.textContent.includes('Multi-Factor'));
  both.click();                      // check the pair
  qa('.btn').pop().click();          // Save
  await until(() => SERVER.posts.length > 0, 120);
  assert.ok(SERVER.posts.length, 'a re-arm was posted; warn=' + warnText());
  const out = SERVER.armor;
  assert.deepEqual(factorTypes(out), ['combined']);
  assert.ok(await opensWithCombined(out, 'pw', cred), 'combined opens with both');
  assert.ok(!(await opensWithPassword(out, 'pw')), 'password alone refused under MFA');
  assert.ok(!(await opensWithPasskey(out, cred)), 'passkey alone refused under MFA');
});

test('passkey-only → + password (Set a password)', async () => {
  const root = await mintRoot(); const cred = 'cred-po-a';
  const armor = await aPasskeyOnly(root, 'pw', cred);
  SERVER = makeServer({
    armor, rootPub: root.rootPub,
    passkeys: [await pkRow(cred)],
  });
  await openPanel();
  clickPactByText('Set a password');
  await gather('pw');               // Authorizing: present the passkey
  const [a, b] = qa('input.oin'); a.value = 'addpw'; b.value = 'addpw';
  qa('.btn').filter((x) => !x.classList.contains('flat')).pop().click();
  await until(() => SERVER.posts.length > 0, 120);
  assert.ok(SERVER.posts.length, 'a re-arm was posted; warn=' + warnText());
  const out = SERVER.armor;
  assert.deepEqual(factorTypes(out), ['passkey', 'password']);
  assert.ok(await opensWithPassword(out, 'addpw'), 'added password opens');
  assert.ok(await opensWithPasskey(out, cred), 'passkey still opens');
});

test('password → change password', async () => {
  const root = await mintRoot();
  const armor = await aPassword(root, 'pw');
  SERVER = makeServer({ armor, rootPub: root.rootPub, passkeys: [] });
  await openPanel();
  q('.krow .chg').click();
  await gather('pw');
  const [a, b] = qa('input.oin'); a.value = 'changed'; b.value = 'changed';
  qa('.btn').filter((x) => !x.classList.contains('flat')).pop().click();
  await until(() => SERVER.posts.length > 0, 120);
  assert.ok(SERVER.posts.length, 'a re-arm was posted; warn=' + warnText());
  const out = SERVER.armor;
  assert.deepEqual(factorTypes(out), ['password']);
  assert.ok(await opensWithPassword(out, 'changed'));
  assert.ok(!(await opensWithPassword(out, 'pw')));
});

test('password + passkey → remove password (passkey-only)', async () => {
  const root = await mintRoot(); const cred = 'cred-rm-a';
  const armor = await aBoth(root, 'pw', cred);
  SERVER = makeServer({
    armor, rootPub: root.rootPub,
    passkeys: [await pkRow(cred)],
  });
  await openPanel();
  q('.krow .kx').click();           // password × → remove
  await gather('pw');
  await until(() => SERVER.posts.length > 0, 120);
  assert.ok(SERVER.posts.length, 'a re-arm was posted; warn=' + warnText());
  const out = SERVER.armor;
  assert.deepEqual(factorTypes(out), ['passkey']);
  assert.ok(await opensWithPasskey(out, cred));
  assert.ok(!(await opensWithPassword(out, 'pw')));
});

test('password + passkey → promote a second passkey to full authority', async () => {
  const root = await mintRoot(); const c1 = 'cred-full-1'; const c2 = 'cred-unlock-2';
  const armor = await aBoth(root, 'pw', c1);   // password + passkey1 (a root factor)
  SERVER = makeServer({
    armor, rootPub: root.rootPub,
    passkeys: [await pkRow(c1), await pkRow(c2)], // c2 registered but NOT a root factor yet
  });
  await openPanel();
  const c2row = qa('.krow').find((r) => r.querySelector('[data-k]') && r.textContent.includes('unlock only'));
  assert.ok(c2row, 'the unlock-only passkey row is present');
  c2row.querySelector('[data-k]').click();     // changeKey → prove root, then present the key
  await gather('pw');                           // proves the password, then presents c2 (promote)
  await until(() => SERVER.posts.length > 0, 120);
  assert.ok(SERVER.posts.length, 'a re-arm was posted; warn=' + warnText());
  const out = SERVER.armor;
  assert.deepEqual(factorTypes(out), ['passkey', 'passkey', 'password']);
  assert.ok(await opensWithPasskey(out, c2), 'the promoted passkey now opens the root alone');
  assert.ok(await opensWithPasskey(out, c1), 'the first passkey still opens');
  assert.ok(await opensWithPassword(out, 'pw'), 'the password still opens');
});

test('password + passkey → demote the passkey to unlock only', async () => {
  const root = await mintRoot(); const cred = 'cred-demote-1';
  const armor = await aBoth(root, 'pw', cred);
  SERVER = makeServer({ armor, rootPub: root.rootPub, passkeys: [await pkRow(cred)] });
  await openPanel();
  const row = qa('.krow').find((r) => r.querySelector('[data-k]') && r.textContent.includes('full authority'));
  assert.ok(row, 'the full-authority passkey row is present');
  row.querySelector('[data-k]').click();        // changeKey → demote (no present step)
  await gather('pw');
  await until(() => SERVER.posts.length > 0, 120);
  assert.ok(SERVER.posts.length, 'a re-arm was posted; warn=' + warnText());
  const out = SERVER.armor;
  assert.deepEqual(factorTypes(out), ['password']);
  assert.ok(await opensWithPassword(out, 'pw'));
  assert.ok(!(await opensWithPasskey(out, cred)), 'the demoted passkey no longer opens the root');
});

test('password-only → enroll a passkey (two-step: Authorizing then Enrolling)', async () => {
  const root = await mintRoot();
  const armor = await aPassword(root, 'pw');
  SERVER = makeServer({ armor, rootPub: root.rootPub, passkeys: [] });
  await openPanel();
  clickPactByText('Add a passkey');
  await gather('pw');                            // Authorizing (open with the password)
  await until(() => q('.sheet .shbtn'));         // Enrolling sheet
  assert.match(q('.sheet .shttl').textContent, /Enrolling/);
  q('.sheet .shbtn').click();                    // create → register
  await until(() => SERVER.createdCredId, 120);
  assert.ok(SERVER.createdCredId, 'a credential was enrolled; warn=' + warnText());
  assert.equal(SERVER.passkeys.length, 1, 'the new credential is registered');
});

test('MFA → dual full-authority factors, no MFA (UI shows BOTH full)', async () => {
  // The operator's flagged case: leave Multi-Factor so the password AND the
  // passkey each hold FULL authority on their own. Assert both the crypto (each
  // opens alone) AND the rendered authority badges (both read "full authority",
  // no error banner) — a passkey left showing "unlock only" here is the failure.
  const root = await mintRoot(); const cred = 'cred-dual';
  const armor = await aMfa(root, 'pw', cred);
  SERVER = makeServer({ armor, rootPub: root.rootPub, passkeys: [await pkRow(cred)] });
  await openPanel();
  q('.krow [data-p]').click();                 // authority editor
  await gather('pw');
  const both = qa('.pick').find((r) => r.textContent.includes('Multi-Factor'));
  both.click();                                 // turn OFF require-both
  // ensure both individual factors are set to full authority in the editor
  for (const name of ['Passkey', 'Password']) {
    const row = qa('.pick').find((r) => r.querySelector('.pn') && r.querySelector('.pn').textContent === name);
    if (row && !row.classList.contains('on')) row.click();
  }
  qa('.btn').pop().click();                      // Save
  await until(() => SERVER.posts.length > 0, 120);
  assert.ok(SERVER.posts.length, 'a re-arm was posted; warn=' + warnText());
  await until(() => q('.krow'));                 // back on the Factors panel
  const out = SERVER.armor;
  assert.deepEqual(factorTypes(out), ['passkey', 'password']);
  assert.ok(await opensWithPassword(out, 'pw'), 'password opens alone');
  assert.ok(await opensWithPasskey(out, cred), 'passkey opens alone');
  // the rendered authority badges must BOTH say full authority
  assert.equal(warnText(), '', 'no invalid-state error');
  const pwBadge = q('.krow .tag');
  assert.match(pwBadge.textContent, /full authority/i, 'password badge full');
  const keyBadge = qa('.krow [data-k]').pop();
  assert.ok(keyBadge, 'passkey authority badge present');
  assert.match(keyBadge.textContent, /full authority/i, 'passkey badge full authority (not unlock only)');
});
