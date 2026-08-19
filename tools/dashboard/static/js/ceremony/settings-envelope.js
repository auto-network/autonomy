/*
 * Signed-settings envelope construction shared by browser and Node.
 *
 * The Python module tools/network/settingskit/envelope.py is the wire
 * authority; the two must produce byte-identical canonical records
 * (asserted by ceremony/tests/settings-envelope.test.mjs against
 * Python-generated vectors). Keep this module free of storage and
 * transport: it only validates, canonicalizes, signs, and verifies the
 * addressed record. Design of record: graph://21a0da9e-1c2.
 */

import {
  bytesToHex,
  canonicalJson,
  domainBytes,
  hexToBytes,
} from './primitives.js';

const SETTINGS_ENVELOPE_DOMAIN = 'autonomy.network.settings.envelope.v1\n';

const PUBLICATION_STATES = ['raw', 'curated', 'published', 'canonical'];

const ENVELOPE_FIELDS = [
  'org',
  'set_id',
  'key',
  'schema_revision',
  'publication_state',
  'deprecated',
  'successor_id',
  'payload',
  'signed_at',
  'signing_key',
  'witness',
];

let webCrypto = globalThis.crypto;
if (
  !webCrypto
  && typeof process !== 'undefined'
  && process.versions?.node
) {
  ({ webcrypto: webCrypto } = await import('node:crypto'));
}
if (!webCrypto?.subtle) {
  throw new Error('settings envelopes require WebCrypto');
}

function requireLowerHex(value, length, name) {
  const pattern = new RegExp(`^[0-9a-f]{${length}}$`);
  if (typeof value !== 'string' || !pattern.test(value)) {
    throw new Error(`${name} must be ${length} lowercase hex chars`);
  }
  return value;
}

function requireNonEmptyString(value, name) {
  if (typeof value !== 'string' || !value) {
    throw new Error(`${name} must be a non-empty string`);
  }
  return value;
}

function requirePlainObject(value, name) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${name} must be an object`);
  }
  return value;
}

// The exact object the browser signs. Mirrors Python build_record: every
// structural refusal here is a refusal there, or the two sides would accept
// different worlds.
function buildSettingsRecord({
  org,
  set_id: setId,
  key,
  schema_revision: schemaRevision,
  publication_state: publicationState,
  deprecated,
  successor_id: successorId,
  payload,
  signed_at: signedAt,
  signing_key: signingKey,
  witness,
}) {
  requireLowerHex(org, 64, 'org genesis id');
  requireNonEmptyString(setId, 'set_id');
  requireNonEmptyString(key, 'key');
  if (!Number.isSafeInteger(schemaRevision) || schemaRevision < 1) {
    throw new Error('schema_revision must be a positive integer');
  }
  if (!PUBLICATION_STATES.includes(publicationState)) {
    throw new Error(
      `publication_state must be one of ${PUBLICATION_STATES.join(', ')}`,
    );
  }
  if (typeof deprecated !== 'boolean') {
    throw new Error('deprecated must be a boolean');
  }
  if (successorId === undefined) {
    throw new Error('successor_id must be present (null when none)');
  }
  if (successorId !== null) {
    requireNonEmptyString(successorId, 'successor_id');
  }
  requirePlainObject(payload, 'payload');
  if (!Number.isSafeInteger(signedAt) || signedAt < 0) {
    throw new Error('signed_at must be a non-negative integer (unix milliseconds)');
  }
  // Required for BOTH key strategies: after a rekey the signing key is not
  // the row key, so a record that cannot name its signer cannot exist.
  requireLowerHex(signingKey, 64, 'signing_key');
  if (witness === undefined) {
    throw new Error(
      'witness must be present (null for an organization that has never published)',
    );
  }
  if (witness !== null) {
    requirePlainObject(witness, 'witness');
    if (Object.keys(witness).length === 0) {
      throw new Error('witness must be the served attestation object, or null');
    }
  }
  const record = {
    org,
    set_id: setId,
    key,
    schema_revision: schemaRevision,
    publication_state: publicationState,
    deprecated,
    successor_id: successorId,
    payload,
    signed_at: signedAt,
    signing_key: signingKey,
    // "No witness" is an explicit null in the signed bytes, never a missing
    // key — an organization that has never published has no attestation to
    // cite, and both builders must agree on those bytes too.
    witness,
  };
  // This also rejects values outside the shared canonical-JSON grammar
  // (floats, undefined, foreign types) anywhere in payload or witness.
  canonicalJson(record);
  return record;
}

function validateSettingsRecord(record) {
  requirePlainObject(record, 'envelope record');
  const keys = new Set(Object.keys(record));
  if (
    keys.size !== ENVELOPE_FIELDS.length
    || !ENVELOPE_FIELDS.every((field) => keys.has(field))
  ) {
    throw new Error(
      'envelope record must carry exactly the addressed-record fields',
    );
  }
  return buildSettingsRecord(record);
}

function settingsSigningInput(record) {
  return domainBytes(
    SETTINGS_ENVELOPE_DOMAIN,
    canonicalJson(validateSettingsRecord(record)),
  );
}

async function signSettingsRecord(record, ed25519SigningKey) {
  if (
    !ed25519SigningKey
    || ed25519SigningKey.type !== 'private'
    || ed25519SigningKey.algorithm?.name !== 'Ed25519'
    || !ed25519SigningKey.usages?.includes('sign')
  ) {
    throw new Error('signSettingsRecord requires an Ed25519 private signing key');
  }
  const signingInput = settingsSigningInput(record);
  const signature = await webCrypto.subtle.sign(
    'Ed25519',
    ed25519SigningKey,
    signingInput,
  );
  // The record names its signer; a signature by any other key would fail
  // verification everywhere else, so prove it against the NAMED key before
  // returning rather than handing back a silently useless signature. Mirrors
  // the Python side's sign_record refusal — WebCrypto cannot compare a
  // private CryptoKey to a public hex directly, so the proof is a verify.
  const namedKey = await webCrypto.subtle.importKey(
    'raw',
    hexToBytes(record.signing_key),
    { name: 'Ed25519' },
    false,
    ['verify'],
  );
  const signedByNamedKey = await webCrypto.subtle.verify(
    'Ed25519',
    namedKey,
    signature,
    signingInput,
  );
  if (!signedByNamedKey) {
    throw new Error('record.signing_key does not match the signing key');
  }
  return bytesToHex(signature);
}

// Cryptographic step only — persona resolution, membership, scope,
// revocation, currency and the witness bound are boundary checks and live
// server-side.
async function verifySettingsRecord(record, signatureHex) {
  const validated = validateSettingsRecord(record);
  if (typeof signatureHex !== 'string' || !/^[0-9a-f]{128}$/.test(signatureHex)) {
    throw new Error('signature must be 128 lowercase hex chars');
  }
  const verificationKey = await webCrypto.subtle.importKey(
    'raw',
    hexToBytes(validated.signing_key),
    { name: 'Ed25519' },
    false,
    ['verify'],
  );
  return webCrypto.subtle.verify(
    'Ed25519',
    verificationKey,
    hexToBytes(signatureHex),
    settingsSigningInput(validated),
  );
}

export {
  ENVELOPE_FIELDS,
  PUBLICATION_STATES,
  SETTINGS_ENVELOPE_DOMAIN,
  buildSettingsRecord,
  settingsSigningInput,
  signSettingsRecord,
  validateSettingsRecord,
  verifySettingsRecord,
};
