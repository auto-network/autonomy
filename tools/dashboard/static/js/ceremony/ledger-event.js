/*
 * Authority-ledger event construction shared by browser and Node ceremonies.
 *
 * The Python ledger Event type is the wire authority. Keep this module free
 * of storage and transport: it only canonicalizes, signs, and hashes events,
 * and derives the per-organization Ed25519 persona signing key.
 */

import {
  bytesToHex,
  canonicalJson,
  domainBytes,
} from './primitives.js';

const EVENT_DOMAIN = 'autonomy.ledger.event.v1\n';
const EVENT_VERSION = 1;
const PERSONA_SALT = 'autonomy.identity.persona.v1';
const PKCS8_ED25519_PREFIX = [
  0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06,
  0x03, 0x2b, 0x65, 0x70, 0x04, 0x22, 0x04, 0x20,
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
  throw new Error('ledger events require WebCrypto');
}

const textEncoder = new TextEncoder();

function requireLowerHex(value, length, name) {
  const pattern = new RegExp(`^[0-9a-f]{${length}}$`);
  if (typeof value !== 'string' || !pattern.test(value)) {
    throw new Error(`${name} must be ${length} lowercase hex chars`);
  }
  return value;
}

function normalizeHlc(hlc) {
  if (
    !Array.isArray(hlc)
    || hlc.length !== 2
    || !hlc.every(
      (value) => Number.isSafeInteger(value) && value >= 0,
    )
  ) {
    throw new Error('hlc must be two non-negative safe integers');
  }
  return [hlc[0], hlc[1]];
}

function normalizeParents(parents) {
  if (!Array.isArray(parents)) {
    throw new Error('parents must be an array');
  }
  const normalized = parents.map(
    (parent) => requireLowerHex(parent, 64, 'parent'),
  );
  return Array.from(new Set(normalized)).sort();
}

function requirePayload(payload) {
  if (
    !payload
    || typeof payload !== 'object'
    || Array.isArray(payload)
  ) {
    throw new Error('payload must be an object');
  }
  // This also rejects values outside the shared canonical-JSON grammar.
  canonicalJson(payload);
  return payload;
}

function unsignedFields(event) {
  if (!event || typeof event !== 'object' || Array.isArray(event)) {
    throw new Error('event must be an object');
  }
  return {
    v: event.v,
    author_key: event.author_key,
    parents: event.parents,
    hlc: event.hlc,
    payload: event.payload,
  };
}

function wireFields(event) {
  if (typeof event.sig !== 'string') {
    throw new Error('signed event must carry sig');
  }
  return {
    ...unsignedFields(event),
    sig: event.sig,
  };
}

function buildEvent({
  authorKey,
  parents,
  hlc,
  payload,
}) {
  return {
    v: EVENT_VERSION,
    author_key: requireLowerHex(authorKey, 64, 'authorKey'),
    parents: normalizeParents(parents),
    hlc: normalizeHlc(hlc),
    payload: requirePayload(payload),
  };
}

function signingInput(event) {
  return domainBytes(
    EVENT_DOMAIN,
    canonicalJson(unsignedFields(event)),
  );
}

async function signEvent(event, ed25519SigningKey) {
  if (
    !ed25519SigningKey
    || ed25519SigningKey.type !== 'private'
    || ed25519SigningKey.algorithm?.name !== 'Ed25519'
    || !ed25519SigningKey.usages?.includes('sign')
  ) {
    throw new Error('signEvent requires an Ed25519 private signing key');
  }
  const signature = await webCrypto.subtle.sign(
    'Ed25519',
    ed25519SigningKey,
    signingInput(event),
  );
  return {
    ...unsignedFields(event),
    sig: bytesToHex(signature),
  };
}

async function eventId(event) {
  const digest = await webCrypto.subtle.digest(
    'SHA-256',
    textEncoder.encode(canonicalJson(wireFields(event))),
  );
  return bytesToHex(digest);
}

function base64UrlToBytes(value) {
  let base64 = value.replace(/-/g, '+').replace(/_/g, '/');
  while (base64.length % 4) base64 += '=';
  const binary = atob(base64);
  const output = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    output[index] = binary.charCodeAt(index);
  }
  return output;
}

async function importDerivedPersonaKey(derivedSeed) {
  const pkcs8 = new Uint8Array(
    PKCS8_ED25519_PREFIX.length + derivedSeed.length,
  );
  pkcs8.set(PKCS8_ED25519_PREFIX, 0);
  pkcs8.set(derivedSeed, PKCS8_ED25519_PREFIX.length);
  let jwk;
  try {
    const derivationKey = await webCrypto.subtle.importKey(
      'pkcs8',
      pkcs8,
      { name: 'Ed25519' },
      true,
      ['sign'],
    );
    jwk = await webCrypto.subtle.exportKey('jwk', derivationKey);
    const signingKey = await webCrypto.subtle.importKey(
      'jwk',
      jwk,
      { name: 'Ed25519' },
      false,
      ['sign'],
    );
    const publicBytes = base64UrlToBytes(jwk.x);
    const verificationKey = await webCrypto.subtle.importKey(
      'raw',
      publicBytes,
      { name: 'Ed25519' },
      false,
      ['verify'],
    );
    return {
      publicHex: bytesToHex(publicBytes),
      signingKey,
      verificationKey,
    };
  } finally {
    pkcs8.fill(0);
    if (jwk) {
      delete jwk.d;
      delete jwk.x;
    }
  }
}

async function derivePersona(personalRootSeed, genesisId) {
  const seed = new Uint8Array(personalRootSeed);
  if (seed.length !== 32) {
    throw new Error('personalRootSeed must be exactly 32 raw bytes');
  }
  requireLowerHex(genesisId, 64, 'genesisId');
  const material = await webCrypto.subtle.importKey(
    'raw',
    seed,
    'HKDF',
    false,
    ['deriveBits'],
  );
  const derivedSeed = new Uint8Array(await webCrypto.subtle.deriveBits(
    {
      name: 'HKDF',
      hash: 'SHA-256',
      salt: textEncoder.encode(PERSONA_SALT),
      info: textEncoder.encode(genesisId),
    },
    material,
    256,
  ));
  try {
    return await importDerivedPersonaKey(derivedSeed);
  } finally {
    derivedSeed.fill(0);
  }
}

export {
  buildEvent,
  derivePersona,
  eventId,
  signEvent,
  signingInput,
};
