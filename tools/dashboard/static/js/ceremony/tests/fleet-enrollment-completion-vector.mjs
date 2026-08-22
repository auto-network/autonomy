import fs from 'node:fs';
import { completeFleetEnrollment } from '../fleet-enrollment.js';

const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const seed = Uint8Array.from(Buffer.from(fixture.personal_root_seed, 'hex'));
const completion = await completeFleetEnrollment({
  personalRootSeed: seed,
  requestId: fixture.request_id,
  request: fixture.request,
  channelBinding: fixture.channel_binding,
  approval: fixture.approval,
  rosterEntry: fixture.roster_entry,
});
process.stdout.write(JSON.stringify({
  ...completion,
  root_seed_zeroed: seed.every((value) => value === 0),
}));
