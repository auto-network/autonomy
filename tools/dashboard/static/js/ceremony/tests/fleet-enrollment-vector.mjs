import fs from 'node:fs';
import { mintFleetEnrollmentEvidence } from '../fleet-enrollment.js';

const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const seed = Uint8Array.from(Buffer.from(fixture.personal_root_seed, 'hex'));
const evidence = await mintFleetEnrollmentEvidence({
  personalRootSeed: seed,
  rootPub: fixture.root_pub,
  request: fixture.request,
  channelBinding: fixture.channel_binding,
  issuedAt: fixture.issued_at,
  seq: fixture.seq,
});
process.stdout.write(JSON.stringify({
  ...evidence,
  root_seed_zeroed: seed.every((value) => value === 0),
}));
