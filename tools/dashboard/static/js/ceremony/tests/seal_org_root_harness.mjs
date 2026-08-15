/*
 * Produce a browser-sealed organization root for the Python side to open.
 *
 * The browser is the only place an organization root is ever generated in the
 * clear, so the acceptance runs in that direction: this harness mints and
 * seals exactly as the founding ceremony does, and Python proves it can
 * recover the identical root from the sealed payload alone.
 *
 * usage: node seal_org_root_harness.mjs PERSONAL_SEED_HEX OUT.json
 */
import { writeFile } from 'node:fs/promises';

import { generateSealedOrgRoot, ORG_ROOT_ARMOR_PURPOSE } from '../founding.js';

const [seedHex, outPath] = process.argv.slice(2);
if (!seedHex || !outPath) {
  throw new Error('usage: node seal_org_root_harness.mjs PERSONAL_SEED_HEX OUT.json');
}

const personalRootSeed = Uint8Array.from(
  seedHex.match(/.{2}/g).map((b) => parseInt(b, 16)),
);

const { rootPub, rootSigningKey, sealedOrgKey } = await generateSealedOrgRoot({
  personalRootSeed,
});

// The signing key must be usable for the founding batch in the same ceremony,
// so prove it signs rather than merely existing.
const signature = new Uint8Array(await crypto.subtle.sign(
  'Ed25519', rootSigningKey, new TextEncoder().encode('founding-batch-probe'),
));
const verifyKey = await crypto.subtle.importKey(
  'raw',
  Uint8Array.from(rootPub.match(/.{2}/g).map((b) => parseInt(b, 16))),
  { name: 'Ed25519' },
  false,
  ['verify'],
);
const signatureVerifies = await crypto.subtle.verify(
  'Ed25519', verifyKey, signature, new TextEncoder().encode('founding-batch-probe'),
);

await writeFile(outPath, JSON.stringify({
  root_pub: rootPub,
  sealed_org_key: sealedOrgKey,
  purpose: ORG_ROOT_ARMOR_PURPOSE,
  signature_verifies: signatureVerifies,
}, null, 2));
