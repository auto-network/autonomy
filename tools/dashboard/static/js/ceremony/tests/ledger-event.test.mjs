import assert from 'node:assert/strict';
import { webcrypto as crypto } from 'node:crypto';
import { readFile } from 'node:fs/promises';

import {
  buildEvent,
  derivePersona,
  eventId,
  signEvent,
  signingInput,
} from '../ledger-event.js';
import {
  bytesToHex,
  canonicalJson,
  hexToBytes,
  importEd25519RootSigningKey,
} from '../primitives.js';

const fixturePath = process.argv[2];
if (!fixturePath) {
  throw new Error('usage: node ledger-event.test.mjs FIXTURE.json');
}
const fixture = JSON.parse(await readFile(fixturePath, 'utf8'));
const authorSigningKey = await importEd25519RootSigningKey(
  hexToBytes(fixture.author_seed_hex),
);
const authorVerificationKey = await crypto.subtle.importKey(
  'raw',
  hexToBytes(fixture.author_public_hex),
  { name: 'Ed25519' },
  false,
  ['verify'],
);
const nodeEvents = [];

for (const vector of fixture.vectors) {
  const event = buildEvent(vector.input);
  assert.deepEqual(event, vector.unsigned, `${vector.name}: unsigned event`);
  assert.equal(
    bytesToHex(signingInput(event)),
    vector.signing_input_hex,
    `${vector.name}: signing input`,
  );

  const pythonSignature = hexToBytes(vector.event.sig);
  assert.equal(
    await crypto.subtle.verify(
      'Ed25519',
      authorVerificationKey,
      pythonSignature,
      signingInput(event),
    ),
    true,
    `${vector.name}: verify Python signature in Node`,
  );

  const signed = await signEvent(event, authorSigningKey);
  assert.deepEqual(
    signed,
    vector.event,
    `${vector.name}: deterministic Node/Python signature`,
  );
  assert.equal(
    await eventId(signed),
    vector.event_id,
    `${vector.name}: event id`,
  );
  assert.equal(
    bytesToHex(new TextEncoder().encode(canonicalJson(signed))),
    vector.wire_hex,
    `${vector.name}: canonical wire bytes`,
  );

  const mutations = {
    v: { ...signed, v: signed.v + 1 },
    author_key: { ...signed, author_key: fixture.other_public_hex },
    parents: {
      ...signed,
      parents: signed.parents.length
        ? signed.parents.map(
          (parent, index) => (index === 0 ? '44'.repeat(32) : parent),
        )
        : ['44'.repeat(32)],
    },
    hlc: { ...signed, hlc: [signed.hlc[0], signed.hlc[1] + 1] },
    payload: {
      ...signed,
      payload: { ...signed.payload, mutation_probe: true },
    },
    sig: {
      ...signed,
      sig: `${signed.sig[0] === '0' ? '1' : '0'}${signed.sig.slice(1)}`,
    },
  };
  for (const [field, mutation] of Object.entries(mutations)) {
    assert.notEqual(
      await eventId(mutation),
      vector.event_id,
      `${vector.name}: ${field} mutation must change event id`,
    );
  }
  nodeEvents.push(signed);
}

const persona = await derivePersona(
  hexToBytes(fixture.persona.personal_root_seed_hex),
  fixture.persona.genesis_id,
);
assert.equal(persona.publicHex, fixture.persona.public_hex);
assert.equal(persona.signingKey.extractable, false);
assert.deepEqual(persona.signingKey.usages, ['sign']);
assert.equal(persona.verificationKey.extractable, false);
assert.deepEqual(persona.verificationKey.usages, ['verify']);
const personaProbe = new TextEncoder().encode('persona parity probe');
const personaSignature = await crypto.subtle.sign(
  'Ed25519',
  persona.signingKey,
  personaProbe,
);
assert.equal(
  await crypto.subtle.verify(
    'Ed25519',
    persona.verificationKey,
    personaSignature,
    personaProbe,
  ),
  true,
);

process.stdout.write(JSON.stringify({
  events: nodeEvents,
  persona_public_hex: persona.publicHex,
}));
