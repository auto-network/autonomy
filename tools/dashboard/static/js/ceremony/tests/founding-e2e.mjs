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
  foundExistingOrganizationShell,
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

async function trackedFetch(route, options) {
  const response = await fetch(new URL(route, base), options);
  wire.push({ route, sent: options.body || '', status: response.status });
  return response;
}

const personalRootSeed = hexToBytes(fixture.personal_root_seed_hex);

// 1 — the shell. This is what mints the stable id genesis must bind.
const shell = await call('/api/orgs', {
  slug: fixture.org,
  type: 'shared',
  identity: { name: fixture.org, type: 'shared' },
});

// 2 + 3 — the shared production sequence: unlock, seal, store, and found.
const founded = await foundExistingOrganizationShell({
  org: fixture.org,
  orgId: shell.org.id,
  openRoot: async () => ({ seed: personalRootSeed }),
  transport: { fetch: trackedFetch },
  now: fixture.now,
});

process.stdout.write(JSON.stringify({
  org_id: shell.org.id,
  founded_flag: shell.founded,
  root_pub: founded.rootPub,
  sealed_org_key: founded.sealedOrgKey,
  genesis_id: founded.genesisId,
  founder_persona_pub: founded.founderPersonaPub,
  event_ids: founded.server.event_ids,
  wire,
}));
