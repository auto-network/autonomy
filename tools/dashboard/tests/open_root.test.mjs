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

test('v3 password policy → password field opens it', async () => {
  const root = await mintRoot();
  SERVER = { armor: await aV3Password(root, 'pw'), rootPub: root.rootPub,
    passkeys: [], factorPolicy: v3PolicyView() };
  const p = openRoot({ title: 'Approve X' });
  await until(() => q('input[type="password"]'));
  assert.ok(q('input[type="password"]'), 'password field shown');
  q('input[type="password"]').value = 'pw';
  q('input[type="password"]').dispatchEvent(new window.Event('input'));
  q('.or-ok').click();
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

test('frozen vault root uses the supplied armor and no live identity reads', async () => {
  const {collectVaultOpeners,clearVaultOpeners}=await import('../static/js/ceremony/open-vault.js');
  const {createRootAnchorEnvelope}=await import('../static/js/ceremony/root-anchor.js');
  const root=await mintRoot();
  const signingKey=await primitivesSigningKey(root.seed);
  const anchor=await createRootAnchorEnvelope({...root,signingKey});
  const armor=await aV3Password(root,'frozen password');let state;
  const previousFetch=global.fetch;global.fetch=()=>{throw Error('must not read live identity');};
  try {
    const pending=collectVaultOpeners({v:2,root:{armor,armor_version:3,root_pub:root.rootPub,methods:['password'],passkeys:[]},anchor,
      governance:{form:'root-reachable',anchor_id:anchor.anchor_id}}, {view:value=>state=value});
    assert.ok(await until(()=>!!state));state.password('wrong');assert.ok(await until(()=>state.error));
    state.password('frozen password');const gathered=await pending;
    assert.deepEqual(Object.keys(gathered.openers),[anchor.anchor_id]);assert.equal(gathered.seeds[0].length,32);
    clearVaultOpeners(gathered);assert.ok(gathered.seeds[0].every(v=>v===0));
  } finally {global.fetch=previousFetch;root.seed.fill(0);}
});

async function primitivesSigningKey(seed) {
  const prefix=Buffer.from('302e020100300506032b657004220420','hex');
  return crypto.subtle.importKey('pkcs8',Buffer.concat([prefix,seed]),{name:'Ed25519'},false,['sign']);
}

test('shared view offers one passkey action and accepts the browser-selected alternative', async () => {
  const root = await mintRoot();
  const ids = ['old-phone', 'pc', 'phone'];
  const factors = [];
  for (const id of ids) factors.push({ factor_id: id, type: 'passkey', credential_id: enc(id),
    recipients: [{ recipient_public_key: await rootRecipientPub(id), label: id, created_at: '2026-09-12T00:00:00Z' }] });
  const policy = { op: 'or', children: ids.map(id => ({ op: 'factor', factor_id: id })) };
  SERVER = { rootPub: root.rootPub, passkeys: ids.map(id => ({ credential_id: enc(id), rp_id: 'localhost' })),
    factorPolicy: { armor_version: 3, factors: [] },
    armor: await buildFactorPolicyArmor({ rootSeed: root.seed, rootPub: root.rootPub, generation: 1, factors, access: ids, policy }) };
  const previous = navigator.credentials.get;
  let allowed;
  navigator.credentials.get = async ({ publicKey }) => {
    allowed = publicKey.allowCredentials.map(item => String.fromCharCode(...item.id));
    const prf = prfFor('phone');
    return { rawId: new TextEncoder().encode('phone').buffer,
      getClientExtensionResults: () => ({ prf: { results: { first: prf.buffer } } }) };
  };
  try {
    let state;
    const promise = openRoot({ view: value => { state = value; } });
    await until(() => state);
    await state.passkey();
    const opened = await promise;
    assert.deepEqual(allowed, ids);
    assert.equal(bytesToHex(opened.seed), bytesToHex(root.seed)); opened.seed.fill(0);
    assert.equal(document.querySelector('.or-overlay'), null);
  } finally { navigator.credentials.get = previous; }
});

test('embedded password verification clears and blurs input without reopening keyboard', async () => {
  const root = await mintRoot();
  SERVER = { armor: await aV3Password(root, 'pw'), rootPub: root.rootPub, passkeys: [], factorPolicy: v3PolicyView() };
  const mount = document.createElement('div'); document.body.append(mount);
  const promise = openRoot({ mount: () => mount });
  await until(() => mount.querySelector('input'));
  const input = mount.querySelector('input');
  assert.notEqual(document.activeElement, input);
  input.focus(); input.value = 'pw'; input.dispatchEvent(new window.Event('input'));
  mount.querySelector('.or-ok').click();
  assert.equal(mount.querySelector('input').value, '');
  assert.notEqual(document.activeElement.type, 'password');
  const opened = await promise;
  assert.equal(bytesToHex(opened.seed), bytesToHex(root.seed)); opened.seed.fill(0);
  assert.equal(mount.childElementCount, 0); assert.equal(mount.isConnected, true); mount.remove();
});

test('embedded Back during password verification settles once and leaves caller mount intact', async () => {
  const root = await mintRoot();
  SERVER = { armor: await aV3Password(root, 'pw'), rootPub: root.rootPub, passkeys: [], factorPolicy: v3PolicyView() };
  const mount = document.createElement('div'); document.body.append(mount);
  const abort = new AbortController();
  const promise = openRoot({ mount: () => mount, signal: abort.signal });
  await until(() => mount.querySelector('input'));
  const input = mount.querySelector('input'); input.value = 'pw'; input.dispatchEvent(new window.Event('input'));
  mount.querySelector('.or-ok').click(); abort.abort();
  assert.equal(await promise, null);
  await new Promise(resolve => setTimeout(resolve, 100));
  assert.equal(mount.childElementCount, 0); assert.equal(mount.isConnected, true); mount.remove();
});

for (const mode of ['passkey', 'or', 'and']) {
  test('embedded ' + mode + ' policy uses the existing root policy evaluator', async () => {
    const root = await mintRoot(), credential = 'embedded-' + mode;
    const made = await createPasswordFactor(root.rootPub, 'pw', 'pw', IT); made.seed.fill(0);
    const passkey = { factor_id: 'pk', type: 'passkey', credential_id: enc(credential),
      recipients: [{ recipient_public_key: await rootRecipientPub(credential), label: 'Test passkey', created_at: '2026-09-12T00:00:00Z' }] };
    const factors = mode === 'passkey' ? [passkey] : [made.factor, passkey];
    const policy = mode === 'passkey' ? { op: 'factor', factor_id: 'pk' }
      : { op: mode, children: [{ op: 'factor', factor_id: 'pw' }, { op: 'factor', factor_id: 'pk' }] };
    SERVER = { rootPub: root.rootPub, passkeys: [{ credential_id: enc(credential), rp_id: 'localhost' }],
      factorPolicy: { armor_version: 3, factors: [] },
      armor: await buildFactorPolicyArmor({ rootSeed: root.seed, rootPub: root.rootPub,
        generation: 1, factors, access: factors.map(f => f.factor_id), policy }) };
    const mount = document.createElement('div'); document.body.append(mount);
    let resolved = false;
    const promise = openRoot({ mount: () => mount }).then(value => { resolved = true; return value; });
    await until(() => mount.querySelector('.or-factor-btn'));
    if (mode !== 'passkey') {
      assert.equal(mount.querySelector('.approval-separator').textContent, mode.toUpperCase());
      const input = mount.querySelector('input'); input.value = 'pw'; input.dispatchEvent(new window.Event('input'));
      mount.querySelector('.or-ok').click();
      if (mode === 'and') {
        await until(() => mount.querySelector('.done'));
        assert.equal(resolved, false, 'one factor does not satisfy AND');
        mount.querySelector('.or-factor-btn').click();
      }
    } else mount.querySelector('.or-factor-btn').click();
    const opened = await promise;
    assert.equal(bytesToHex(opened.seed), bytesToHex(root.seed)); opened.seed.fill(0); mount.remove();
  });
}

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
  await until(() => q('input[type="password"]') && q('.or-ok'));
  q('input[type="password"]').value = 'pw';
  q('input[type="password"]').dispatchEvent(new window.Event('input'));
  q('.or-ok').click();
  await until(() => q('.or-factor-btn') && !q('.or-ok'));
  q('.or-factor-btn').click();
  const opened = await promise;
  assert.equal(bytesToHex(opened.seed), bytesToHex(root.seed));
});
