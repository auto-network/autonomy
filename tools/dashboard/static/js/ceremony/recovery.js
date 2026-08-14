/*
 * The personal recovery-code ceremony's COLD dual-function seed (row 16).
 *
 * One high-entropy recovery code yields BOTH recovery functions, kept
 * DOMAIN-SEPARATED: an Ed25519 recovery keypair (signing -- co-signs an org
 * root key.rotate, the recovery_pub the genesis policy declares and the fold
 * verifies, per auto-n9cy3) and a KEK-recovery seed (decryption -- the share
 * that reconstructs the key wrapping the personal-root armor). The code is
 * COLD: its authority over a stolen root comes entirely from never touching a
 * browser except at generation. This module DERIVES; it never wraps the armor.
 *
 * The KEK-recovery seed's USAGE -- reconstructing the KEK to open the armor --
 * is register row 17 and the vault's multi-share KEK design (0c206bd8),
 * designed-unbuilt; row 16 derives and records the seed, row 17 consumes it.
 */
import { bytesToHex } from './primitives.js';
import { importDerivedPersonaKey } from './ledger-event.js';

// Distinct info per function -- the signing seed and the KEK seed can never be
// reconstructed from each other, and neither collides with the persona/KEM
// derivations (different domain strings).
const RECOVERY_SIGN_INFO = 'autonomy.recovery.signing.v1';
const RECOVERY_KEK_INFO = 'autonomy.recovery.kek.v1';
const RECOVERY_MIN_CODE_BYTES = 32; // at least 256 bits of entropy

let webCrypto = globalThis.crypto;
if (
  !webCrypto
  && typeof process !== 'undefined'
  && process.versions?.node
) {
  ({ webcrypto: webCrypto } = await import('node:crypto'));
}
if (!webCrypto?.subtle) {
  throw new Error('recovery ceremony requires WebCrypto');
}

const textEncoder = new TextEncoder();

async function hkdf32(ikm, info) {
  const material = await webCrypto.subtle.importKey(
    'raw', ikm, 'HKDF', false, ['deriveBits'],
  );
  return new Uint8Array(await webCrypto.subtle.deriveBits(
    {
      name: 'HKDF',
      hash: 'SHA-256',
      // HKDF salt=None convention (an all-zero digest-sized salt), matching
      // the other ceremony derivations; the info string carries the domain.
      salt: new Uint8Array(32),
      info: textEncoder.encode(info),
    },
    material,
    256,
  ));
}

// deriveRecoveryFactors(recoveryCode: Uint8Array)
//   -> {recoveryPub, recoverySigningKey, kekRecoverySeed}
// The raw code and the signing seed are zeroed on every path; only the public
// half, the non-extractable signing key, and the KEK seed the caller must
// persist come back.
export async function deriveRecoveryFactors(recoveryCode) {
  if (
    !(recoveryCode instanceof Uint8Array)
    || recoveryCode.length < RECOVERY_MIN_CODE_BYTES
  ) {
    throw new Error(
      `recovery code must be a Uint8Array of at least ${RECOVERY_MIN_CODE_BYTES} bytes`,
    );
  }
  const ikm = new Uint8Array(recoveryCode);
  let signSeed = null;
  try {
    signSeed = await hkdf32(ikm, RECOVERY_SIGN_INFO);
    const kekRecoverySeed = await hkdf32(ikm, RECOVERY_KEK_INFO);
    const { publicHex, signingKey } = await importDerivedPersonaKey(signSeed);
    return {
      recoveryPub: publicHex,
      recoverySigningKey: signingKey,
      kekRecoverySeed,
    };
  } finally {
    ikm.fill(0);
    if (signSeed) signSeed.fill(0);
  }
}

// ── The printable recovery code ────────────────────────────────────────────
//
// FRESH-FORK DECISION (no prior ruling on the encoding; the vault crib fixes
// only the 256-bit entropy and the mechanism): the code renders as CROCKFORD
// BASE32 -- a transcription-resistant alphabet that excludes the ambiguous
// I L O U, so 1/I/L and 0/O read the same and decode maps them -- plus a
// 2-character SHA-256 checksum that catches a typo BEFORE derivation, grouped
// in fives for reading. Chosen over a BIP-39 word list to avoid embedding a
// 2048-word table in the ceremony bundle; the operator can override the
// surface without touching deriveRecoveryFactors, which takes raw bytes.
const CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';

function base32Encode(bytes) {
  let bits = 0;
  let value = 0;
  let out = '';
  for (const b of bytes) {
    value = (value << 8) | b;
    bits += 8;
    while (bits >= 5) {
      out += CROCKFORD[(value >>> (bits - 5)) & 31];
      bits -= 5;
    }
  }
  if (bits > 0) out += CROCKFORD[(value << (5 - bits)) & 31];
  return out;
}

function base32Decode(str) {
  let bits = 0;
  let value = 0;
  const out = [];
  for (const ch of str) {
    const idx = CROCKFORD.indexOf(ch);
    if (idx < 0) throw new Error(`invalid recovery code character: ${ch}`);
    value = (value << 5) | idx;
    bits += 5;
    if (bits >= 8) {
      out.push((value >>> (bits - 8)) & 0xff);
      bits -= 8;
    }
  }
  return new Uint8Array(out);
}

async function checksumSuffix(bytes) {
  const digest = new Uint8Array(await webCrypto.subtle.digest('SHA-256', bytes));
  return CROCKFORD[digest[0] >>> 3]
    + CROCKFORD[((digest[0] & 7) << 2) | (digest[1] >>> 6)];
}

export function generateRecoveryCode() {
  return webCrypto.getRandomValues(new Uint8Array(RECOVERY_MIN_CODE_BYTES));
}

export async function encodeRecoveryCode(bytes) {
  if (!(bytes instanceof Uint8Array) || bytes.length !== RECOVERY_MIN_CODE_BYTES) {
    throw new Error(`recovery code must be ${RECOVERY_MIN_CODE_BYTES} bytes`);
  }
  const body = base32Encode(bytes) + await checksumSuffix(bytes);
  return body.match(/.{1,5}/g).join('-');
}

export async function decodeRecoveryCode(printable) {
  if (typeof printable !== 'string') {
    throw new Error('recovery code must be a string');
  }
  const cleaned = printable
    .toUpperCase()
    .replace(/[\s-]/g, '')
    .replace(/O/g, '0')
    .replace(/[IL]/g, '1');
  if (cleaned.length < 3) throw new Error('recovery code is too short');
  const body = cleaned.slice(0, -2);
  const check = cleaned.slice(-2);
  const bytes = base32Decode(body);
  if (bytes.length !== RECOVERY_MIN_CODE_BYTES) {
    throw new Error('recovery code has the wrong length');
  }
  if (await checksumSuffix(bytes) !== check) {
    throw new Error('recovery code checksum failed — check for a typo');
  }
  return bytes;
}

export { RECOVERY_SIGN_INFO, RECOVERY_KEK_INFO, RECOVERY_MIN_CODE_BYTES, bytesToHex };
