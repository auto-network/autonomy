import fs from 'node:fs';
import { mintFleetInvite } from '../fleet-enrollment.js';

const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const seed = Uint8Array.from(Buffer.from(fixture.personal_root_seed, 'hex'));
const minted = await mintFleetInvite({
  personalRootSeed: seed,
  rootPub: fixture.root_pub,
  rendezvous: fixture.rendezvous,
  inviteId: fixture.invite_id,
  expiresAt: fixture.expires_at,
});
process.stdout.write(JSON.stringify({
  ...minted,
  root_seed_zeroed: seed.every((value) => value === 0),
}));
