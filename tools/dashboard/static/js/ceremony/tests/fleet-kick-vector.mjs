import fs from 'node:fs';
import { signFleetKick } from '../fleet-kick.js';

const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const seed = Uint8Array.from(Buffer.from(fixture.personal_root_seed, 'hex'));
const minted = await signFleetKick(
  { seed, rootPub: fixture.root_pub },
  {
    machineId: fixture.machine_id,
    machinePublicKey: fixture.machine_public_key,
    seq: fixture.seq,
  },
  { issuedAt: fixture.issued_at },
);
process.stdout.write(JSON.stringify({
  ...minted,
  root_seed_zeroed: seed.every((value) => value === 0),
}));
