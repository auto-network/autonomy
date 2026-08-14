/*
 * WebCrypto-only primitives shared by browser and Node ceremonies.
 *
 * Canonical JSON and armor behavior mirror tools/network/idkit. Keep this
 * module free of DOM, storage, and transport dependencies.
 */

const ARMOR_AAD_PREFIX = 'autonomy.idkit.armor.v1\n';
const ARMOR_BEGIN = '-----BEGIN AUTONOMY NETWORK ROOT KEY-----';
const ARMOR_END = '-----END AUTONOMY NETWORK ROOT KEY-----';

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

async function decryptArmor(armorText, passphrase) {
  if (typeof armorText !== 'string') {
    throw new Error('armor must be text');
  }
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
    !sameKeys(data, ['v', 'kdf', 'cipher', 'root_pub', 'ct'])
    || data.v !== 1
    || !sameKeys(data.kdf, ['name', 'hash', 'iterations', 'salt'])
    || data.kdf.name !== 'PBKDF2'
    || data.kdf.hash !== 'SHA-256'
    || !Number.isSafeInteger(data.kdf.iterations)
    || data.kdf.iterations < 10000
    || data.kdf.iterations > 100000000
    || !sameKeys(data.cipher, ['name', 'iv'])
    || data.cipher.name !== 'AES-256-GCM'
    || typeof data.root_pub !== 'string'
    || !/^[0-9a-f]{64}$/.test(data.root_pub)
    || canonicalBase64Length(data.kdf.salt) !== 16
    || canonicalBase64Length(data.cipher.iv) !== 12
    || canonicalBase64Length(data.ct) !== 48
  ) {
    throw new Error('unsupported or non-canonical armor format');
  }

  const passphraseMaterial = await webCrypto.subtle.importKey(
    'raw',
    textEncoder.encode(passphrase),
    'PBKDF2',
    false,
    ['deriveKey'],
  );
  const armorKey = await webCrypto.subtle.deriveKey(
    {
      name: 'PBKDF2',
      salt: b64ToBytes(data.kdf.salt),
      iterations: data.kdf.iterations,
      hash: 'SHA-256',
    },
    passphraseMaterial,
    { name: 'AES-GCM', length: 256 },
    false,
    ['decrypt'],
  );

  let ed25519SigningSeed;
  try {
    ed25519SigningSeed = new Uint8Array(await webCrypto.subtle.decrypt(
      {
        name: 'AES-GCM',
        iv: b64ToBytes(data.cipher.iv),
        additionalData: textEncoder.encode(
          ARMOR_AAD_PREFIX + data.root_pub,
        ),
      },
      armorKey,
      b64ToBytes(data.ct),
    ));
  } catch {
    throw new Error('wrong passphrase (the key blob did not open)');
  }
  if (ed25519SigningSeed.length !== 32) {
    throw new Error('armor plaintext is not an Ed25519 signing seed');
  }
  return {
    seed: ed25519SigningSeed,
    rootPub: data.root_pub,
  };
}

/**
 * Import a raw 32-byte Ed25519 root SIGNING seed as a non-extractable key.
 *
 * This function always interprets its input as Ed25519 signing material; it
 * never imports an X25519 encapsulation key. The raw key shapes overlap, so
 * callers must use role-specific sources and this module exposes no ambiguous
 * generic key importer.
 */
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

const ARMOR_VERSION_2 = 2;
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

function v2SealAad(rootPub) {
  return textEncoder.encode(V2_SEAL_AAD + rootPub);
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
const V2_FACTOR_PARSERS = { password: parseV2PasswordFactor };

function parseArmorV2(armorText) {
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
    if (seen.has(f.type)) throw new Error(`v2 duplicate factor type ${f.type}`);
    seen.add(f.type);
    parser(f); // total dispatch — a known type always has a strict parser
  }
  return data;
}

async function decryptArmorV2(armorText, passphrase) {
  const data = parseArmorV2(armorText);
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
        additionalData: v2SealAad(data.root_pub),
      },
      kekKey, b64ToBytes(data.kek_seal.ct),
    ));
  } catch {
    masterKek.fill(0);
    throw new Error('v2 master key does not open the seed seal');
  }
  masterKek.fill(0);
  if (seed.length !== 32) {
    seed.fill(0);
    throw new Error('armor plaintext is not an Ed25519 signing seed');
  }
  return { seed, rootPub: data.root_pub };
}

async function encryptArmorV2(
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
    const sealIv = webCrypto.getRandomValues(new Uint8Array(12));
    const sealCt = new Uint8Array(await webCrypto.subtle.encrypt(
      { name: 'AES-GCM', iv: sealIv, additionalData: v2SealAad(rootPub) },
      kekKey, seed,
    ));
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
    const body = canonicalJson({
      v: ARMOR_VERSION_2,
      root_pub: rootPub,
      kek_seal: {
        cipher: 'AES-256-GCM', iv: bytesToB64(sealIv), ct: bytesToB64(sealCt),
      },
      factors: [{
        type: 'password',
        kdf: {
          name: 'PBKDF2', hash: 'SHA-256', iterations, salt: bytesToB64(salt),
        },
        cipher: 'AES-256-GCM', iv: bytesToB64(wrapIv), wrap: bytesToB64(wrap),
      }],
    });
    const b64 = bytesToB64(textEncoder.encode(body));
    return `${ARMOR_BEGIN}\n${b64.match(/.{1,64}/g).join('\n')}\n${ARMOR_END}`;
  } finally {
    seed.fill(0);
    masterKek.fill(0);
  }
}

// One-shot v1 -> v2 upgrade. Opens the legacy v1 blob and re-seals it as v2. The
// v1 reader is deleted with the rest of the v1 path once migration has run.
async function migrateArmorV1ToV2(
  v1ArmorText, passphrase, iterations = V2_DEFAULT_ITERATIONS,
) {
  const opened = await decryptArmor(v1ArmorText, passphrase);
  try {
    return await encryptArmorV2(opened.seed, opened.rootPub, passphrase, iterations);
  } finally {
    opened.seed.fill(0);
  }
}

export {
  canonicalJson,
  hexToBytes,
  bytesToHex,
  b64ToBytes,
  domainBytes,
  decryptArmor,
  decryptArmorV2,
  encryptArmorV2,
  migrateArmorV1ToV2,
  parseArmorV2,
  importEd25519RootSigningKey,
};

export {
  SUITE_X25519_HKDF_SHA256_CHACHA20POLY1305,
  deriveEncapsulationKeypair,
  openSealedArmor,
  sealToEncapsulationKey,
} from './sealing.js';
