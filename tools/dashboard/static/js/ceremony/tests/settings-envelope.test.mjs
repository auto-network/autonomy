import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

import { derivePersona } from '../ledger-event.js';
import {
  canonicalEnvelopeJson,
  buildSettingsRecord,
  settingsSigningInput,
  signSettingsRecord,
  verifySettingsRecord,
} from '../settings-envelope.js';
import { bytesToHex, hexToBytes } from '../primitives.js';

const fixturePath = process.argv[2];
if (!fixturePath) {
  throw new Error('usage: node settings-envelope.test.mjs FIXTURE.json');
}
const fixture = JSON.parse(await readFile(fixturePath, 'utf8'));
const textEncoder = new TextEncoder();

function canonicalHex(record) {
  return bytesToHex(textEncoder.encode(canonicalEnvelopeJson(buildSettingsRecord(record))));
}

// --- base record: byte-identical encoding, signing input, both signatures ---
const base = fixture.base;
assert.equal(canonicalHex(base.input), base.canonical_hex, 'base: canonical bytes');
assert.equal(
  bytesToHex(settingsSigningInput(base.input)),
  base.signing_input_hex,
  'base: signing input',
);
assert.equal(
  await verifySettingsRecord(base.input, base.signature_hex),
  true,
  'base: Python signature verifies in Node',
);
const rootSeed = hexToBytes(fixture.personal_root_seed_hex);
const personaA = await derivePersona(rootSeed, fixture.genesis_ids[0]);
assert.equal(
  personaA.publicHex,
  base.input.signing_key,
  'base: derived persona matches signing_key',
);
assert.equal(
  await signSettingsRecord(base.input, personaA.signingKey),
  base.signature_hex,
  'base: Node signature is byte-identical (Ed25519 is deterministic)',
);

// --- eleven per-field mutations: bytes match Python and differ from base ---
const mutationFields = Object.keys(fixture.mutations);
assert.equal(mutationFields.length, 11, 'exactly eleven signed fields mutate');
for (const field of mutationFields) {
  const mutation = fixture.mutations[field];
  const hex = canonicalHex(mutation.input);
  assert.equal(hex, mutation.canonical_hex, `${field}: canonical bytes`);
  assert.notEqual(hex, base.canonical_hex, `${field}: mutation changes the bytes`);
  assert.equal(
    await verifySettingsRecord(mutation.input, base.signature_hex),
    false,
    `${field}: base signature does not survive the mutation`,
  );
}

// --- "no witness" is a distinguishable encoding both builders agree on ---
assert.equal(
  canonicalHex(fixture.no_witness.input),
  fixture.no_witness.canonical_hex,
  'no-witness: canonical bytes',
);
assert.notEqual(
  fixture.no_witness.canonical_hex,
  base.canonical_hex,
  'no-witness: distinguishable from a cited attestation',
);
const omittedWitness = { ...base.input };
delete omittedWitness.witness;
assert.throws(
  () => buildSettingsRecord(omittedWitness),
  /witness must be present/,
  'no-witness: a missing witness field is not the null encoding',
);

// --- payload key order and whitespace do not change the bytes ---
const shuffledPayload = JSON.parse(fixture.payload_json_shuffled);
assert.equal(
  canonicalHex({ ...base.input, payload: shuffledPayload }),
  base.canonical_hex,
  'payload serialization form does not reach the bytes',
);

// --- one member, three organizations: 0 of 6 cross-org pairs verify ---
const rows = fixture.cross_org;
assert.equal(rows.length, 3, 'cross-org: three organizations');
for (const row of rows) {
  assert.equal(
    canonicalHex(row.record),
    row.canonical_hex,
    `cross-org ${row.genesis_id.slice(0, 8)}: canonical bytes`,
  );
  assert.equal(
    await verifySettingsRecord(row.record, row.signature_hex),
    true,
    `cross-org ${row.genesis_id.slice(0, 8)}: verifies at its own address`,
  );
}
let crossPairs = 0;
for (const from of rows) {
  for (const to of rows) {
    if (from === to) continue;
    crossPairs += 1;
    assert.equal(
      await verifySettingsRecord(
        { ...from.record, org: to.genesis_id },
        from.signature_hex,
      ),
      false,
      'cross-org: a row from one organization never verifies in another',
    );
  }
}
assert.equal(crossPairs, 6, 'cross-org: all six directed pairs checked');
for (let i = 0; i < rows.length; i += 1) {
  for (let j = i + 1; j < rows.length; j += 1) {
    for (const field of ['key', 'signing_key']) {
      assert.notEqual(
        rows[i].record[field],
        rows[j].record[field],
        `cross-org: ${field} must not recur across organizations`,
      );
    }
    assert.notEqual(
      rows[i].signature_hex,
      rows[j].signature_hex,
      'cross-org: signatures must not recur across organizations',
    );
  }
}

// --- the integer domain is shared: max-safe encodes, one-over refuses ---
assert.equal(
  canonicalHex(fixture.max_safe_integer.input),
  fixture.max_safe_integer.canonical_hex,
  'max-safe integer: byte-identical at every depth',
);

// --- signing with a key other than the named signer is refused ---
const personaB = await derivePersona(rootSeed, fixture.genesis_ids[1]);
await assert.rejects(
  signSettingsRecord(base.input, personaB.signingKey),
  /signing_key does not match/,
  'a signature by a key the record does not name is never returned',
);

// --- structural refusals shared with Python, for both key strategies ---
for (const [name, input] of Object.entries(fixture.refusals)) {
  assert.throws(
    () => buildSettingsRecord(input),
    `refusal ${name}: an envelope that cannot name its signer cannot exist`,
  );
}
// --- floats and non-object payloads: byte-identical on both sides ---
assert.equal(canonicalHex(fixture.floats.input), fixture.floats.canonical_hex, 'floats: canonical bytes');
assert.equal(
  await verifySettingsRecord(fixture.floats.input, fixture.floats.signature_hex),
  true,
  'floats: Python signature verifies in Node',
);
assert.equal(
  canonicalHex(fixture.sealed_payload.input),
  fixture.sealed_payload.canonical_hex,
  'sealed string payload: canonical bytes',
);
assert.equal(
  canonicalHex(fixture.array_payload.input),
  fixture.array_payload.canonical_hex,
  'array payload: canonical bytes',
);
assert.throws(
  () => buildSettingsRecord({ ...base.input, payload: { bad: Number.NaN } }),
  /non-finite/,
  'NaN is refused',
);
assert.throws(
  () => buildSettingsRecord({ ...base.input, payload: { bad: Number.POSITIVE_INFINITY } }),
  /non-finite/,
  'Infinity is refused',
);
// An undefined member is omitted, as JSON.stringify and primitives.canonicalJson omit it.
assert.equal(canonicalEnvelopeJson({ a: undefined, b: 1 }), '{"b":1}', 'undefined members are omitted');
assert.throws(
  () => canonicalEnvelopeJson({ n: 1e20 }),
  /safe range/,
  'an integer-valued double beyond the safe range and below 1e21 is refused',
);
assert.equal(canonicalEnvelopeJson({ n: 1e21 }), '{"n":1e+21}', 'from 1e21 the form is exponential');
assert.throws(
  () => buildSettingsRecord({ ...base.input, deprecated: 0 }),
  'refusal: deprecated must be a boolean, not an integer',
);

console.log('settings-envelope vectors: all assertions passed');
