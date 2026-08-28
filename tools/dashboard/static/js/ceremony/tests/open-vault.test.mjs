import assert from 'node:assert/strict';
import test from 'node:test';

import {
  clearVaultOpeners,
  gatherVaultOpeners,
} from '../open-vault.js';

function b64u(bytes) {
  return Buffer.from(bytes).toString('base64url');
}

const passwordFactor = {
  factor_id: 'pw-1', type: 'password', armor: 'armor-one',
};
const credentialId = new Uint8Array([1, 2, 3, 4]);
const passkeyFactor = {
  factor_id: 'pk-1', type: 'passkey', credential_id: b64u(credentialId),
  rp_id: 'dashboard.example.test', transports: ['internal'],
};

test('both gathers both factor ids in one complete result', async () => {
  const passwordSeed = new Uint8Array(32).fill(0x11);
  const passkeySeed = new Uint8Array(32).fill(0x22);
  let assertionOptions;
  const gathered = await gatherVaultOpeners(
    { v: 1, policy: 'both', factors: [passwordFactor, passkeyFactor] },
    'correct password',
    {
      decryptArmor: async (armor, password) => {
        assert.equal(armor, 'armor-one');
        assert.equal(password, 'correct password');
        return { seed: passwordSeed };
      },
      credentials: {
        get: async (options) => {
          assertionOptions = options;
          return {
            rawId: credentialId,
            getClientExtensionResults: () => ({
              prf: { results: { first: passkeySeed.buffer } },
            }),
          };
        },
      },
      cryptoApi: { getRandomValues: (array) => array.fill(7) },
      currentHostname: 'dashboard.example.test',
    },
  );

  assert.deepEqual(Object.keys(gathered.openers).sort(), ['pk-1', 'pw-1']);
  assert.equal(gathered.openers['pw-1'], '11'.repeat(32));
  assert.equal(gathered.openers['pk-1'], '22'.repeat(32));
  assert.equal(
    Buffer.from(assertionOptions.publicKey.allowCredentials[0].id).toString('base64url'),
    passkeyFactor.credential_id,
  );
  assert.ok(assertionOptions.publicKey.extensions.prf.eval.first);
  assert.equal(assertionOptions.publicKey.rpId, 'dashboard.example.test');

  clearVaultOpeners(gathered);
  assert.ok(passwordSeed.every((byte) => byte === 0));
  assert.ok(passkeySeed.every((byte) => byte === 0));
  assert.deepEqual(gathered.openers, { 'pw-1': '', 'pk-1': '' });
});

test('a cancelled second factor posts no partial set and zeros the first', async () => {
  const passwordSeed = new Uint8Array(32).fill(0x44);
  const cancelled = new Error('cancel');
  cancelled.name = 'NotAllowedError';
  await assert.rejects(
    gatherVaultOpeners(
      { v: 1, policy: 'both', factors: [passwordFactor, passkeyFactor] },
      'correct password',
      {
        decryptArmor: async () => ({ seed: passwordSeed }),
        credentials: { get: async () => { throw cancelled; } },
        cryptoApi: { getRandomValues: (array) => array.fill(7) },
        currentHostname: 'dashboard.example.test',
      },
    ),
    /Passkey was cancelled/,
  );
  assert.ok(passwordSeed.every((byte) => byte === 0));
});

test('a passkey from another rp id is never offered', async () => {
  let called = false;
  await assert.rejects(
    gatherVaultOpeners(
      { v: 1, policy: 'prf', factors: [passkeyFactor] },
      '',
      {
        credentials: { get: async () => { called = true; } },
        cryptoApi: { getRandomValues: (array) => array },
        currentHostname: 'localhost',
      },
    ),
    /no passkey factor/,
  );
  assert.equal(called, false);
});

test('root-reachable ceremony requires an informed choice when root has alternatives', async () => {
  await assert.rejects(
    gatherVaultOpeners({
      v: 2,
      governance: {
        v: 1, form: 'root-reachable', anchor_id: 'anchor-1',
        display_name: 'Personal root vault',
      },
      anchor: { anchor_id: 'anchor-1', root_pub: 'ab'.repeat(32) },
      root: {
        armor: 'armor', root_pub: 'ab'.repeat(32),
        methods: ['password', 'passkey'], passkeys: [],
      },
    }, 'password', {
      decryptArmor: async () => { throw new Error('must not open'); },
      openAnchor: async () => { throw new Error('must not open'); },
    }),
    /Choose how to open your personal root/,
  );
});

test('v3 armor: MFA policy opens with password + passkey PRF and yields only the anchor opener', async () => {
  // Real v3 crypto end to end: a password factor and a passkey factor under an
  // AND policy (multi-factor), the armor built by buildFactorPolicyArmor, the
  // gatherer opening it via openFactorPolicyArmor with the typed password and
  // the asserted PRF — the exact live shape that 500'd on the operator's vault
  // request before the v3 support landed.
  const { createHash, createPrivateKey, createPublicKey } = await import('node:crypto');
  const rfp = await import('../root-factor-policy.js');
  const rootSeed = new Uint8Array(32).fill(0x31);
  const pkcs8 = Buffer.concat([
    Buffer.from('302e020100300506032b657004220420', 'hex'), Buffer.from(rootSeed),
  ]);
  const priv = createPrivateKey({ key: pkcs8, format: 'der', type: 'pkcs8' });
  const jwk = createPublicKey(priv).export({ format: 'jwk' });
  const rootPub = Buffer.from(jwk.x, 'base64url').toString('hex');

  const made = await rfp.createPasswordFactor(rootPub, 'pw.1', 'root password', 10000);
  const prfSeed = new Uint8Array(32).fill(0x22);
  const prfRecipient = await (await import('../sealing.js')).deriveEncapsulationKeypair(
    prfSeed, rfp.FACTOR_RECIPIENT_PURPOSE,
  );
  const pkFactor = {
    factor_id: 'pk.1',
    type: 'passkey',
    credential_id: b64u(credentialId),
    recipients: [{
      recipient_public_key: prfRecipient.publicKeyHex,
      label: 'Test device', created_at: '2026-08-27T00:00:00Z',
    }],
  };
  const armor = await rfp.buildFactorPolicyArmor({
    rootSeed, rootPub, generation: 1,
    factors: [made.factor, pkFactor],
    access: ['pw.1', 'pk.1'],
    policy: {
      op: 'and',
      children: [
        { op: 'factor', factor_id: 'pw.1' },
        { op: 'factor', factor_id: 'pk.1' },
      ],
    },
  });

  const anchorSeed = new Uint8Array(32).fill(0x52);
  let openedWithRoot = null;
  const ceremony = {
    v: 2,
    governance: {
      v: 1, form: 'root-reachable', anchor_id: 'personal-root-anchor-1',
      display_name: 'Personal root vault',
    },
    anchor: {
      v: 1, anchor_id: 'personal-root-anchor-1', root_pub: rootPub,
      public_key: 'cd'.repeat(32), sealed_seed: { ciphertext: 'unused' },
      display_name: 'Personal root vault', created_at: '2026-08-24T00:00:00Z',
      signature: 'ef'.repeat(64),
    },
    root: {
      armor, armor_version: 3, root_pub: rootPub, methods: ['both'],
      passkeys: [{
        credential_id: b64u(credentialId), label: 'Test device',
        rp_id: 'dashboard.example.test', transports: ['internal'],
      }],
    },
  };
  const gathered = await gatherVaultOpeners(ceremony, 'root password', {
    rootMethod: 'both',
    credentials: {
      get: async () => ({
        rawId: credentialId,
        getClientExtensionResults: () => ({
          prf: { results: { first: prfSeed.buffer.slice(0) } },
        }),
      }),
    },
    cryptoApi: { getRandomValues: (array) => array.fill(7) },
    currentHostname: 'dashboard.example.test',
    openAnchor: async (anchor, suppliedRootSeed) => {
      openedWithRoot = Buffer.from(suppliedRootSeed).toString('hex');
      return anchorSeed;
    },
  });

  assert.equal(openedWithRoot, Buffer.from(rootSeed).toString('hex'),
    'the v3 armor opened to the real root seed');
  assert.deepEqual(Object.keys(gathered.openers), ['personal-root-anchor-1'],
    'only the anchor opener leaves the gatherer');
  assert.equal(gathered.openers['personal-root-anchor-1'], '52'.repeat(32));
});
