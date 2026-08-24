/**
 * Revision-2 organization-root sealing primitives.
 *
 * The HPKE implementation is vendored from the audited, standards-oriented
 * hpke-js packages:
 *   @hpke/core 1.9.0
 *   @hpke/dhkem-x25519 1.8.0
 *   @hpke/chacha20poly1305 1.8.0
 *   @hpke/common 1.10.1 (shared transitive implementation)
 *
 * hpke-js is tested against the RFC 9180 vectors. Its X25519 and
 * ChaCha20-Poly1305 providers are derived from noble-curves/noble-ciphers,
 * which received a Cure53 audit:
 * https://cure53.de/audit-report_noble-crypto-libs.pdf
 *
 * This module supplies only Autonomy's domain separation and record framing.
 * It intentionally names X25519 encapsulation keys explicitly: their 64-hex
 * representation must not be confused with Ed25519 signing keys.
 */

import {
  Chacha20Poly1305,
  CipherSuite,
  DhkemX25519HkdfSha256,
  HkdfSha256,
  X25519,
  X25519HkdfSha256,
} from '../../vendor/hpke-x25519-chacha20poly1305-1.8.0.mjs';

const SUITE_X25519_HKDF_SHA256_CHACHA20POLY1305 = 1;
const DERIVATION_PREFIX = 'autonomy.idkit.encap-key.v1\n';
const SEAL_PREFIX = 'autonomy.idkit.seal.v';
const X25519_KEY_BYTES = 32;
const AEAD_TAG_BYTES = 16;
const MIN_RECORD_BYTES = 1 + X25519_KEY_BYTES + AEAD_TAG_BYTES;
const UTF8 = new TextEncoder();

if (!globalThis.crypto?.subtle) {
  // Node 18 does not expose WebCrypto globally unless launched with a flag.
  // The guarded dynamic import is never evaluated by a browser.
  const { webcrypto } = await import('node:crypto');
  globalThis.crypto = webcrypto;
}

const suite = new CipherSuite({
  kem: new DhkemX25519HkdfSha256(),
  kdf: new HkdfSha256(),
  aead: new Chacha20Poly1305(),
});
const x25519 = new X25519(new X25519HkdfSha256());

function asBytes(value, what) {
  if (value instanceof ArrayBuffer) {
    return new Uint8Array(value);
  }
  if (ArrayBuffer.isView(value)) {
    return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  }
  throw new TypeError(`${what} must be bytes`);
}

function validatePurpose(purpose) {
  if (
    typeof purpose !== 'string'
    || purpose.length === 0
    || [...purpose].some((character) => {
      const code = character.codePointAt(0);
      return code < 0x20 || code > 0x7e;
    })
  ) {
    throw new TypeError('seal purpose must be non-empty printable ASCII');
  }
  return purpose;
}

function decodeHex(value, what, expectedBytes = null) {
  if (
    typeof value !== 'string'
    || value.length % 2 !== 0
    || !/^[0-9a-f]*$/.test(value)
  ) {
    throw new TypeError(`${what} must be lowercase hexadecimal`);
  }
  const output = Uint8Array.from(
    value.match(/.{2}/g) ?? [],
    (pair) => Number.parseInt(pair, 16),
  );
  if (expectedBytes !== null && output.length !== expectedBytes) {
    throw new TypeError(`${what} must be ${expectedBytes} bytes`);
  }
  return output;
}

function encodeHex(value) {
  return [...asBytes(value, 'value')]
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('');
}

function sealInfo(suiteId, purpose) {
  return UTF8.encode(`${SEAL_PREFIX}${suiteId}\n${validatePurpose(purpose)}`);
}

function validateRecord(record) {
  if (record.length === 0) {
    throw new TypeError('sealed record is empty');
  }
  if (record[0] !== SUITE_X25519_HKDF_SHA256_CHACHA20POLY1305) {
    throw new TypeError(`unrecognized sealing suite: ${record[0]}`);
  }
  if (record.length < MIN_RECORD_BYTES) {
    throw new TypeError('sealed record is truncated');
  }
}

async function derivePrivateBytes(seed, purpose) {
  const seedBytes = asBytes(seed, 'personal root seed');
  if (seedBytes.length < X25519_KEY_BYTES) {
    throw new TypeError('personal root seed must be at least 32 bytes');
  }
  const key = await crypto.subtle.importKey('raw', seedBytes, 'HKDF', false, [
    'deriveBits',
  ]);
  const derived = await crypto.subtle.deriveBits(
    {
      name: 'HKDF',
      hash: 'SHA-256',
      salt: new Uint8Array(32),
      info: UTF8.encode(`${DERIVATION_PREFIX}${validatePurpose(purpose)}`),
    },
    key,
    X25519_KEY_BYTES * 8,
  );
  return new Uint8Array(derived);
}

/**
 * Derive the purpose-specific X25519 encapsulation keypair.
 *
 * The role-specific property names are deliberate: neither value is an
 * Ed25519 signing key, even though both private-key encodings are 64 hex
 * characters long.
 */
async function deriveEncapsulationKeypair(personalRootSeed, purpose) {
  const privateBytes = await derivePrivateBytes(personalRootSeed, purpose);
  try {
    const privateKey = await x25519.deserializePrivateKey(privateBytes);
    const publicKey = await x25519.derivePublicKey(privateKey);
    return {
      privateKeyHex: encodeHex(
        await x25519.serializePrivateKey(privateKey),
      ),
      publicKeyHex: encodeHex(
        await x25519.serializePublicKey(publicKey),
      ),
    };
  } finally {
    privateBytes.fill(0);
  }
}

/**
 * Seal bytes to an explicitly named X25519 encapsulation public key.
 *
 * This helper exists for cross-language conformance and future re-sealing.
 * Revision-2 dashboard reads use openSealedArmor().
 */
async function sealToEncapsulationKey(
  plaintext,
  recipientEncapsulationPublicKeyHex,
  purpose,
) {
  const publicBytes = decodeHex(
    recipientEncapsulationPublicKeyHex,
    'X25519 encapsulation public key',
    X25519_KEY_BYTES,
  );
  try {
    const recipientPublicKey = await x25519.deserializePublicKey(publicBytes);
    const sealed = await suite.seal(
      {
        recipientPublicKey,
        info: sealInfo(
          SUITE_X25519_HKDF_SHA256_CHACHA20POLY1305,
          purpose,
        ),
      },
      asBytes(plaintext, 'plaintext'),
    );
    const enc = asBytes(sealed.enc, 'encapsulated key');
    const ciphertext = asBytes(sealed.ct, 'ciphertext');
    const record = new Uint8Array(1 + enc.length + ciphertext.length);
    record[0] = SUITE_X25519_HKDF_SHA256_CHACHA20POLY1305;
    record.set(enc, 1);
    record.set(ciphertext, 1 + enc.length);
    return record;
  } catch {
    throw new Error('failed to seal revision-2 armor');
  }
}

/**
 * Open a revision-2 sealed org-root record using the personal root seed.
 *
 * `armor` is the server payload fragment:
 *   {sealed_root_key: <lowercase hex>, seal_purpose: <printable ASCII>}
 *
 * Malformed suite/framing is rejected before key derivation. Authentication
 * failures deliberately return one generic error.
 */
async function openSealedArmor(armor, personalRootSeed) {
  if (!armor || typeof armor !== 'object' || Array.isArray(armor)) {
    throw new TypeError('revision-2 armor must be an object');
  }
  const purpose = validatePurpose(armor.seal_purpose);
  const record = decodeHex(armor.sealed_root_key, 'sealed root key');
  validateRecord(record);

  const privateBytes = await derivePrivateBytes(personalRootSeed, purpose);
  try {
    const recipientKey = await x25519.deserializePrivateKey(privateBytes);
    const plaintext = await suite.open(
      {
        recipientKey,
        enc: record.slice(1, 1 + X25519_KEY_BYTES),
        info: sealInfo(record[0], purpose),
      },
      record.slice(1 + X25519_KEY_BYTES),
    );
    return new Uint8Array(plaintext);
  } catch {
    throw new Error('failed to open revision-2 armor');
  } finally {
    privateBytes.fill(0);
  }
}

/** Open a sealed record with an already-derived X25519 private key.
 *
 * Policy-factor recipients deliberately derive their X25519 key under a
 * stable factor purpose while binding each ciphertext to a separate policy
 * generation/path purpose.  openSealedArmor derives both from one purpose and
 * therefore cannot represent that construction; this generic half mirrors
 * tools.network.idkit.sealing.open exactly.
 */
async function openWithEncapsulationPrivateKey(
  recordValue,
  recipientEncapsulationPrivateKeyHex,
  purpose,
) {
  const record = asBytes(recordValue, 'sealed record');
  validateRecord(record);
  const privateBytes = decodeHex(
    recipientEncapsulationPrivateKeyHex,
    'X25519 encapsulation private key',
    X25519_KEY_BYTES,
  );
  try {
    const recipientKey = await x25519.deserializePrivateKey(privateBytes);
    const plaintext = await suite.open(
      {
        recipientKey,
        enc: record.slice(1, 1 + X25519_KEY_BYTES),
        info: sealInfo(record[0], purpose),
      },
      record.slice(1 + X25519_KEY_BYTES),
    );
    return new Uint8Array(plaintext);
  } catch {
    throw new Error('failed to open sealed record');
  } finally {
    privateBytes.fill(0);
  }
}

export {
  SUITE_X25519_HKDF_SHA256_CHACHA20POLY1305,
  deriveEncapsulationKeypair,
  openSealedArmor,
  openWithEncapsulationPrivateKey,
  sealToEncapsulationKey,
};
