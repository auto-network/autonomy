import assert from 'node:assert/strict';
import { webcrypto as crypto } from 'node:crypto';
import { readFile } from 'node:fs/promises';

import * as primitives from '../primitives.js';

const {
  b64ToBytes,
  bytesToHex,
  canonicalJson,
  decryptArmor,
  armorVersion,
  parseArmor,
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

// Flip one bit of the seal that carries the seed. Both slots are AEAD, so a
// tampered seal must be indistinguishable from a wrong password — a caller
// that could tell them apart would learn whether the password was right.
function tamperCiphertext(armor) {
  const lines = armor.split('\n').filter((line) => line.length > 0);
  const data = JSON.parse(
    Buffer.from(lines.slice(1, -1).join(''), 'base64').toString('utf8'),
  );
  const sealed = Buffer.from(data.kek_seal.ct, 'base64');
  sealed[0] ^= 0x01;
  data.kek_seal.ct = sealed.toString('base64');
  const body = Buffer.from(JSON.stringify(data)).toString('base64');
  const wrapped = body.match(/.{1,64}/g);
  return [lines[0], ...wrapped, lines[lines.length - 1]].join('\n');
}

// Tampering the seed seal is refused even with the RIGHT password, and it
// reports a different cause than a wrong one. That is not a leak: telling the
// two apart requires having already opened the factor, which requires the
// password. It is worth distinguishing, because "your password is wrong" and
// "this blob has been altered" call for different actions from the operator.
await assert.rejects(
  decryptArmor(tamperCiphertext(fixture.armor), fixture.passphrase),
  /does not open the seed seal/,
);

