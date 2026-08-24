import process from 'node:process';

import { importEd25519RootSigningKey } from '../primitives.js';
import {
  createRootAnchorEnvelope,
  openRootAnchorEnvelope,
} from '../root-anchor.js';

const input = JSON.parse(await new Promise((resolve) => {
  let data = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (chunk) => { data += chunk; });
  process.stdin.on('end', () => resolve(data));
}));

const rootSeed = Uint8Array.from(Buffer.from(input.root_seed, 'hex'));
try {
  if (input.action === 'open') {
    const seed = await openRootAnchorEnvelope(input.anchor, rootSeed);
    try { process.stdout.write(JSON.stringify({ anchor_seed: Buffer.from(seed).toString('hex') })); }
    finally { seed.fill(0); }
  } else if (input.action === 'create') {
    const signingKey = await importEd25519RootSigningKey(rootSeed);
    const envelope = await createRootAnchorEnvelope(
      { seed: rootSeed, signingKey, rootPub: input.root_pub },
      {
        anchorId: input.anchor_id,
        displayName: input.display_name,
        createdAt: input.created_at,
        anchorSeed: Uint8Array.from(Buffer.from(input.anchor_seed, 'hex')),
      },
    );
    process.stdout.write(JSON.stringify({ anchor: envelope }));
  } else {
    throw new Error(`unknown action ${input.action}`);
  }
} finally {
  rootSeed.fill(0);
}
