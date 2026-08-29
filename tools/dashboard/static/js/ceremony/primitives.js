/*
 * WebCrypto-only primitives shared by browser and Node ceremonies.
 *
 * Canonical JSON and armor behavior mirror tools/network/idkit. Keep this
 * module free of DOM, storage, and transport dependencies.
 */

import {
  deriveEncapsulationKeypair,
  openSealedArmor,
  sealToEncapsulationKey,
} from './sealing.js';

const ARMOR_BEGIN = '-----BEGIN AUTONOMY NETWORK ROOT KEY-----';
const ARMOR_END = '-----END AUTONOMY NETWORK ROOT KEY-----';

// The one label a passkey factor is derived and sealed under — the SAME
// vault-factor purpose the enrollment ceremony derives the provisioning key
// under (one key, one label). Mirrors idkit.armor.PASSKEY_ARMOR_PURPOSE.
const PASSKEY_ARMOR_PURPOSE = 'autonomy/vault-factor/v1';
const CREDENTIAL_ID_RE = /^[A-Za-z0-9_-]{1,256}$/;

// Raw 32-byte Ed25519 signing seed -> PKCS#8 (RFC 8410).
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
  throw new Error('ceremony primitives require WebCrypto');
}

const SHORT_ESCAPES = {
  8: '\\b', 9: '\\t', 10: '\\n', 12: '\\f', 13: '\\r',
  34: '\\"', 92: '\\\\',
};

const textEncoder = new TextEncoder();

function escapeString(value) {
  let out = '"';
  for (let i = 0; i < value.length; i += 1) {
    const codeUnit = value.charCodeAt(i);
    if (SHORT_ESCAPES[codeUnit]) {
      out += SHORT_ESCAPES[codeUnit];
    } else if (codeUnit < 0x20 || codeUnit > 0x7e) {
      out += `\\u${(`000${codeUnit.toString(16)}`).slice(-4)}`;
    } else {
      out += value[i];
    }
  }
  return `${out}"`;
}

function codePoints(value) {
  return Array.from(value).map((character) => character.codePointAt(0));
}

// Python sorts string keys by Unicode code point. JavaScript's default sort
// compares UTF-16 code units and disagrees when astral-plane keys are present.
function compareLikePython(left, right) {
  const leftPoints = codePoints(left);
  const rightPoints = codePoints(right);
  const length = Math.min(leftPoints.length, rightPoints.length);
  for (let i = 0; i < length; i += 1) {
    if (leftPoints[i] !== rightPoints[i]) {
      return leftPoints[i] - rightPoints[i];
    }
  }
  return leftPoints.length - rightPoints.length;
}

function canonicalJson(value) {
  if (value === null) return 'null';
  const type = typeof value;
  if (type === 'boolean') return value ? 'true' : 'false';
  if (type === 'number') {
    if (!Number.isSafeInteger(value)) {
      throw new Error(`canonical JSON allows integers only, got ${value}`);
    }
    return String(value);
  }
  if (type === 'string') return escapeString(value);
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(',')}]`;
  }
  if (type === 'object') {
    const parts = [];
    for (const key of Object.keys(value).sort(compareLikePython)) {
      const member = value[key];
      if (member === undefined) continue;
      parts.push(`${escapeString(key)}:${canonicalJson(member)}`);
    }
    return `{${parts.join(',')}}`;
  }
  throw new Error(`type ${type} is not allowed in canonical JSON`);
}

function hexToBytes(hex) {
  if (
    typeof hex !== 'string'
    || hex.length % 2 !== 0
    || /[^0-9a-f]/.test(hex)
  ) {
    throw new Error('expected lowercase hex');
  }
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i += 1) {
    out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return out;
}

function bytesToHex(bytes) {
  const view = new Uint8Array(bytes);
  let out = '';
  for (const byte of view) {
    out += (`0${byte.toString(16)}`).slice(-2);
  }
  return out;
}

function b64ToBytes(base64) {
  const binary = atob(base64);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    out[i] = binary.charCodeAt(i);
  }
  return out;
}

function domainBytes(domain, canonicalString) {
  const domainEncoded = textEncoder.encode(domain);
  const canonicalEncoded = textEncoder.encode(canonicalString);
  const out = new Uint8Array(
    domainEncoded.length + canonicalEncoded.length,
  );
  out.set(domainEncoded, 0);
  out.set(canonicalEncoded, domainEncoded.length);
  return out;
}

function sameKeys(value, keys) {
  return (
    value
    && typeof value === 'object'
    && !Array.isArray(value)
    && Object.keys(value).sort().join(',') === keys.slice().sort().join(',')
  );
}

function canonicalBase64Length(value) {
  if (typeof value !== 'string') return -1;
  let bytes;
  try {
    bytes = b64ToBytes(value);
  } catch {
    return -1;
  }
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  if (btoa(binary) !== value) return -1;
  return bytes.length;
}

async function importEd25519RootSigningKey(ed25519SigningSeed) {
  // Both the raw seed copy and the pkcs8 it is spliced into carry root key
  // material and must be zeroed on every exit -- the outer finally covers the
  // length guard too, so no path leaves the seed on the heap.
  const seed = new Uint8Array(ed25519SigningSeed);
  try {
    if (seed.length !== 32) {
      throw new Error('Ed25519 root signing seed must be exactly 32 bytes');
    }
    const pkcs8 = new Uint8Array(
      PKCS8_ED25519_PREFIX.length + seed.length,
    );
    pkcs8.set(PKCS8_ED25519_PREFIX, 0);
    pkcs8.set(seed, PKCS8_ED25519_PREFIX.length);
    try {
      return await webCrypto.subtle.importKey(
        'pkcs8',
        pkcs8,
        { name: 'Ed25519' },
        false,
        ['sign'],
      );
    } finally {
      pkcs8.fill(0);
    }
  } finally {
    seed.fill(0);
  }
}

// ── Armor v2: versioned multi-lock envelope (mirror of armor.py) ─────────────
//
// Byte-compatible with the canonical Python (tools/network/idkit/armor.py):
export {
  canonicalJson,
  hexToBytes,
  bytesToHex,
  b64ToBytes,
  domainBytes,
  importEd25519RootSigningKey,
};

export {
  SUITE_X25519_HKDF_SHA256_CHACHA20POLY1305,
  deriveEncapsulationKeypair,
  openSealedArmor,
  sealToEncapsulationKey,
} from './sealing.js';
