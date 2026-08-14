/*
 * WebCrypto-only organization identity and D21 registration ceremonies.
 *
 * Browser UI wiring and persistence live outside this module. The headless
 * client writes armor to an explicit caller-owned path; the only plaintext
 * private material here is a transient 32-byte Ed25519 signing seed.
 */

import {
  canonicalJson,
  bytesToHex,
  domainBytes,
  importEd25519RootSigningKey,
} from './primitives.js';

const ARMOR_BEGIN = '-----BEGIN AUTONOMY NETWORK ROOT KEY-----';
const ARMOR_END = '-----END AUTONOMY NETWORK ROOT KEY-----';
const ARMOR_AAD_PREFIX = 'autonomy.idkit.armor.v1\n';
const REQUEST_DOMAIN = 'autonomy.network.registry.request.v1\n';
const REGISTRATION_PATH = '/v1/orgs';
const DEFAULT_ITERATIONS = 600000;
const MIN_ITERATIONS = 10000;
const MAX_ITERATIONS = 100000000;

const PKCS8_ED25519_PREFIX_LENGTH = 16;
const textEncoder = new TextEncoder();

let webCrypto = globalThis.crypto;
if (
  !webCrypto
  && typeof process !== 'undefined'
  && process.versions?.node
) {
  ({ webcrypto: webCrypto } = await import('node:crypto'));
}
if (!webCrypto?.subtle) {
  throw new Error('organization ceremonies require WebCrypto');
}

let armorIterations = DEFAULT_ITERATIONS;

function bytesToB64(bytes) {
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function assertRootPub(rootPubHex, name = 'root_pub') {
  if (
    typeof rootPubHex !== 'string'
    || !/^[0-9a-f]{64}$/.test(rootPubHex)
  ) {
    throw new Error(`${name} must be 64 lowercase hex characters`);
  }
}

function setArmorIterationsForTest(iterations) {
  if (
    !Number.isSafeInteger(iterations)
    || iterations < MIN_ITERATIONS
    || iterations > MAX_ITERATIONS
  ) {
    throw new Error(
      `armor iterations must be in [${MIN_ITERATIONS}, ${MAX_ITERATIONS}]`,
    );
  }
  armorIterations = iterations;
}

async function generateOrgRootKey() {
  const keyPair = await webCrypto.subtle.generateKey(
    { name: 'Ed25519' },
    true,
    ['sign', 'verify'],
  );
  const publicBytes = new Uint8Array(
    await webCrypto.subtle.exportKey('raw', keyPair.publicKey),
  );
  const pkcs8 = new Uint8Array(
    await webCrypto.subtle.exportKey('pkcs8', keyPair.privateKey),
  );
  if (pkcs8.length < PKCS8_ED25519_PREFIX_LENGTH + 32) {
    pkcs8.fill(0);
    throw new Error('unexpected Ed25519 PKCS#8 shape from WebCrypto');
  }
  const seed = pkcs8.slice(pkcs8.length - 32);
  pkcs8.fill(0);
  return {
    seed,
    pubHex: bytesToHex(publicBytes),
  };
}

async function armorSeed(
  ed25519SigningSeed,
  rootPubHex,
  passphrase,
  iterations = armorIterations,
) {
  const seed = new Uint8Array(ed25519SigningSeed);
  if (seed.length !== 32) {
    throw new Error('Ed25519 root signing seed must be exactly 32 bytes');
  }
  assertRootPub(rootPubHex);
  if (typeof passphrase !== 'string' || !passphrase) {
    throw new Error('passphrase must be a non-empty string');
  }
  if (
    !Number.isSafeInteger(iterations)
    || iterations < MIN_ITERATIONS
    || iterations > MAX_ITERATIONS
  ) {
    throw new Error(
      `armor iterations must be in [${MIN_ITERATIONS}, ${MAX_ITERATIONS}]`,
    );
  }

  const salt = webCrypto.getRandomValues(new Uint8Array(16));
  const iv = webCrypto.getRandomValues(new Uint8Array(12));
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
      salt,
      iterations,
      hash: 'SHA-256',
    },
    passphraseMaterial,
    { name: 'AES-GCM', length: 256 },
    false,
    ['encrypt'],
  );
  const ciphertext = new Uint8Array(await webCrypto.subtle.encrypt(
    {
      name: 'AES-GCM',
      iv,
      additionalData: textEncoder.encode(
        ARMOR_AAD_PREFIX + rootPubHex,
      ),
    },
    armorKey,
    seed,
  ));
  const body = canonicalJson({
    v: 1,
    kdf: {
      name: 'PBKDF2',
      hash: 'SHA-256',
      iterations,
      salt: bytesToB64(salt),
    },
    cipher: {
      name: 'AES-256-GCM',
      iv: bytesToB64(iv),
    },
    root_pub: rootPubHex,
    ct: bytesToB64(ciphertext),
  });
  const encodedBody = btoa(body);
  const lines = [ARMOR_BEGIN];
  for (let index = 0; index < encodedBody.length; index += 64) {
    lines.push(encodedBody.slice(index, index + 64));
  }
  lines.push(ARMOR_END);
  return lines.join('\n');
}

function validateRegistrationPayload(payload, rootPubHex) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error('registration payload must be an object');
  }
  const allowed = new Set([
    'root_pub',
    'recovery_policy',
    'recovery_pub',
  ]);
  for (const key of Object.keys(payload)) {
    if (!allowed.has(key)) {
      throw new Error(`D21 registration payload does not allow ${key}`);
    }
  }
  if (payload.root_pub !== rootPubHex) {
    throw new Error('registration root_pub must equal the signing root');
  }
  assertRootPub(payload.root_pub);
  if (
    payload.recovery_policy !== 'none'
    && payload.recovery_policy !== 'recovery-key'
  ) {
    throw new Error(
      'recovery_policy must be none or recovery-key',
    );
  }
  if (payload.recovery_policy === 'recovery-key') {
    assertRootPub(payload.recovery_pub, 'recovery_pub');
    // The recovery factor must be a DISTINCT key -- naming the root as its own
    // recovery key is no second factor at all (a stolen root signs both), the
    // exact self-defeat rejected at genesis (_v_genesis). Close it on this
    // write path too.
    if (payload.recovery_pub === payload.root_pub) {
      throw new Error(
        'recovery_pub must differ from root_pub — a recovery factor the root '
        + 'controls is no second factor',
      );
    }
  } else if (payload.recovery_pub !== undefined) {
    throw new Error(
      'recovery_pub is only valid with recovery-key policy',
    );
  }
}

async function buildRegistrationEnvelope(
  rootSigningKey,
  rootPubHex,
  payload,
  timestamp = Math.floor(Date.now() / 1000),
) {
  assertRootPub(rootPubHex);
  validateRegistrationPayload(payload, rootPubHex);
  if (!Number.isSafeInteger(timestamp)) {
    throw new Error('registration timestamp must be an integer');
  }
  const signingInput = domainBytes(
    REQUEST_DOMAIN,
    canonicalJson({
      v: 1,
      method: 'POST',
      path: REGISTRATION_PATH,
      ts: timestamp,
      signer: rootPubHex,
      payload,
    }),
  );
  const signature = await webCrypto.subtle.sign(
    'Ed25519',
    rootSigningKey,
    signingInput,
  );
  return {
    v: 1,
    signer: rootPubHex,
    ts: timestamp,
    payload,
    sig: bytesToHex(signature),
  };
}

function buildRecoveryBlock(
  orgUuid,
  rootPubHex,
  recoveryPubHex,
  recoverySeedHex,
) {
  assertRootPub(rootPubHex);
  assertRootPub(recoveryPubHex, 'recovery_pub');
  if (
    typeof recoverySeedHex !== 'string'
    || !/^[0-9a-f]{64}$/.test(recoverySeedHex)
  ) {
    throw new Error('recovery seed must be 64 lowercase hex characters');
  }
  return [
    '========== AUTONOMY NETWORK RECOVERY KEY — KEEP OFFLINE ==========',
    '',
    `Org UUID:          ${orgUuid}`,
    `Org root pub:      ${rootPubHex}`,
    `Recovery pub:      ${recoveryPubHex}`,
    `Recovery seed:     ${recoverySeedHex}`,
    `Created:           ${new Date().toISOString()}`,
    '',
    'This is the ONLY copy. It is not stored on any server or in this',
    'process. Anyone holding the recovery seed can rebind the org',
    'identity to a new root key under the pre-declared recovery policy.',
    'Print it or write it down, keep it cold, and never paste it anywhere',
    'except a rebind ceremony.',
    '==================================================================',
  ].join('\n');
}

export {
  armorSeed,
  buildRecoveryBlock,
  buildRegistrationEnvelope,
  generateOrgRootKey,
  importEd25519RootSigningKey,
  setArmorIterationsForTest,
};
