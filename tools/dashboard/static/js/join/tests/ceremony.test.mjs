/* The acceptance-ceremony seam's orchestration and its I1 guarantees, proven
 * with injected fakes: the passphrase opens the armor and is never returned or
 * sent, the root seed and kem seed are zeroed on every path (including a thrown
 * mint), and only the signed public claim + the invitee's own kem key come
 * back. The real crypto is covered by ceremony/claim.js's own tests.
 * Run: node ceremony.test.mjs
 */
import assert from 'node:assert/strict';

import { makeCeremony } from '../ceremony.js';

const INVITE = 'e'.repeat(64);
const BEARER = 'bearer-token-xyz';
const PERSONA = 'f'.repeat(64);
const CLAIMKEY = 'c'.repeat(64);
const INPUTS = { org: 'o', inviteRef: INVITE, bearer: BEARER };
const CONTEXT = { orgSlug: 'o', genesisId: 'b'.repeat(64) };

function fakes(over = {}) {
  const openedSeed = new Uint8Array(32).fill(7);
  const kemSeed = new Uint8Array(32).fill(9);
  const seen = {};
  const base = {
    fetchPersonal: async () => {
      seen.fetched = true;
      return { armored_private_key: 'ARMOR' };
    },
    decryptArmor: async (armor, passphrase) => {
      seen.armor = armor;
      seen.passphrase = passphrase;
      return { seed: openedSeed };
    },
    randomSeed: () => kemSeed,
    mintMemberClaim: async (args) => {
      seen.mintArgs = args;
      return {
        event: { kind: 'member.claim' },
        wire: '{"kind":"member.claim"}',
        claimKey: CLAIMKEY,
        personaPub: PERSONA,
        kemCredential: { c: 1 },
        kemPrivateKey: { k: 1 },
      };
    },
  };
  return { deps: { ...base, ...over }, openedSeed, kemSeed, seen };
}

// 1. Happy path: fetch -> decrypt(passphrase) -> mint(seed, invite, bearer,
//    kemSeed) -> return signed claim + kem key; passphrase never comes back.
{
  const { deps, openedSeed, kemSeed, seen } = fakes();
  const runCeremony = makeCeremony(deps);
  const out = await runCeremony({ context: CONTEXT, inputs: INPUTS, passphrase: 'open sesame' });

  assert.equal(seen.fetched, true);
  assert.equal(seen.armor, 'ARMOR');
  assert.equal(seen.passphrase, 'open sesame');
  assert.equal(seen.mintArgs.inviteRef, INVITE);
  assert.equal(seen.mintArgs.token, BEARER);
  assert.equal(seen.mintArgs.personalRootSeed, openedSeed);
  assert.equal(seen.mintArgs.kemSeed, kemSeed);
  assert.equal(seen.mintArgs.context, CONTEXT);

  assert.deepEqual(out.event, { kind: 'member.claim' });
  assert.equal(out.personaPub, PERSONA);
  assert.equal(out.claimKey, CLAIMKEY);
  assert.deepEqual(out.kemPrivateKey, { k: 1 });
  // no passphrase, seed, or armor anywhere in the result
  assert.equal(JSON.stringify(out).includes('open sesame'), false);
  assert.equal('seed' in out, false);
  // both seeds zeroed after the ceremony
  assert.equal(openedSeed.every((b) => b === 0), true, 'root seed must be zeroed');
  assert.equal(kemSeed.every((b) => b === 0), true, 'kem seed must be zeroed');
}

// 2. A bearer-less (key-bound) invite passes token: null, not "".
{
  const { deps, seen } = fakes();
  const runCeremony = makeCeremony(deps);
  await runCeremony({ context: CONTEXT, inputs: { inviteRef: INVITE }, passphrase: 'p' });
  assert.equal(seen.mintArgs.token, null);
}

// 3. No personal identity on the device -> refuse before touching a passphrase.
{
  const { deps } = fakes({ fetchPersonal: async () => ({}) });
  const runCeremony = makeCeremony(deps);
  await assert.rejects(
    runCeremony({ context: CONTEXT, inputs: INPUTS, passphrase: 'p' }),
    /no personal identity/,
  );
}

// 4. An empty passphrase is refused up front.
{
  const { deps } = fakes();
  const runCeremony = makeCeremony(deps);
  await assert.rejects(
    runCeremony({ context: CONTEXT, inputs: INPUTS, passphrase: '' }),
    /passphrase is required/,
  );
}

// 5. A thrown mint STILL zeroes the root seed (no seed left in memory on error).
{
  const { deps, openedSeed, kemSeed } = fakes({
    mintMemberClaim: async () => {
      throw new Error('mint blew up');
    },
  });
  const runCeremony = makeCeremony(deps);
  await assert.rejects(
    runCeremony({ context: CONTEXT, inputs: INPUTS, passphrase: 'p' }),
    /mint blew up/,
  );
  assert.equal(openedSeed.every((b) => b === 0), true, 'root seed zeroed on error');
  assert.equal(kemSeed.every((b) => b === 0), true, 'kem seed zeroed on error');
}

// 6. A faulting randomSeed (acquired after the armor is opened) still zeroes
//    the root seed — the acquisitions live inside the try (review finding a).
{
  const { deps, openedSeed } = fakes({
    randomSeed: () => {
      throw new Error('rng failed');
    },
  });
  const runCeremony = makeCeremony(deps);
  await assert.rejects(
    runCeremony({ context: CONTEXT, inputs: INPUTS, passphrase: 'p' }),
    /rng failed/,
  );
  assert.equal(openedSeed.every((b) => b === 0), true, 'root seed zeroed when rng faults');
}

console.log('ceremony: all assertions passed');
