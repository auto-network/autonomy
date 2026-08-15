/*
 * The whole browser founding ceremony, start to finish, over HTTP.
 *
 * Three calls and no passphrase: ask for an organization shell, mint and seal
 * the organization root locally and submit only the sealed material, then
 * fold the four events this client signed. Every secret involved -- the
 * personal root seed, the organization root -- stays in this process.
 *
 * usage: node founding-e2e.mjs FIXTURE.json
 */
import { readFile } from 'node:fs/promises';

import {
  buildFoundingBatch,
  generateSealedOrgRoot,
} from '../founding.js';
import { hexToBytes } from '../primitives.js';

const fixture = JSON.parse(await readFile(process.argv[2], 'utf8'));
const base = fixture.server_url;
const wire = [];

async function call(route, body) {
  const response = await fetch(new URL(route, base), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const text = await response.text();
  // Every byte this client sends, recorded for the wire-absence assertion.
  wire.push({ route, sent: JSON.stringify(body), status: response.status });
  if (!response.ok) {
    throw new Error(`${route} failed ${response.status}: ${text}`);
  }
  return text ? JSON.parse(text) : null;
}

const personalRootSeed = hexToBytes(fixture.personal_root_seed_hex);

// 1 — the shell. This is what mints the stable id genesis must bind.
const shell = await call('/api/orgs', {
  slug: fixture.org,
  type: 'shared',
  identity: { name: fixture.org, type: 'shared' },
});

// 2 — mint the organization root here, seal it to this owner, submit sealed.
const { rootPub, rootSigningKey, sealedOrgKey } = await generateSealedOrgRoot({
  personalRootSeed,
});
await call('/api/network/org-key/sealed', { org: fixture.org, ...sealedOrgKey });

// 3 — sign the four constitutional events and fold them.
const batch = await buildFoundingBatch({
  orgId: shell.org.id,
  rootPub,
  rootSigningKey,
  personalRootSeed,
  now: fixture.now,
});
const folded = await call('/api/network/ledger/found', {
  org: fixture.org,
  events: batch.wires,
});

process.stdout.write(JSON.stringify({
  org_id: shell.org.id,
  founded_flag: shell.founded,
  root_pub: rootPub,
  sealed_org_key: sealedOrgKey,
  genesis_id: batch.genesisId,
  founder_persona_pub: batch.founderPersonaPub,
  event_ids: folded.event_ids,
  wire,
}));
