import { readFile } from 'node:fs/promises';

import {
  buildFoundingBatch,
  foundOrganization,
} from '../founding.js';
import {
  hexToBytes,
  importEd25519RootSigningKey,
} from '../primitives.js';

const fixturePath = process.argv[2];
if (!fixturePath) {
  throw new Error('usage: node founding-live.mjs FIXTURE.json');
}
const fixture = JSON.parse(await readFile(fixturePath, 'utf8'));
const rootSigningKey = await importEd25519RootSigningKey(
  hexToBytes(fixture.root_seed_hex),
);
const inputs = {
  orgId: fixture.org_id,
  rootPub: fixture.root_pub,
  rootSigningKey,
  personalRootSeed: hexToBytes(fixture.personal_root_seed_hex),
  now: fixture.now,
  kemSeed: fixture.kem_seed_hex
    ? hexToBytes(fixture.kem_seed_hex)
    : null,
};

let result;
if (fixture.server_url) {
  result = await foundOrganization({
    org: fixture.org,
    transport: {
      fetch(route, options) {
        return fetch(new URL(route, fixture.server_url), options);
      },
    },
    ...inputs,
  });
} else {
  result = await buildFoundingBatch(inputs);
}
process.stdout.write(JSON.stringify(result));
