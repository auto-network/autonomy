import assert from 'node:assert/strict';
import { webcrypto as crypto } from 'node:crypto';
import { readFile } from 'node:fs/promises';

import * as primitives from '../primitives.js';

const {
  b64ToBytes,
  bytesToHex,
  canonicalJson,
  decryptArmor,
  domainBytes,
  hexToBytes,
  importEd25519RootSigningKey,
} = primitives;

const fixturePath = process.argv[2];
if (!fixturePath) {
  throw new Error('usage: node primitives.test.mjs FIXTURE.json');
}
const fixture = JSON.parse(await readFile(fixturePath, 'utf8'));

assert.equal(
  'importRootKey' in primitives,
  false,
  'ambiguous Ed25519/X25519 key import must not be exported',
);

const opened = await decryptArmor(fixture.armor, fixture.passphrase);
assert.equal(bytesToHex(opened.seed), fixture.seed_hex);
assert.equal(opened.rootPub, fixture.root_pub);

await assert.rejects(
  decryptArmor(fixture.armor, 'wrong-passphrase'),
  /wrong passphrase/,
);

function tamperCiphertext(armor) {
  const lines = armor.split('\n').filter((line) => line.length > 0);
  const data = JSON.parse(
    Buffer.from(lines.slice(1, -1).join(''), 'base64').toString('utf8'),
  );
  const ciphertext = Buffer.from(data.ct, 'base64');
  ciphertext[0] ^= 0x01;
  data.ct = ciphertext.toString('base64');
  const body = Buffer.from(JSON.stringify(data)).toString('base64');
  const wrapped = body.match(/.{1,64}/g);
  return [lines[0], ...wrapped, lines[lines.length - 1]].join('\n');
}

await assert.rejects(
  decryptArmor(tamperCiphertext(fixture.armor), fixture.passphrase),
  /wrong passphrase/,
);

for (const vector of fixture.canonical_vectors) {
  assert.equal(
    bytesToHex(new TextEncoder().encode(canonicalJson(vector.value))),
    vector.canonical_hex,
    vector.name,
  );
}

assert.deepEqual(
  hexToBytes(fixture.seed_hex),
  opened.seed,
);
assert.equal(bytesToHex(b64ToBytes('AAECAw==')), '00010203');

const signingCanonical = canonicalJson(fixture.signing.payload);
assert.equal(
  bytesToHex(new TextEncoder().encode(signingCanonical)),
  fixture.signing.canonical_hex,
);
const signingMessage = domainBytes(
  fixture.signing.domain,
  signingCanonical,
);
assert.equal(bytesToHex(signingMessage), fixture.signing.message_hex);

const signingKey = await importEd25519RootSigningKey(opened.seed);
assert.equal(signingKey.type, 'private');
assert.equal(signingKey.extractable, false);
assert.equal(signingKey.algorithm.name, 'Ed25519');
assert.deepEqual(signingKey.usages, ['sign']);

const signature = new Uint8Array(await crypto.subtle.sign(
  'Ed25519',
  signingKey,
  signingMessage,
));
assert.equal(
  bytesToHex(signature),
  fixture.signing.python_signature_hex,
  'Ed25519 signatures must match Python idkit byte for byte',
);

const verificationKey = await crypto.subtle.importKey(
  'raw',
  hexToBytes(fixture.root_pub),
  { name: 'Ed25519' },
  false,
  ['verify'],
);
assert.equal(
  await crypto.subtle.verify(
    'Ed25519',
    verificationKey,
    signature,
    signingMessage,
  ),
  true,
);

opened.seed.fill(0);
console.log('ceremony primitives: Python/Node parity verified');
