import fs from 'node:fs';

import { verifyDelegationCert } from '../relaykit-core.js';

const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const leaf = await verifyDelegationCert(
  fixture.cert_wire,
  fixture.root_pub,
  fixture.org,
  fixture.now,
);
process.stdout.write(JSON.stringify({
  child_pub: leaf.child_pub,
  scope: leaf.scope,
  subject: leaf.subject,
  org: leaf.org,
}));
