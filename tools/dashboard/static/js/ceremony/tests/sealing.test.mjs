import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

import {
  SUITE_X25519_HKDF_SHA256_CHACHA20POLY1305,
  deriveEncapsulationKeypair,
  openSealedArmor,
  sealToEncapsulationKey,
} from '../sealing.js';
import * as sharedPrimitives from '../primitives.js';

function fromHex(value) {
  return Uint8Array.from(
    value.match(/.{2}/g) ?? [],
    (pair) => Number.parseInt(pair, 16),
  );
}

function toHex(value) {
  return Buffer.from(value).toString('hex');
}

async function rejects(action, pattern) {
  await assert.rejects(action, pattern);
}

const vector = JSON.parse(
  await readFile(new URL('./sealing-vector.json', import.meta.url), 'utf8'),
);
const fixturePath = process.argv[2];
assert.ok(fixturePath, 'Python parity fixture path is required');
const fixture = JSON.parse(await readFile(fixturePath, 'utf8'));
const seed = fromHex(vector.seed_hex);

assert.equal(SUITE_X25519_HKDF_SHA256_CHACHA20POLY1305, 1);
assert.equal(
  sharedPrimitives.deriveEncapsulationKeypair,
  deriveEncapsulationKeypair,
  'the shared ceremony surface must export the role-specific key derivation',
);
assert.equal(sharedPrimitives.openSealedArmor, openSealedArmor);

const keypair = await deriveEncapsulationKeypair(seed, vector.purpose);
assert.deepEqual(
  keypair,
  {
    privateKeyHex: vector.private_key_hex,
    publicKeyHex: vector.public_key_hex,
  },
  'JS must derive the frozen Python X25519 keypair byte for byte',
);

const openedFrozen = await openSealedArmor(
  {
    sealed_root_key: vector.record_hex,
    seal_purpose: vector.purpose,
  },
  seed,
);
assert.equal(toHex(openedFrozen), vector.plaintext_hex);

const openedPython = await openSealedArmor(
  {
    sealed_root_key: fixture.python_record_hex,
    seal_purpose: vector.purpose,
  },
  seed,
);
assert.equal(toHex(openedPython), fixture.plaintext_hex);

const jsRecord = await sealToEncapsulationKey(
  fromHex(fixture.plaintext_hex),
  vector.public_key_hex,
  vector.purpose,
);

await rejects(
  () => openSealedArmor(
    {
      sealed_root_key: vector.record_hex,
      seal_purpose: 'content-key.v1',
    },
    seed,
  ),
  /failed to open revision-2 armor/,
);

const wrongSuite = `02${vector.record_hex.slice(2)}`;
await rejects(
  () => openSealedArmor(
    { sealed_root_key: wrongSuite, seal_purpose: vector.purpose },
    seed,
  ),
  /unrecognized sealing suite/,
);

await rejects(
  () => openSealedArmor(
    {
      sealed_root_key: vector.record_hex.slice(0, 48 * 2),
      seal_purpose: vector.purpose,
    },
    seed,
  ),
  /sealed record is truncated/,
);

await rejects(
  () => openSealedArmor(
    {
      sealed_root_key: vector.record_hex.slice(0, -2),
      seal_purpose: vector.purpose,
    },
    seed,
  ),
  /failed to open revision-2 armor/,
);

process.stdout.write(JSON.stringify({
  opened_python_hex: toHex(openedPython),
  js_record_hex: toHex(jsRecord),
}));
