/* JSDOM test for the single factor-aware root-unlock dialog (ceremony/open-root.js).
 *
 * Proves the ONE dialog adapts to the armor's own factor set: a password field
 * for a password armor, a passkey button for a passkey armor, a chooser when
 * either works, and both-required under MFA — each resolving with a signing key
 * whose seed actually opens that armor. No browser, real crypto (node webcrypto).
 *
 *   node --test tools/dashboard/tests/open_root.test.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const { JSDOM } = createRequire(import.meta.url)('jsdom');
import {
  encryptArmor, encryptArmorCombined, addPasskeyFactor, removePasswordFactor, bytesToHex,
} from '../static/js/ceremony/primitives.js';
import { deriveProvisioningKey } from '../static/js/ceremony/enrollment.js';

const IT = 10000;
function enc(l) { return btoa(l).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, ''); }
function prfFor(l) { const b = new Uint8Array(32); for (let i = 0; i < 32; i += 1) b[i] = (l.charCodeAt(i % l.length) + i * 7) & 0xff; return b; }
async function provPub(l) { return (await deriveProvisioningKey(prfFor(l))).publicKeyHex; }
async function mintRoot() {
  const kp = await crypto.subtle.generateKey('Ed25519', true, ['sign', 'verify']);
  const pk8 = new Uint8Array(await crypto.subtle.exportKey('pkcs8', kp.privateKey));
  const rawPub = new Uint8Array(await crypto.subtle.exportKey('raw', kp.publicKey));
  return { seed: pk8.slice(-32), rootPub: bytesToHex(rawPub) };
}
async function aPassword(r, pw) { return encryptArmor(r.seed, r.rootPub, pw, IT); }
async function aBoth(r, pw, c) { return addPasskeyFactor(await encryptArmor(r.seed, r.rootPub, pw, IT), pw, enc(c), await provPub(c)); }
async function aPasskeyOnly(r, pw, c) { return removePasswordFactor(await aBoth(r, pw, c), pw); }
async function aMfa(r, pw, c) { return encryptArmorCombined(r.seed, r.rootPub, pw, enc(c), await provPub(c), IT); }

let SERVER = null;
function J(o) { return { ok: true, status: 200, json: async () => o }; }
async function router(url) {
  if (url === '/api/identity/status') {
    return J({ rp_id: 'localhost', passkeys: SERVER.passkeys });
  }
  if (url === '/api/identity/personal') {
    return J({ armored_private_key: SERVER.armor, root_pub: SERVER.rootPub });
  }
  throw new Error('unrouted ' + url);
}

let openRoot;
test.before(async () => {
  const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'https://localhost/' });
  global.window = dom.window; global.document = dom.window.document;
  global.HTMLElement = dom.window.HTMLElement; global.getComputedStyle = dom.window.getComputedStyle;
  Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true });
  dom.window.PublicKeyCredential = function () {};
  Object.defineProperty(dom.window.navigator, 'credentials', {
    configurable: true,
    value: {
      async get({ publicKey }) {
        const id = String.fromCharCode(...new Uint8Array(publicKey.allowCredentials[0].id));
        const prf = prfFor(id);
        return { getClientExtensionResults: () => ({ prf: { enabled: true, results: { first: prf.buffer } } }) };
      },
    },
  });
  global.fetch = (url) => router(url);
  ({ openRoot } = await import('../static/js/ceremony/open-root.js'));
});

const tick = () => new Promise((r) => setTimeout(r, 0));
async function until(fn, n = 60) { for (let i = 0; i < n && !fn(); i += 1) await tick(); return fn(); }
function q(sel) { const o = document.querySelectorAll('.or-overlay'); const r = o[o.length - 1]; return r ? r.querySelector(sel) : null; }
function qa(sel) { const o = document.querySelectorAll('.or-overlay'); const r = o[o.length - 1]; return r ? [...r.querySelectorAll(sel)] : []; }
function btnByText(t) { return qa('.or-btn').find((b) => b.textContent.includes(t)); }
function rowByText(t) { return qa('.or-row').find((b) => b.textContent.includes(t)); }

test('password armor → password field opens it', async () => {
  const root = await mintRoot();
  SERVER = { armor: await aPassword(root, 'pw'), rootPub: root.rootPub, passkeys: [] };
  const p = openRoot({ title: 'Approve X' });
  await until(() => q('.or-in'));
  assert.ok(q('.or-in'), 'password field shown');
  assert.equal(qa('.or-row').length, 0, 'no chooser for a single opener');
  q('.or-in').value = 'pw'; q('.or-in').dispatchEvent(new window.Event('input'));
  btnByText('Approve').click();
  const out = await p;
  assert.ok(out && out.rootPub === root.rootPub, 'resolved with the right root');
  assert.equal(bytesToHex(out.seed), bytesToHex(root.seed));
});

test('passkey-only armor → passkey button opens it (no password field)', async () => {
  const root = await mintRoot(); const c = 'dev-a';
  SERVER = { armor: await aPasskeyOnly(root, 'pw', c), rootPub: root.rootPub,
    passkeys: [{ credential_id: enc(c), rp_id: 'localhost', provisioning_public_key: await provPub(c) }] };
  const p = openRoot({ title: 'Approve X' });
  await until(() => btnByText('passkey'));
  assert.ok(!q('.or-in'), 'no password field for a passkey-only armor');
  btnByText('passkey').click();
  const out = await p;
  assert.ok(out && out.rootPub === root.rootPub);
  assert.equal(bytesToHex(out.seed), bytesToHex(root.seed));
});

test('password+passkey armor → chooser, pick passkey', async () => {
  const root = await mintRoot(); const c = 'dev-b';
  SERVER = { armor: await aBoth(root, 'pw', c), rootPub: root.rootPub,
    passkeys: [{ credential_id: enc(c), rp_id: 'localhost', provisioning_public_key: await provPub(c) }] };
  const p = openRoot({ title: 'Approve X' });
  await until(() => qa('.or-row').length >= 2);
  assert.equal(qa('.or-row').length, 2, 'both openers offered');
  rowByText('Passkey').click();
  await until(() => btnByText('passkey'));
  btnByText('passkey').click();
  const out = await p;
  assert.equal(bytesToHex(out.seed), bytesToHex(root.seed));
});

test('password+passkey armor → chooser, pick password', async () => {
  const root = await mintRoot(); const c = 'dev-c';
  SERVER = { armor: await aBoth(root, 'pw', c), rootPub: root.rootPub,
    passkeys: [{ credential_id: enc(c), rp_id: 'localhost', provisioning_public_key: await provPub(c) }] };
  const p = openRoot({ title: 'Approve X' });
  await until(() => qa('.or-row').length >= 2);
  rowByText('Password').click();
  await until(() => q('.or-in'));
  q('.or-in').value = 'pw'; q('.or-in').dispatchEvent(new window.Event('input'));
  btnByText('Approve').click();
  const out = await p;
  assert.equal(bytesToHex(out.seed), bytesToHex(root.seed));
});

test('MFA armor → requires BOTH password and passkey', async () => {
  const root = await mintRoot(); const c = 'dev-d';
  SERVER = { armor: await aMfa(root, 'pw', c), rootPub: root.rootPub,
    passkeys: [{ credential_id: enc(c), rp_id: 'localhost', provisioning_public_key: await provPub(c) }] };
  const p = openRoot({ title: 'Approve X' });
  await until(() => q('.or-in') && btnByText('passkey'));
  // Approve is disabled until both are provided
  assert.equal(btnByText('Approve').getAttribute('aria-disabled'), 'true');
  q('.or-in').value = 'pw'; q('.or-in').dispatchEvent(new window.Event('input'));
  btnByText('passkey').click();
  await until(() => { const b = btnByText('Approve'); return !!b && b.getAttribute('aria-disabled') === 'false'; });
  btnByText('Approve').click();
  const out = await p;
  assert.equal(bytesToHex(out.seed), bytesToHex(root.seed));
});

test('cancel resolves null', async () => {
  const root = await mintRoot();
  SERVER = { armor: await aPassword(root, 'pw'), rootPub: root.rootPub, passkeys: [] };
  const p = openRoot({ title: 'Approve X' });
  await until(() => qa('.or-cancel').length);
  qa('.or-cancel').pop().click();
  assert.equal(await p, null);
});
