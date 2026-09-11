// Real claim creation and independent later-sign-in derivation; no crypto fakes.
import assert from 'node:assert/strict';
import { makeCeremony, makeRootCeremony } from '../ceremony.js';
import { deriveKemSeed } from '../../ceremony/founding.js';
import { deriveEncapsulationKeypair } from '../../ceremony/primitives.js';

const root = new Uint8Array(32).fill(37); // Disposable test identity only.
const genesis = 'ab'.repeat(32);
const inputs = { inviteRef: 'cd'.repeat(32) };
const context = { genesisId: genesis, orgSlug: 'test-org', heads: [genesis],
  maxHlc: [1, 0], transport: { fetch: () => { throw new Error('network during root work'); } } };
const opened = [];
const open = async () => { const seed = new Uint8Array(root); opened.push(seed); return { seed }; };
const run = process.argv[2] === 'root'
  ? makeRootCeremony({ openRoot: open })
  : makeCeremony({ fetchPersonal: async () => ({ armored_private_key: 'fixture' }), decryptArmor: open });
const args = { context, inputs, passphrase: 'test-only' };
const first = await run(args);
const laterSeed = await deriveKemSeed(root, 0);
const later = await deriveEncapsulationKeypair(laterSeed, 'autonomy/persona-kem/v1/' + genesis);
laterSeed.fill(0);
assert.equal(first.kemCredential.kem_public_key, later.publicKeyHex,
  'invitation credential must match independent root-based sign-in derivation');
assert.equal(first.kemPrivateKey, later.privateKeyHex);
// A new ceremony, including the finalize path, must preserve the key identity.
const finalized = await run({ ...args, position: { parents: [genesis], hlc: [2, 0] } });
assert.equal(finalized.kemCredential.kem_public_key, first.kemCredential.kem_public_key);
assert.equal(finalized.kemPrivateKey, first.kemPrivateKey);
assert.ok(opened.every(seed => seed.every(byte => byte === 0)));
root.fill(0);
// Private TEST material is captured on a pipe for Python's real grant-open test.
process.stdout.write(JSON.stringify({ credential: first.kemCredential, private: later.privateKeyHex }));
