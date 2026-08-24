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
