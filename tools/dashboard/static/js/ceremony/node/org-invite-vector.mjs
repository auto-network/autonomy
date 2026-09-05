#!/usr/bin/env node
// Cross-implementation vector for the browser mint ceremony (org-invite.js):
// run the EXACT module the Membership screen imports against a real server,
// so the browser chain is proven under node before any operator passkey
// touches it. Driven by test_org_invite_browser_ceremony.py.
//
// argv: --server URL --org SLUG --genesis HEX --role NAME --expiry MS
//       [--max-uses N]
// env:  AUTONOMY_PERSONAL_SEED_HEX — 32-byte personal root seed, hex.
import { webcrypto } from 'node:crypto';

if (!globalThis.crypto) globalThis.crypto = webcrypto;

const { mintOrgInvite } = await import('../org-invite.js');

function arg(name) {
  const index = process.argv.indexOf(name);
  return index === -1 ? undefined : process.argv[index + 1];
}

const seedHex = process.env.AUTONOMY_PERSONAL_SEED_HEX || '';
if (!/^[0-9a-f]{64}$/.test(seedHex)) {
  process.stderr.write('AUTONOMY_PERSONAL_SEED_HEX must be 64 hex chars\n');
  process.exit(2);
}
const seed = Uint8Array.from(seedHex.match(/../g).map((b) => parseInt(b, 16)));

try {
  const result = await mintOrgInvite({
    fetchImpl: globalThis.fetch,
    serverUrl: arg('--server'),
    org: arg('--org'),
    genesisId: arg('--genesis'),
    personalRootSeed: seed,
    role: arg('--role'),
    expiry: Number(arg('--expiry')),
    maxUses: arg('--max-uses') ? Number(arg('--max-uses')) : null,
  });
  process.stdout.write(JSON.stringify(result) + '\n');
} catch (error) {
  process.stderr.write('org-invite-vector: ' + (error.message || error) + '\n');
  process.exit(1);
}
