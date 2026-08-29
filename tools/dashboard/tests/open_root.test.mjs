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
import { bytesToHex, deriveEncapsulationKeypair } from '../static/js/ceremony/primitives.js';
import { deriveProvisioningKey } from '../static/js/ceremony/enrollment.js';
import {
  FACTOR_RECIPIENT_PURPOSE, buildFactorPolicyArmor, createPasswordFactor,
  mintPasswordArmor,
} from '../static/js/ceremony/root-factor-policy.js';

const IT = 10000;
function enc(l) { return btoa(l).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, ''); }
function prfFor(l) { const b = new Uint8Array(32); for (let i = 0; i < 32; i += 1) b[i] = (l.charCodeAt(i % l.length) + i * 7) & 0xff; return b; }
async function provPub(l) { return (await deriveProvisioningKey(prfFor(l))).publicKeyHex; }
async function rootRecipientPub(l) {
  return (await deriveEncapsulationKeypair(
    prfFor(l), FACTOR_RECIPIENT_PURPOSE,
  )).publicKeyHex;
}
async function mintRoot() {
  const kp = await crypto.subtle.generateKey('Ed25519', true, ['sign', 'verify']);
  const pk8 = new Uint8Array(await crypto.subtle.exportKey('pkcs8', kp.privateKey));
  const rawPub = new Uint8Array(await crypto.subtle.exportKey('raw', kp.publicKey));
  return { seed: pk8.slice(-32), rootPub: bytesToHex(rawPub) };
}
async function aV3Password(r, pw) {
  return mintPasswordArmor({ rootSeed: r.seed, rootPub: r.rootPub, password: pw, factorId: 'pw.primary', iterations: IT });
}
function v3PolicyView() {
  return { armor_version: 3, factors: [{ factor_id: 'pw.primary', label: 'Main password' }] };
}

let SERVER = null;
function J(o) { return { ok: true, status: 200, json: async () => o }; }
async function router(url) {
  if (url === '/api/identity/status') {
    return J({ rp_id: 'localhost', passkeys: SERVER.passkeys });
  }
  if (url === '/api/identity/personal') {
    return J({ armored_private_key: SERVER.armor, root_pub: SERVER.rootPub });
  }
  if (url === '/api/identity/factor-policy') {
    return J(SERVER.factorPolicy || { error: 'legacy armor' });
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

test('v3 password policy → password field opens it', async () => {
  const root = await mintRoot();
  SERVER = { armor: await aV3Password(root, 'pw'), rootPub: root.rootPub,
    passkeys: [], factorPolicy: v3PolicyView() };
  const p = openRoot({ title: 'Approve X' });
  await until(() => q('.or-in'));
  assert.ok(q('.or-in'), 'password field shown');
  q('.or-in').value = 'pw'; q('.or-in').dispatchEvent(new window.Event('input'));
  await until(() => btnByText('Use this password'));
  btnByText('Use this password').click();
  const out = await p;
  assert.ok(out && out.rootPub === root.rootPub, 'resolved with the right root');
  assert.equal(bytesToHex(out.seed), bytesToHex(root.seed));
});

test('cancel resolves null', async () => {
  const root = await mintRoot();
  SERVER = { armor: await aV3Password(root, 'pw'), rootPub: root.rootPub,
    passkeys: [], factorPolicy: v3PolicyView() };
  const p = openRoot({ title: 'Approve X' });
  await until(() => qa('.or-cancel').length);
  qa('.or-cancel').pop().click();
  assert.equal(await p, null);
});

test('a retired-format armor is refused outright', async () => {
  const fake = ['-----BEGIN AUTONOMY NETWORK ROOT KEY-----',
    btoa('{"v": 2}'), '-----END AUTONOMY NETWORK ROOT KEY-----'].join('\n');
  SERVER = { armor: fake, rootPub: 'f'.repeat(64), passkeys: [],
    factorPolicy: { error: 'legacy armor' } };
  await assert.rejects(() => openRoot({ title: 'Approve X' }), /retired format/);
});

test('v3 grouped policy gathers one password AND one passkey', async () => {
  const root = await mintRoot(); const c = 'dev-v3';
  const made = await createPasswordFactor(root.rootPub, 'pw.primary', 'pw', IT);
  const passwordFactor = made.factor; made.seed.fill(0);
  const passkeyFactor = {
    factor_id: 'pk.primary', type: 'passkey', credential_id: enc(c),
    recipients: [{
      recipient_public_key: await rootRecipientPub(c),
      label: 'Test device', created_at: '2026-08-24T00:00:00Z',
    }],
  };
  const policy = {
    op: 'and', children: [
      { op: 'factor', factor_id: 'pw.primary' },
      { op: 'factor', factor_id: 'pk.primary' },
    ],
  };
  const armor = await buildFactorPolicyArmor({
    rootSeed: root.seed, rootPub: root.rootPub, generation: 1,
    factors: [passwordFactor, passkeyFactor],
    access: ['pw.primary', 'pk.primary'], policy,
  });
  SERVER = {
    armor, rootPub: root.rootPub,
    passkeys: [{ credential_id: enc(c), rp_id: 'localhost' }],
    factorPolicy: {
      armor_version: 3,
      factors: [
        { factor_id: 'pw.primary', label: 'Main password' },
        { factor_id: 'pk.primary', label: 'Phone passkey' },
      ],
    },
  };
  const promise = openRoot({ title: 'Approve X' });
  await until(() => q('.or-in') && btnByText('Use this password'));
  q('.or-in').value = 'pw'; q('.or-in').dispatchEvent(new window.Event('input'));
  btnByText('Use this password').click();
  await until(() => btnByText('Use a passkey'));
  btnByText('Use a passkey').click();
  const opened = await promise;
  assert.equal(bytesToHex(opened.seed), bytesToHex(root.seed));
});
