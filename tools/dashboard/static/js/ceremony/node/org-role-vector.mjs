#!/usr/bin/env node
// Cross-implementation vector for the browser role ceremonies (org-role.js):
// run the EXACT module the Roles editor imports against a real server, so
// the root-unseal + sign chain is proven under node before any operator
// passkey touches it. Driven by test_org_role_browser_ceremony.py.
//
// argv: --server URL --org SLUG --action define|grant|revoke
//       define: --name NAME [--scopes a,b] [--requires self|sponsor|admin-ack]
//               [--version N] [--threshold N]
//       grant/revoke: --genesis HEX --persona HEX --role NAME
// env:  AUTONOMY_PERSONAL_SEED_HEX — 32-byte personal root seed, hex.
import { webcrypto } from 'node:crypto';

if (!globalThis.crypto) globalThis.crypto = webcrypto;

const { defineRole, grantRole, revokeRole } = await import('../org-role.js');

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

const common = {
  fetchImpl: globalThis.fetch,
  serverUrl: arg('--server'),
  org: arg('--org'),
  personalRootSeed: seed,
};

try {
  const action = arg('--action');
  let result;
  if (action === 'define') {
    result = await defineRole({
      ...common,
      name: arg('--name'),
      scopeSet: arg('--scopes') ? arg('--scopes').split(',').filter(Boolean) : [],
      claimRequires: arg('--requires') || 'admin-ack',
      version: arg('--version') ? Number(arg('--version')) : null,
      approverThreshold: arg('--threshold') ? Number(arg('--threshold')) : null,
    });
  } else if (action === 'grant' || action === 'revoke') {
    const run = action === 'grant' ? grantRole : revokeRole;
    result = await run({
      ...common,
      genesisId: arg('--genesis'),
      persona: arg('--persona'),
      role: arg('--role'),
    });
  } else {
    throw new Error('--action must be define, grant, or revoke');
  }
  process.stdout.write(JSON.stringify(result) + '\n');
} catch (error) {
  process.stdout.write(JSON.stringify({
    error: error.message || String(error),
    status: error.status || null,
    reason: error.reason || null,
  }) + '\n');
  process.exit(1);
}
