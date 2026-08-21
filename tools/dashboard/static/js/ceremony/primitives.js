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
// same field names, same AAD prefixes, same PBKDF2-SHA256 / AES-256-GCM crypto,
// so a v2 armor minted here opens in Python and vice versa. The seed is sealed
// under a fresh random master KEK; the master KEK is wrapped by a factor list
// (a password factor for now). Strict, RECURSIVELY closed parse (I1), with TOTAL
// factor-type dispatch — a registered type always has a strict parser, so a type
// can never clear the membership check yet hit no field-closure (F4).

const ARMOR_VERSION = 2;
const V2_SEAL_AAD = 'autonomy.idkit.armor.v2.kek-seal\n';
const V2_FACTOR_AAD = 'autonomy.idkit.armor.v2.factor\n';
const V2_MIN_ITERATIONS = 10000;
const V2_MAX_ITERATIONS = 100000000;
const V2_DEFAULT_ITERATIONS = 600000;

function bytesToB64(bytes) {
  let binary = '';
  for (const b of new Uint8Array(bytes)) binary += String.fromCharCode(b);
  return btoa(binary);
}

// What each factor type contributes to the set commitment: its type plus the
// PUBLIC material identifying it. A registry, like the parser table, so a new
// type cannot be added without deciding what pins it — a type contributing
// nothing would be one an attacker could add or strip unnoticed.
const V2_FACTOR_COMMITMENTS = {
  password: (f) => ({
    type: 'password',
    salt: f.kdf.salt,
    iterations: f.kdf.iterations,
  }),
  recovery: (f) => ({ type: 'recovery', kem_pub: f.kem_pub }),
  // Plural: one per full passkey, pinned by BOTH the credential and the
  // encapsulation pub the master KEK is sealed to — so a factor cannot be
  // swapped for one that opens under a different device's ceremony unnoticed.
  passkey: (f) => ({
    type: 'passkey',
    credential_id: f.credential_id,
    kem_pub: f.kem_pub,
  }),
};

// A digest over the factor SET. Each factor's wrap is already bound to its own
// slot, but nothing bound the LIST — so anyone able to write the file could
// delete a factor and it would still open with whatever remained. Stripping a
// recovery factor that way is silent: the owner finds out when they reach for
// the code, on the day they have already lost everything else.
async function v2FactorCommitment(factors) {
  const items = factors.map((f) => {
    const contribute = V2_FACTOR_COMMITMENTS[f.type];
    if (!contribute) {
      throw new Error(
        `v2 factor type ${f.type} has no set commitment; a factor that pins `
        + 'nothing could be added or stripped unnoticed',
      );
    }
    return contribute(f);
  });
  // Sorted by the whole canonical item, not just type: passkey factors repeat
  // the type, so a type-only key would not be a total order over the set.
  const keyOf = (x) => canonicalJson(x);
  items.sort((a, b) => (keyOf(a) < keyOf(b) ? -1 : keyOf(a) > keyOf(b) ? 1 : 0));
  const digest = new Uint8Array(await webCrypto.subtle.digest(
    'SHA-256', textEncoder.encode(canonicalJson(items)),
  ));
  return bytesToHex(digest);
}

// Binds the identity AND the exact set of factors, so editing the list
// invalidates the seal: an altered armor fails to open rather than opening
// with a lock quietly missing.
async function v2SealAad(rootPub, factors) {
  return textEncoder.encode(
    `${V2_SEAL_AAD}${rootPub}\n${await v2FactorCommitment(factors)}`,
  );
}

function v2FactorAad(rootPub, factorType) {
  return textEncoder.encode(`${V2_FACTOR_AAD}${rootPub}\n${factorType}`);
}

function parseV2PasswordFactor(f) {
  if (
    !sameKeys(f, ['type', 'kdf', 'cipher', 'iv', 'wrap'])
    || !sameKeys(f.kdf, ['name', 'hash', 'iterations', 'salt'])
    || f.kdf.name !== 'PBKDF2'
    || f.kdf.hash !== 'SHA-256'
    || !Number.isSafeInteger(f.kdf.iterations)
    || f.kdf.iterations < V2_MIN_ITERATIONS
    || f.kdf.iterations > V2_MAX_ITERATIONS
    || f.cipher !== 'AES-256-GCM'
    || canonicalBase64Length(f.kdf.salt) !== 16
    || canonicalBase64Length(f.iv) !== 12
    || canonicalBase64Length(f.wrap) !== 48
  ) {
    throw new Error('v2 password factor is malformed');
  }
}

// The registry IS the parser table: total dispatch by construction, so a type
// cannot be accepted without a strict parser (F4 — otherwise I1 reopens when a
// new lock type is added).
function parseV2PasskeyFactor(f) {
  if (
    !sameKeys(f, ['type', 'credential_id', 'kem_pub', 'sealed'])
    || typeof f.credential_id !== 'string'
    || !CREDENTIAL_ID_RE.test(f.credential_id)
    || typeof f.kem_pub !== 'string'
    || !/^[0-9a-f]{64}$/.test(f.kem_pub)
    // suite(1) + X25519 enc(32) + ChaCha20-Poly1305 of a 32-byte KEK (32+16)
    || canonicalBase64Length(f.sealed) !== 81
  ) {
    throw new Error('v2 passkey factor is malformed');
  }
}

const V2_FACTOR_PARSERS = {
  password: parseV2PasswordFactor,
  passkey: parseV2PasskeyFactor,
};

function parseArmor(armorText) {
  if (typeof armorText !== 'string') throw new Error('armor must be text');
  const lines = armorText
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
  if (
    lines.length < 3
    || lines[0] !== ARMOR_BEGIN
    || lines[lines.length - 1] !== ARMOR_END
  ) {
    throw new Error('this is not an auto.network root key armor');
  }
  let data;
  try {
    const body = b64ToBytes(lines.slice(1, -1).join(''));
    data = JSON.parse(new TextDecoder().decode(body));
  } catch {
    throw new Error('armor body does not decode');
  }
  if (
    !sameKeys(data, ['v', 'root_pub', 'kek_seal', 'factors'])
    || data.v !== 2
    || typeof data.root_pub !== 'string'
    || !/^[0-9a-f]{64}$/.test(data.root_pub)
    || !sameKeys(data.kek_seal, ['cipher', 'iv', 'ct'])
    || data.kek_seal.cipher !== 'AES-256-GCM'
    || canonicalBase64Length(data.kek_seal.iv) !== 12
    || canonicalBase64Length(data.kek_seal.ct) !== 48
    || !Array.isArray(data.factors)
    || data.factors.length === 0
  ) {
    throw new Error('unsupported or non-canonical v2 armor format');
  }
  const seen = new Set();
  for (const f of data.factors) {
    if (!f || typeof f !== 'object' || typeof f.type !== 'string') {
      throw new Error('v2 factor must be an object with a type');
    }
    const parser = V2_FACTOR_PARSERS[f.type];
    if (!parser) throw new Error(`v2 factor has unknown type ${f.type}`);
    // Singular types dedupe on type; passkey is plural (one per credential).
    const dedupKey = f.type === 'passkey' ? `passkey:${f.credential_id}` : f.type;
    if (seen.has(dedupKey)) throw new Error(`v2 duplicate factor ${dedupKey}`);
    seen.add(dedupKey);
    parser(f); // total dispatch — a known type always has a strict parser
  }
  return data;
}

async function decryptArmor(armorText, passphrase) {
  const data = parseArmor(armorText);
  const pw = data.factors.find((f) => f.type === 'password');
  if (!pw) throw new Error('v2 armor has no password factor');
  const material = await webCrypto.subtle.importKey(
    'raw', textEncoder.encode(passphrase), 'PBKDF2', false, ['deriveKey'],
  );
  const pwKey = await webCrypto.subtle.deriveKey(
    {
      name: 'PBKDF2', salt: b64ToBytes(pw.kdf.salt),
      iterations: pw.kdf.iterations, hash: 'SHA-256',
    },
    material, { name: 'AES-GCM', length: 256 }, false, ['decrypt'],
  );
  let masterKek;
  try {
    masterKek = new Uint8Array(await webCrypto.subtle.decrypt(
      {
        name: 'AES-GCM', iv: b64ToBytes(pw.iv),
        additionalData: v2FactorAad(data.root_pub, 'password'),
      },
      pwKey, b64ToBytes(pw.wrap),
    ));
  } catch {
    throw new Error('wrong passphrase (the key blob did not open)');
  }
  const kekKey = await webCrypto.subtle.importKey(
    'raw', masterKek, { name: 'AES-GCM', length: 256 }, false, ['decrypt'],
  );
  let seed;
  try {
    seed = new Uint8Array(await webCrypto.subtle.decrypt(
      {
        name: 'AES-GCM', iv: b64ToBytes(data.kek_seal.iv),
        additionalData: await v2SealAad(data.root_pub, data.factors),
      },
      kekKey, b64ToBytes(data.kek_seal.ct),
    ));
  } catch {
    masterKek.fill(0);
    throw new Error(
      'v2 master key does not open the seed seal — the factor list may '
      + 'have been altered',
    );
  }
  masterKek.fill(0);
  if (seed.length !== 32) {
    seed.fill(0);
    throw new Error('armor plaintext is not an Ed25519 signing seed');
  }
  return { seed, rootPub: data.root_pub };
}

async function encryptArmor(
  ed25519SigningSeed, rootPub, passphrase, iterations = V2_DEFAULT_ITERATIONS,
) {
  if (
    !Number.isSafeInteger(iterations)
    || iterations < V2_MIN_ITERATIONS
    || iterations > V2_MAX_ITERATIONS
  ) {
    throw new Error('iterations out of range');
  }
  // Copy the seed and the master KEK so both can be zeroed on every exit.
  const seed = new Uint8Array(ed25519SigningSeed);
  const masterKek = webCrypto.getRandomValues(new Uint8Array(32));
  try {
    const kekKey = await webCrypto.subtle.importKey(
      'raw', masterKek, { name: 'AES-GCM', length: 256 }, false, ['encrypt'],
    );
    const material = await webCrypto.subtle.importKey(
      'raw', textEncoder.encode(passphrase), 'PBKDF2', false, ['deriveKey'],
    );
    const salt = webCrypto.getRandomValues(new Uint8Array(16));
    const pwKey = await webCrypto.subtle.deriveKey(
      { name: 'PBKDF2', salt, iterations, hash: 'SHA-256' },
      material, { name: 'AES-GCM', length: 256 }, false, ['encrypt'],
    );
    const wrapIv = webCrypto.getRandomValues(new Uint8Array(12));
    const wrap = new Uint8Array(await webCrypto.subtle.encrypt(
      {
        name: 'AES-GCM', iv: wrapIv,
        additionalData: v2FactorAad(rootPub, 'password'),
      },
      pwKey, masterKek,
    ));
    const factors = [{
      type: 'password',
      kdf: {
        name: 'PBKDF2', hash: 'SHA-256', iterations, salt: bytesToB64(salt),
      },
      cipher: 'AES-256-GCM', iv: bytesToB64(wrapIv), wrap: bytesToB64(wrap),
    }];
    // Sealed LAST: the seal commits to the factor set, so the set must exist.
    const sealIv = webCrypto.getRandomValues(new Uint8Array(12));
    const sealCt = new Uint8Array(await webCrypto.subtle.encrypt(
      { name: 'AES-GCM', iv: sealIv, additionalData: await v2SealAad(rootPub, factors) },
      kekKey, seed,
    ));
    const body = canonicalJson({
      v: ARMOR_VERSION,
      root_pub: rootPub,
      kek_seal: {
        cipher: 'AES-256-GCM', iv: bytesToB64(sealIv), ct: bytesToB64(sealCt),
      },
      factors,
    });
    const b64 = bytesToB64(textEncoder.encode(body));
    return `${ARMOR_BEGIN}\n${b64.match(/.{1,64}/g).join('\n')}\n${ARMOR_END}`;
  } finally {
    seed.fill(0);
    masterKek.fill(0);
  }
}

// ── passkey factor (the browser half of idkit.armor's passkey factor) ─────
//
// The ceremony runs here, where the master KEK lives: open the armor with the
// password, seal the master KEK to a passkey's provisioning key (its public
// half), reseal the seed to the new factor set, and hand the updated armor to
// the server to store. Opening with a passkey is the passkey-only unlock. Byte-
// identical to idkit.armor, guarded by test_passkey_armor_factor_crossimpl.

async function v2MasterKekFromPassword(data, passphrase) {
  const pw = data.factors.find((f) => f.type === 'password');
  if (!pw) throw new Error('v2 armor has no password factor to open with a passphrase');
  const material = await webCrypto.subtle.importKey(
    'raw', textEncoder.encode(passphrase), 'PBKDF2', false, ['deriveKey'],
  );
  const pwKey = await webCrypto.subtle.deriveKey(
    {
      name: 'PBKDF2', salt: b64ToBytes(pw.kdf.salt),
      iterations: pw.kdf.iterations, hash: 'SHA-256',
    },
    material, { name: 'AES-GCM', length: 256 }, false, ['decrypt'],
  );
  try {
    return new Uint8Array(await webCrypto.subtle.decrypt(
      {
        name: 'AES-GCM', iv: b64ToBytes(pw.iv),
        additionalData: v2FactorAad(data.root_pub, 'password'),
      },
      pwKey, b64ToBytes(pw.wrap),
    ));
  } catch {
    throw new Error('wrong passphrase (the key blob did not open)');
  }
}

function emitV2(data) {
  const b64 = bytesToB64(textEncoder.encode(canonicalJson(data)));
  return `${ARMOR_BEGIN}\n${b64.match(/.{1,64}/g).join('\n')}\n${ARMOR_END}`;
}

// Recover the seed under the CURRENT commitment, then commit to the new one —
// the mutation these factor operations share. Needs the master KEK, which is
// exactly the authority that separates an owner editing their own locks from
// someone editing the file behind their back.
async function resealSeedToFactorSet(data, masterKek, previousFactors) {
  const kekKey = await webCrypto.subtle.importKey(
    'raw', masterKek, { name: 'AES-GCM', length: 256 }, false, ['decrypt', 'encrypt'],
  );
  let seed;
  try {
    seed = new Uint8Array(await webCrypto.subtle.decrypt(
      {
        name: 'AES-GCM', iv: b64ToBytes(data.kek_seal.iv),
        additionalData: await v2SealAad(data.root_pub, previousFactors),
      },
      kekKey, b64ToBytes(data.kek_seal.ct),
    ));
  } catch {
    throw new Error('could not recover the seed to reseal the factor set');
  }
  const sealIv = webCrypto.getRandomValues(new Uint8Array(12));
  const sealCt = new Uint8Array(await webCrypto.subtle.encrypt(
    {
      name: 'AES-GCM', iv: sealIv,
      additionalData: await v2SealAad(data.root_pub, data.factors),
    },
    kekKey, seed,
  ));
  seed.fill(0);
  data.kek_seal = {
    cipher: 'AES-256-GCM', iv: bytesToB64(sealIv), ct: bytesToB64(sealCt),
  };
}

async function addPasskeyFactor(armorText, passphrase, credentialId, passkeyKemPubHex) {
  if (typeof credentialId !== 'string' || !CREDENTIAL_ID_RE.test(credentialId)) {
    throw new Error('credential_id must be base64url (the WebAuthn rawId)');
  }
  if (typeof passkeyKemPubHex !== 'string' || !/^[0-9a-f]{64}$/.test(passkeyKemPubHex)) {
    throw new Error('passkey_kem_pub must be 64 lowercase hex chars');
  }
  const data = parseArmor(armorText);
  if (data.factors.some((f) => f.type === 'passkey' && f.credential_id === credentialId)) {
    throw new Error('this armor already carries a factor for that passkey');
  }
  const masterKek = await v2MasterKekFromPassword(data, passphrase);
  try {
    const previousFactors = data.factors.slice();
    const sealed = await sealToEncapsulationKey(
      masterKek, passkeyKemPubHex, PASSKEY_ARMOR_PURPOSE,
    );
    data.factors = [...data.factors, {
      type: 'passkey',
      credential_id: credentialId,
      kem_pub: passkeyKemPubHex,
      sealed: bytesToB64(sealed),
    }];
    await resealSeedToFactorSet(data, masterKek, previousFactors);
    return emitV2(parseArmor(emitV2(data)));
  } finally {
    masterKek.fill(0);
  }
}

async function removePasskeyFactor(armorText, passphrase, credentialId) {
  const data = parseArmor(armorText);
  const remaining = data.factors.filter(
    (f) => !(f.type === 'passkey' && f.credential_id === credentialId),
  );
  if (remaining.length === data.factors.length) {
    throw new Error('this armor carries no passkey factor for that credential');
  }
  if (remaining.length === 0) {
    throw new Error(
      'refusing to remove the last factor: an armor nothing can open is a '
      + 'destroyed identity, not a hardened one',
    );
  }
  const masterKek = await v2MasterKekFromPassword(data, passphrase);
  try {
    const previousFactors = data.factors.slice();
    data.factors = remaining;
    await resealSeedToFactorSet(data, masterKek, previousFactors);
    return emitV2(parseArmor(emitV2(data)));
  } finally {
    masterKek.fill(0);
  }
}

async function decryptArmorWithPasskey(armorText, prfOutput) {
  const data = parseArmor(armorText);
  const prf = new Uint8Array(prfOutput);
  const { publicKeyHex } = await deriveEncapsulationKeypair(prf, PASSKEY_ARMOR_PURPOSE);
  const factor = data.factors.find(
    (f) => f.type === 'passkey' && f.kem_pub === publicKeyHex,
  );
  if (!factor) {
    throw new Error('no passkey factor on this armor opens with that ceremony output');
  }
  let masterKek;
  try {
    masterKek = await openSealedArmor(
      {
        sealed_root_key: bytesToHex(b64ToBytes(factor.sealed)),
        seal_purpose: PASSKEY_ARMOR_PURPOSE,
      },
      prf,
    );
  } catch {
    throw new Error('the passkey factor does not open with that ceremony output');
  }
  const kekKey = await webCrypto.subtle.importKey(
    'raw', masterKek, { name: 'AES-GCM', length: 256 }, false, ['decrypt'],
  );
  let seed;
  try {
    seed = new Uint8Array(await webCrypto.subtle.decrypt(
      {
        name: 'AES-GCM', iv: b64ToBytes(data.kek_seal.iv),
        additionalData: await v2SealAad(data.root_pub, data.factors),
      },
      kekKey, b64ToBytes(data.kek_seal.ct),
    ));
  } catch {
    masterKek.fill(0);
    throw new Error(
      'v2 master key does not open the seed seal — the factor list may have '
      + 'been altered',
    );
  }
  masterKek.fill(0);
  if (seed.length !== 32) {
    seed.fill(0);
    throw new Error('armor plaintext is not an Ed25519 signing seed');
  }
  return { seed, rootPub: data.root_pub };
}

// Remove THE password factor — an armor carries at most one, so this is a
// single, unambiguous factor, not a bulk "every of a type" operation. Passkeys
// are plural and removed one at a time by credential via removePasskeyFactor;
// this never touches them. The last remaining factor can't be removed, so
// dropping the password requires another factor (a passkey) to already exist.
async function removePasswordFactor(armorText, passphrase) {
  const data = parseArmor(armorText);
  const remaining = data.factors.filter((f) => f.type !== 'password');
  if (remaining.length === data.factors.length) {
    throw new Error('this armor has no password factor to remove');
  }
  if (remaining.length === 0) {
    throw new Error('refusing to remove the last factor — add a passkey first');
  }
  const masterKek = await v2MasterKekFromPassword(data, passphrase);
  try {
    const previousFactors = data.factors.slice();
    data.factors = remaining;
    await resealSeedToFactorSet(data, masterKek, previousFactors);
    return emitV2(parseArmor(emitV2(data)));
  } finally {
    masterKek.fill(0);
  }
}

// The re-arm signing domain — matches idkit's REARMOR_DOMAIN byte-for-byte, so
// a signature the browser makes verifies against the root server-side.
const REARMOR_DOMAIN = 'autonomy.identity.rearmor.v1\n';

// Produce the signed body POST /api/identity/personal/armor expects: re-factor
// the CURRENT armor per *action*, sign the result with the root (recovered from
// the current armor, proving possession), and return {armored_private_key,
// signature, require_pair?}. The password opens the current armor for both the
// re-factoring and the signing seed; opening a passkey-only identity with a PRF
// instead is a later addition.
//
//   action = {kind:'promote', credentialId, provisioningPub}
//          | {kind:'demote',  credentialId}
//          | {kind:'removePassword'}
//          | {kind:'setAuthority'}   // policy-only; armor unchanged
async function signArmorUpdate(currentArmor, password, action, requirePair) {
  const opened = await decryptArmor(currentArmor, password);
  const seed = opened.seed;
  try {
    let newArmor = currentArmor;
    if (action.kind === 'promote') {
      newArmor = await addPasskeyFactor(
        currentArmor, password, action.credentialId, action.provisioningPub);
    } else if (action.kind === 'demote') {
      newArmor = await removePasskeyFactor(
        currentArmor, password, action.credentialId);
    } else if (action.kind === 'removePassword') {
      newArmor = await removePasswordFactor(currentArmor, password);
    } else if (action.kind !== 'setAuthority') {
      throw new Error(`unknown re-arm action: ${action.kind}`);
    }
    const rootKey = await importEd25519RootSigningKey(seed);
    const sig = bytesToHex(await webCrypto.subtle.sign(
      'Ed25519', rootKey, textEncoder.encode(REARMOR_DOMAIN + newArmor)));
    const body = { armored_private_key: newArmor, signature: sig };
    if (requirePair !== undefined && requirePair !== null) {
      body.require_pair = !!requirePair;
    }
    return body;
  } finally {
    seed.fill(0);
  }
}

// One-shot v1 -> v2 upgrade. Opens the legacy v1 blob and re-seals it as v2. The
// v1 reader is deleted with the rest of the v1 path once migration has run.
function armorVersion(armorText) {
  if (typeof armorText !== 'string') throw new Error('armor must be text');
  const lines = armorText
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
  if (
    lines.length < 3
    || lines[0] !== ARMOR_BEGIN
    || lines[lines.length - 1] !== ARMOR_END
  ) {
    throw new Error('this is not an auto.network root key armor');
  }
  let data;
  try {
    data = JSON.parse(new TextDecoder().decode(b64ToBytes(lines.slice(1, -1).join(''))));
  } catch {
    throw new Error('armor body does not decode');
  }
  if (data.v !== 1 && data.v !== 2) {
    throw new Error(`unsupported armor version: ${data.v}`);
  }
  return data.v;
}

// Open a v1 OR v2 armor. The v1 branch exists only to read a pre-migration blob
// and is deleted from the shipped product once migration has run.
export {
  canonicalJson,
  hexToBytes,
  bytesToHex,
  b64ToBytes,
  domainBytes,
  decryptArmor,
  decryptArmorWithPasskey,
  addPasskeyFactor,
  removePasskeyFactor,
  removePasswordFactor,
  signArmorUpdate,
  REARMOR_DOMAIN,
  PASSKEY_ARMOR_PURPOSE,
  armorVersion,
  encryptArmor,
  parseArmor,
  importEd25519RootSigningKey,
};

export {
  SUITE_X25519_HKDF_SHA256_CHACHA20POLY1305,
  deriveEncapsulationKeypair,
  openSealedArmor,
  sealToEncapsulationKey,
} from './sealing.js';
