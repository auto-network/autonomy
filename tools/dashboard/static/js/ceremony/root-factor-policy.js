/* Browser mirror of tools/network/idkit/root_factor_policy.py.
 *
 * This module compiles a canonical AND/OR factor expression into HPKE-wrapped
 * shares of the personal Ed25519 root.  It uses only the existing vetted
 * sealing.js construction plus WebCrypto PBKDF2/AES-GCM/Ed25519.  Passwords,
 * PRF outputs, factor seeds, and the root seed remain browser-local.
 */

import {
  canonicalJson,
  hexToBytes,
  bytesToHex,
  importEd25519RootSigningKey,
} from './primitives.js';
import { deriveRecoveryFactors } from './recovery.js';
import {
  deriveEncapsulationKeypair,
  openWithEncapsulationPrivateKey,
  sealToEncapsulationKey,
} from './sealing.js';

const ARMOR_BEGIN = '-----BEGIN AUTONOMY NETWORK ROOT KEY-----';
const ARMOR_END = '-----END AUTONOMY NETWORK ROOT KEY-----';
const ARMOR_VERSION = 3;
const POLICY_VERSION = 1;
const POLICY_SIGNATURE_DOMAIN = 'autonomy.identity.root-factor-policy.v1\n';
const TRANSITION_DOMAIN = 'autonomy.identity.factor-policy-transition.v1\n';
// Root membership is crypto-separated from a standalone vault-class factor,
// even when the same physical password/passkey gesture is enrolled in both.
const FACTOR_RECIPIENT_PURPOSE = 'autonomy/root-factor-recipient/v1';
const FACTOR_ACCESS_DERIVE_INFO = 'autonomy.identity.factor-access.v1\n';
const PASSWORD_FACTOR_AAD = 'autonomy.identity.password-factor.v1\n';
const WRAP_PURPOSE_PREFIX = 'autonomy/root-policy-wrap/v1';
const MAX_FACTORS = 32;
const MAX_POLICY_DEPTH = 16;
const textEncoder = new TextEncoder();
const textDecoder = new TextDecoder();

let webCrypto = globalThis.crypto;
if (!webCrypto && typeof process !== 'undefined' && process.versions?.node) {
  ({ webcrypto: webCrypto } = await import('node:crypto'));
}

function bytesToB64(bytes) {
  let binary = '';
  for (const byte of new Uint8Array(bytes)) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function b64ToBytes(value) {
  if (typeof value !== 'string') throw new Error('expected canonical base64');
  let binary;
  try { binary = atob(value); } catch { throw new Error('expected canonical base64'); }
  const out = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  if (bytesToB64(out) !== value) throw new Error('expected canonical base64');
  return out;
}

function bytesToB64url(bytes) {
  return bytesToB64(bytes).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function b64urlToBytes(value) {
  let encoded = value.replace(/-/g, '+').replace(/_/g, '/');
  while (encoded.length % 4) encoded += '=';
  return b64ToBytes(encoded);
}

function concatBytes(...parts) {
  const size = parts.reduce((total, part) => total + part.length, 0);
  const out = new Uint8Array(size);
  let offset = 0;
  for (const part of parts) { out.set(part, offset); offset += part.length; }
  return out;
}

function sameKeys(value, keys) {
  return value && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).sort().join(',') === keys.slice().sort().join(',');
}

function requireFactorId(value) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value)) {
    throw new Error('factor_id has an invalid shape');
  }
  return value;
}

function requirePublicKey(value, what) {
  if (typeof value !== 'string' || !/^[0-9a-f]{64}$/.test(value)) {
    throw new Error(`${what} must be 64 lowercase hex characters`);
  }
  return value;
}

function canonicalExpression(value, leaves = [], depth = 1) {
  if (depth > MAX_POLICY_DEPTH) throw new Error('root policy is too deep');
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('policy node must be an object');
  }
  if (value.op === 'factor') {
    if (!sameKeys(value, ['op', 'factor_id'])) throw new Error('factor node is malformed');
    const factorId = requireFactorId(value.factor_id);
    if (leaves.includes(factorId)) throw new Error(`factor ${factorId} occurs more than once`);
    leaves.push(factorId);
    if (leaves.length > MAX_FACTORS) throw new Error('root policy has too many factors');
    return { op: 'factor', factor_id: factorId };
  }
  if (!['and', 'or'].includes(value.op) || !sameKeys(value, ['op', 'children'])
      || !Array.isArray(value.children) || value.children.length < 2) {
    throw new Error('policy node must be factor or an and/or with at least two children');
  }
  const children = value.children
    .map((child) => canonicalExpression(child, leaves, depth + 1))
    .sort((left, right) => {
      const a = canonicalJson(left); const b = canonicalJson(right);
      return a < b ? -1 : (a > b ? 1 : 0);
    });
  return { op: value.op, children };
}

function policyFactorIds(policy) {
  const ids = [];
  (function walk(node) {
    if (node.op === 'factor') ids.push(node.factor_id);
    else node.children.forEach(walk);
  }(canonicalExpression(policy)));
  return ids;
}

function policySatisfied(policy, suppliedFactorIds) {
  const supplied = new Set(suppliedFactorIds);
  function evaluate(node) {
    if (node.op === 'factor') return supplied.has(node.factor_id);
    const values = node.children.map(evaluate);
    return node.op === 'and' ? values.every(Boolean) : values.some(Boolean);
  }
  return evaluate(canonicalExpression(policy));
}

function parseFactor(value, rootPub) {
  const factorId = requireFactorId(value?.factor_id);
  if (value.type === 'passkey') {
    if (!sameKeys(value, ['factor_id', 'type', 'credential_id', 'recipients'])) {
      throw new Error('passkey factor is malformed');
    }
    if (typeof value.credential_id !== 'string'
        || !/^[A-Za-z0-9_-]{1,256}$/.test(value.credential_id)) {
      throw new Error('credential_id must be canonical base64url');
    }
    if (!Array.isArray(value.recipients) || value.recipients.length > MAX_FACTORS) {
      throw new Error('passkey recipients must be a bounded array');
    }
    const recipients = value.recipients.map((row) => {
      if (!sameKeys(row, ['recipient_public_key', 'label', 'created_at'])
          || typeof row.label !== 'string' || !row.label.trim() || row.label.trim().length > 120
          || typeof row.created_at !== 'string'
          || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(row.created_at)) {
        throw new Error('passkey recipient is malformed');
      }
      return {
        recipient_public_key: requirePublicKey(
          row.recipient_public_key, 'passkey recipient',
        ),
        label: row.label.trim(),
        created_at: row.created_at,
      };
    }).sort((left, right) => left.recipient_public_key.localeCompare(right.recipient_public_key));
    if (new Set(recipients.map((row) => row.recipient_public_key)).size !== recipients.length) {
      throw new Error('passkey recipient public keys must be unique');
    }
    return {
      factor_id: factorId,
      type: 'passkey',
      credential_id: value.credential_id,
      recipients,
    };
  }
  if (value.type !== 'password'
      || !sameKeys(value, ['factor_id', 'type', 'recipient_public_key',
        'access_public_key', 'protector'])) {
    throw new Error('password factor is malformed');
  }
  const protector = value.protector;
  const kdf = protector?.kdf;
  if (!sameKeys(protector, ['kdf', 'cipher', 'iv', 'wrapped_seed'])
      || protector.cipher !== 'AES-256-GCM'
      || !sameKeys(kdf, ['name', 'hash', 'iterations', 'salt'])
      || kdf.name !== 'PBKDF2' || kdf.hash !== 'SHA-256'
      || !Number.isSafeInteger(kdf.iterations)
      || kdf.iterations < 10000 || kdf.iterations > 100000000
      || b64ToBytes(kdf.salt).length !== 16
      || b64ToBytes(protector.iv).length !== 12
      || b64ToBytes(protector.wrapped_seed).length !== 48) {
    throw new Error('password factor protector is malformed');
  }
  return {
    factor_id: factorId,
    type: 'password',
    recipient_public_key: requirePublicKey(value.recipient_public_key, 'factor recipient'),
    access_public_key: requirePublicKey(value.access_public_key, 'factor access key'),
    protector: JSON.parse(JSON.stringify(protector)),
  };
}

function validateState(rootPub, factors, access, policy) {
  requirePublicKey(rootPub, 'root_pub');
  if (!Array.isArray(factors) || factors.length > MAX_FACTORS) {
    throw new Error('factor inventory is malformed');
  }
  const parsedFactors = factors.map((factor) => parseFactor(factor, rootPub))
    .sort((left, right) => left.factor_id.localeCompare(right.factor_id));
  const ids = parsedFactors.map((factor) => factor.factor_id);
  if (new Set(ids).size !== ids.length) throw new Error('factor ids must be unique');
  const credentialIds = parsedFactors
    .filter((factor) => factor.type === 'passkey')
    .map((factor) => factor.credential_id);
  if (new Set(credentialIds).size !== credentialIds.length) {
    throw new Error('one passkey credential must be represented by one logical factor');
  }
  const parsedPolicy = canonicalExpression(policy);
  const memberRecipients = [];
  for (const factorId of policyFactorIds(parsedPolicy)) {
    const factor = parsedFactors.find((candidate) => candidate.factor_id === factorId);
    if (!factor) throw new Error(`root policy names unknown factor ${factorId}`);
    const recipients = factor.type === 'password'
      ? [factor.recipient_public_key]
      : factor.recipients.map((row) => row.recipient_public_key);
    if (!recipients.length) throw new Error(`factor ${factorId} cannot derive root material`);
    memberRecipients.push(...recipients);
  }
  if (new Set(memberRecipients).size !== memberRecipients.length) {
    throw new Error('root policy repeats one cryptographic recipient under multiple factor ids');
  }
  if (!Array.isArray(access) || access.some((factorId) => !ids.includes(factorId))) {
    throw new Error('dashboard access names an unknown factor');
  }
  return {
    factors: parsedFactors,
    access: [...new Set(access)].sort(),
    policy: parsedPolicy,
  };
}

async function sha256Hex(bytes) {
  return bytesToHex(await webCrypto.subtle.digest('SHA-256', bytes));
}

function passwordAad(rootPub, factorId, recipientPublicKey, accessPublicKey) {
  return concatBytes(textEncoder.encode(PASSWORD_FACTOR_AAD), textEncoder.encode(canonicalJson({
    access_public_key: accessPublicKey,
    factor_id: factorId,
    recipient_public_key: recipientPublicKey,
    root_pub: rootPub,
  })));
}

async function hkdf(seed, info) {
  const key = await webCrypto.subtle.importKey('raw', seed, 'HKDF', false, ['deriveBits']);
  return new Uint8Array(await webCrypto.subtle.deriveBits({
    name: 'HKDF', hash: 'SHA-256', salt: new Uint8Array(32),
    info: textEncoder.encode(info),
  }, key, 256));
}

async function accessPublicKey(seed) {
  const derived = await hkdf(seed, FACTOR_ACCESS_DERIVE_INFO);
  try {
    // Import a short-lived extractable Ed25519 key solely to obtain its public
    // `x`; ordinary root signing keys remain non-extractable.
    const prefix = Uint8Array.from([
      0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06,
      0x03, 0x2b, 0x65, 0x70, 0x04, 0x22, 0x04, 0x20,
    ]);
    const pkcs8 = concatBytes(prefix, derived);
    try {
      const extractable = await webCrypto.subtle.importKey(
        'pkcs8', pkcs8, { name: 'Ed25519' }, true, ['sign'],
      );
      const jwk = await webCrypto.subtle.exportKey('jwk', extractable);
      return bytesToHex(b64urlToBytes(jwk.x));
    } finally { pkcs8.fill(0); }
  } finally { derived.fill(0); }
}

async function importFactorAccessSigningKey(seed) {
  const derived = await hkdf(seed, FACTOR_ACCESS_DERIVE_INFO);
  try { return await importEd25519RootSigningKey(derived); }
  finally { derived.fill(0); }
}

async function createPasswordFactor(rootPub, factorId, password, iterations = 600000) {
  requirePublicKey(rootPub, 'root_pub');
  requireFactorId(factorId);
  if (typeof password !== 'string' || !password) throw new Error('password must be non-empty');
  if (!Number.isSafeInteger(iterations) || iterations < 10000 || iterations > 100000000) {
    throw new Error('password KDF iterations are out of range');
  }
  const seed = webCrypto.getRandomValues(new Uint8Array(32));
  const recipient = await deriveEncapsulationKeypair(seed, FACTOR_RECIPIENT_PURPOSE);
  const accessPub = await accessPublicKey(seed);
  const salt = webCrypto.getRandomValues(new Uint8Array(16));
  const material = await webCrypto.subtle.importKey(
    'raw', textEncoder.encode(password), 'PBKDF2', false, ['deriveKey'],
  );
  const key = await webCrypto.subtle.deriveKey({
    name: 'PBKDF2', hash: 'SHA-256', salt, iterations,
  }, material, { name: 'AES-GCM', length: 256 }, false, ['encrypt']);
  const iv = webCrypto.getRandomValues(new Uint8Array(12));
  const wrapped = new Uint8Array(await webCrypto.subtle.encrypt({
    name: 'AES-GCM', iv,
    additionalData: passwordAad(rootPub, factorId, recipient.publicKeyHex, accessPub),
  }, key, seed));
  return {
    factor: {
      factor_id: factorId,
      type: 'password',
      recipient_public_key: recipient.publicKeyHex,
      access_public_key: accessPub,
      protector: {
        kdf: { name: 'PBKDF2', hash: 'SHA-256', iterations, salt: bytesToB64(salt) },
        cipher: 'AES-256-GCM', iv: bytesToB64(iv), wrapped_seed: bytesToB64(wrapped),
      },
    },
    seed,
  };
}

async function openPasswordFactor(rootPub, factorValue, password) {
  const factor = parseFactor(factorValue, rootPub);
  if (factor.type !== 'password') throw new Error('selected factor is not a password');
  const kdf = factor.protector.kdf;
  const material = await webCrypto.subtle.importKey(
    'raw', textEncoder.encode(password), 'PBKDF2', false, ['deriveKey'],
  );
  const key = await webCrypto.subtle.deriveKey({
    name: 'PBKDF2', hash: 'SHA-256', salt: b64ToBytes(kdf.salt),
    iterations: kdf.iterations,
  }, material, { name: 'AES-GCM', length: 256 }, false, ['decrypt']);
  let seed;
  try {
    seed = new Uint8Array(await webCrypto.subtle.decrypt({
      name: 'AES-GCM', iv: b64ToBytes(factor.protector.iv),
      additionalData: passwordAad(
        rootPub, factor.factor_id, factor.recipient_public_key, factor.access_public_key,
      ),
    }, key, b64ToBytes(factor.protector.wrapped_seed)));
  } catch { throw new Error('password factor did not open'); }
  const recipient = await deriveEncapsulationKeypair(seed, FACTOR_RECIPIENT_PURPOSE);
  if (recipient.publicKeyHex !== factor.recipient_public_key
      || await accessPublicKey(seed) !== factor.access_public_key) {
    seed.fill(0);
    throw new Error('password factor seed does not match its public keys');
  }
  return seed;
}

function xor(parts) {
  const out = new Uint8Array(32);
  for (const part of parts) {
    if (part.length !== 32) throw new Error('policy shares must be 32 bytes');
    for (let index = 0; index < 32; index += 1) out[index] ^= part[index];
  }
  return out;
}

async function policyDigest(generation, rootPub, factors, access, policy) {
  return sha256Hex(textEncoder.encode(canonicalJson({
    access, factors, generation, policy, root_pub: rootPub, v: POLICY_VERSION,
  })));
}

function wrapPurpose(digest, path) {
  return `${WRAP_PURPOSE_PREFIX}/${digest}/${path}`;
}

async function compileNode(node, secret, recipients, digest, path) {
  if (node.op === 'factor') {
    return {
      op: 'factor',
      factor_id: node.factor_id,
      sealed: await Promise.all(recipients[node.factor_id].map(async (publicKey) => ({
        recipient_public_key: publicKey,
        sealed: bytesToB64(await sealToEncapsulationKey(
          secret, publicKey, wrapPurpose(digest, path),
        )),
      }))),
    };
  }
  if (node.op === 'or') {
    return {
      op: 'or',
      children: await Promise.all(node.children.map(
        (child, index) => compileNode(child, secret, recipients, digest, `${path}.${index}`),
      )),
    };
  }
  const shares = node.children.slice(0, -1).map(
    () => webCrypto.getRandomValues(new Uint8Array(32)),
  );
  shares.push(xor([secret, ...shares]));
  try {
    return {
      op: 'and',
      children: await Promise.all(node.children.map(
        (child, index) => compileNode(child, shares[index], recipients, digest, `${path}.${index}`),
      )),
    };
  } finally { shares.forEach((share) => share.fill(0)); }
}

async function verifyRootSignature(rootPub, signatureHex, message) {
  const key = await webCrypto.subtle.importKey(
    'raw', hexToBytes(rootPub), { name: 'Ed25519' }, false, ['verify'],
  );
  return webCrypto.subtle.verify('Ed25519', key, hexToBytes(signatureHex), message);
}

async function parseFactorPolicyArmor(armorText) {
  if (typeof armorText !== 'string') throw new Error('factor policy armor must be text');
  const lines = armorText.split('\n').map((line) => line.trim()).filter(Boolean);
  if (lines.length < 3 || lines[0] !== ARMOR_BEGIN || lines.at(-1) !== ARMOR_END) {
    throw new Error('factor policy armor has no BEGIN/END lines');
  }
  const raw = b64ToBytes(lines.slice(1, -1).join(''));
  let body;
  try { body = JSON.parse(textDecoder.decode(raw)); } catch { throw new Error('armor body does not decode'); }
  if (!sameKeys(body, ['v', 'factor_policy']) || body.v !== ARMOR_VERSION
      || textDecoder.decode(raw) !== canonicalJson(body)) {
    throw new Error('factor policy armor is not canonical v3');
  }
  const envelope = body.factor_policy;
  const baseKeys = ['v', 'generation', 'root_pub', 'factors', 'access', 'policy', 'wraps', 'signature'];
  const keys = Object.keys(envelope);
  const hasRecovery = keys.includes('recovery');
  if (!sameKeys(envelope, hasRecovery ? [...baseKeys, 'recovery'] : baseKeys)
      || envelope.v !== POLICY_VERSION
      || !Number.isSafeInteger(envelope.generation) || envelope.generation < 1
      || !/^[0-9a-f]{128}$/.test(envelope.signature)) {
    throw new Error('factor policy envelope is malformed');
  }
  if (hasRecovery) parseRecoverySlot(envelope.recovery);
  const state = validateState(
    envelope.root_pub, envelope.factors, envelope.access, envelope.policy,
  );
  const unsigned = {
    v: POLICY_VERSION, generation: envelope.generation, root_pub: envelope.root_pub,
    factors: state.factors, access: state.access, policy: state.policy, wraps: envelope.wraps,
  };
  if (hasRecovery) unsigned.recovery = parseRecoverySlot(envelope.recovery);
  if (!await verifyRootSignature(
    envelope.root_pub, envelope.signature,
    concatBytes(textEncoder.encode(POLICY_SIGNATURE_DOMAIN), textEncoder.encode(canonicalJson(unsigned))),
  )) throw new Error('factor policy signature does not verify');
  return { ...unsigned, signature: envelope.signature };
}

// ── recovery slot (design graph://fd418706-97e) — the byte-identical mirror of
// root_factor_policy.py's recovery slot. The code opens root ALONE, outside the
// policy tree. Recipient derives from the code via the SAME RECOVERY_ARMOR
// purpose the v2 path uses, so one printed code opens both v2 and v3 armors.
const RECOVERY_ARMOR_PURPOSE = 'autonomy/recovery-armor/v1';

function parseRecoverySlot(value) {
  if (!sameKeys(value, ['recipient_public_key', 'recovery_pub', 'sealed'])
      || !/^[0-9a-f]{64}$/.test(value.recipient_public_key)
      || !/^[0-9a-f]{64}$/.test(value.recovery_pub)
      || b64ToBytes(value.sealed).length !== 81) {
    throw new Error('recovery slot is malformed');
  }
  return {
    recipient_public_key: value.recipient_public_key,
    recovery_pub: value.recovery_pub,
    sealed: value.sealed,
  };
}

async function recoveryRecipientPublicKey(recoveryCode) {
  const { kekRecoverySeed } = await deriveRecoveryFactors(recoveryCode);
  const { publicKeyHex } = await deriveEncapsulationKeypair(kekRecoverySeed, RECOVERY_ARMOR_PURPOSE);
  return publicKeyHex;
}

// Enroll a recovery slot into a v3 armor, re-signed by the root. Requires the
// root seed (reach root) + the code's public halves; refuses to replace one.
async function addRecoverySlot(armorText, { rootSeed, recoveryRecipientPub, recoveryPub }) {
  const envelope = await parseFactorPolicyArmor(armorText);
  if (envelope.recovery) throw new Error('this armor already carries a recovery code');
  const sealed = await sealToEncapsulationKey(new Uint8Array(rootSeed), recoveryRecipientPub, RECOVERY_ARMOR_PURPOSE);
  const recovery = {
    recipient_public_key: recoveryRecipientPub,
    recovery_pub: recoveryPub,
    sealed: bytesToB64(sealed),
  };
  const unsigned = {
    v: POLICY_VERSION, generation: envelope.generation, root_pub: envelope.root_pub,
    factors: envelope.factors, access: envelope.access, policy: envelope.policy,
    wraps: envelope.wraps, recovery,
  };
  const signingKey = await importEd25519RootSigningKey(new Uint8Array(rootSeed));
  const signature = bytesToHex(await webCrypto.subtle.sign(
    'Ed25519', signingKey,
    concatBytes(textEncoder.encode(POLICY_SIGNATURE_DOMAIN), textEncoder.encode(canonicalJson(unsigned))),
  ));
  return emitFactorPolicyArmor({ ...unsigned, signature });
}

// Rotate the recovery code: the OLD code AND root (succession closure). The
// old code must open the existing slot; the root seed re-signs. Lost-code
// regeneration (no old code) is the deferred timelock path.
async function replaceRecoverySlot(armorText, { oldCode, rootSeed, recoveryRecipientPub, recoveryPub }) {
  const envelope = await parseFactorPolicyArmor(armorText);
  if (!envelope.recovery) throw new Error('this armor carries no recovery code to replace');
  const opened = await openRootWithRecovery(armorText, oldCode);   // proves possession
  if (opened.rootPub !== envelope.root_pub) throw new Error('the old recovery code did not open this armor');
  opened.seed.fill(0);
  const sealed = await sealToEncapsulationKey(new Uint8Array(rootSeed), recoveryRecipientPub, RECOVERY_ARMOR_PURPOSE);
  const unsigned = {
    v: POLICY_VERSION, generation: envelope.generation, root_pub: envelope.root_pub,
    factors: envelope.factors, access: envelope.access, policy: envelope.policy,
    wraps: envelope.wraps,
    recovery: { recipient_public_key: recoveryRecipientPub, recovery_pub: recoveryPub, sealed: bytesToB64(sealed) },
  };
  const signingKey = await importEd25519RootSigningKey(new Uint8Array(rootSeed));
  const signature = bytesToHex(await webCrypto.subtle.sign(
    'Ed25519', signingKey,
    concatBytes(textEncoder.encode(POLICY_SIGNATURE_DOMAIN), textEncoder.encode(canonicalJson(unsigned))),
  ));
  return emitFactorPolicyArmor({ ...unsigned, signature });
}

// Open the root seed with the printed code alone.
async function openRootWithRecovery(armorText, recoveryCode) {
  const envelope = await parseFactorPolicyArmor(armorText);
  if (!envelope.recovery) throw new Error('this armor carries no recovery code');
  const { kekRecoverySeed } = await deriveRecoveryFactors(recoveryCode);
  const recipient = await deriveEncapsulationKeypair(kekRecoverySeed, RECOVERY_ARMOR_PURPOSE);
  if (recipient.publicKeyHex !== envelope.recovery.recipient_public_key) {
    throw new Error('that recovery code does not match this armor');
  }
  const seed = await openWithEncapsulationPrivateKey(
    b64ToBytes(envelope.recovery.sealed), recipient.privateKeyHex, RECOVERY_ARMOR_PURPOSE,
  );
  const signingKey = await importEd25519RootSigningKey(seed);
  const probe = textEncoder.encode('autonomy.identity.root-factor-open-check.v1\n');
  const signature = new Uint8Array(await webCrypto.subtle.sign('Ed25519', signingKey, probe));
  if (!await verifyRootSignature(envelope.root_pub, bytesToHex(signature), probe)) {
    seed.fill(0);
    throw new Error('opened root seed does not match root_pub');
  }
  return { seed, signingKey, rootPub: envelope.root_pub, envelope };
}

function emitFactorPolicyArmor(envelope) {
  const body = textEncoder.encode(canonicalJson({ v: ARMOR_VERSION, factor_policy: envelope }));
  const encoded = bytesToB64(body).match(/.{1,64}/g) || [];
  return [ARMOR_BEGIN, ...encoded, ARMOR_END].join('\n');
}

async function buildFactorPolicyArmor({ rootSeed, rootPub, generation, factors, access, policy, recovery }) {
  const state = validateState(rootPub, factors, access, policy);
  const digest = await policyDigest(generation, rootPub, state.factors, state.access, state.policy);
  const recipients = Object.fromEntries(
    state.factors.map((factor) => [factor.factor_id, factor.type === 'password'
      ? [factor.recipient_public_key]
      : factor.recipients.map((row) => row.recipient_public_key)]),
  );
  const wraps = await compileNode(state.policy, rootSeed, recipients, digest, 'r');
  const unsigned = {
    v: POLICY_VERSION, generation, root_pub: rootPub,
    factors: state.factors, access: state.access, policy: state.policy, wraps,
  };
  if (recovery) unsigned.recovery = parseRecoverySlot(recovery);
  const signingKey = await importEd25519RootSigningKey(rootSeed);
  const signature = bytesToHex(await webCrypto.subtle.sign(
    'Ed25519', signingKey,
    concatBytes(textEncoder.encode(POLICY_SIGNATURE_DOMAIN), textEncoder.encode(canonicalJson(unsigned))),
  ));
  return emitFactorPolicyArmor({ ...unsigned, signature });
}

async function openNode(node, seeds, factors, digest, path) {
  if (node.op === 'factor') {
    const seed = seeds[node.factor_id];
    if (!seed) throw new Error(`factor ${node.factor_id} was not supplied`);
    const recipient = await deriveEncapsulationKeypair(seed, FACTOR_RECIPIENT_PURPOSE);
    const publicKeys = factors[node.factor_id].type === 'password'
      ? [factors[node.factor_id].recipient_public_key]
      : factors[node.factor_id].recipients.map((row) => row.recipient_public_key);
    if (!publicKeys.includes(recipient.publicKeyHex)) {
      throw new Error(`factor ${node.factor_id} does not match its recipient`);
    }
    const recipientWrap = node.sealed.find(
      (row) => row.recipient_public_key === recipient.publicKeyHex,
    );
    if (!recipientWrap) throw new Error(`factor ${node.factor_id} has no recipient wrap`);
    return openWithEncapsulationPrivateKey(
      b64ToBytes(recipientWrap.sealed), recipient.privateKeyHex, wrapPurpose(digest, path),
    );
  }
  if (node.op === 'or') {
    let last;
    for (let index = 0; index < node.children.length; index += 1) {
      try { return await openNode(node.children[index], seeds, factors, digest, `${path}.${index}`); }
      catch (error) { last = error; }
    }
    throw new Error('no supplied factor satisfied an OR branch', { cause: last });
  }
  const shares = [];
  try {
    for (let index = 0; index < node.children.length; index += 1) {
      shares.push(await openNode(node.children[index], seeds, factors, digest, `${path}.${index}`));
    }
    return xor(shares);
  } finally { shares.forEach((share) => share.fill(0)); }
}

async function openFactorPolicyArmor(armorText, factorSeeds) {
  const envelope = await parseFactorPolicyArmor(armorText); // signature gates use
  const digest = await policyDigest(
    envelope.generation, envelope.root_pub, envelope.factors,
    envelope.access, envelope.policy,
  );
  const factors = Object.fromEntries(envelope.factors.map((factor) => [factor.factor_id, factor]));
  const seed = await openNode(envelope.wraps, factorSeeds, factors, digest, 'r');
  const signingKey = await importEd25519RootSigningKey(seed);
  const probe = textEncoder.encode('autonomy.identity.root-factor-open-check.v1\n');
  const signature = new Uint8Array(await webCrypto.subtle.sign('Ed25519', signingKey, probe));
  if (!await verifyRootSignature(envelope.root_pub, bytesToHex(signature), probe)) {
    seed.fill(0);
    throw new Error('opened root seed does not match root_pub');
  }
  return { seed, signingKey, rootPub: envelope.root_pub, envelope };
}

async function signFactorPolicyTransition({
  signingKey, baseGeneration, operations, candidateArmor,
}) {
  const candidateSha256 = await sha256Hex(textEncoder.encode(candidateArmor));
  const message = concatBytes(textEncoder.encode(TRANSITION_DOMAIN), textEncoder.encode(canonicalJson({
    base_generation: baseGeneration,
    candidate_sha256: candidateSha256,
    operations,
  })));
  return bytesToHex(await webCrypto.subtle.sign('Ed25519', signingKey, message));
}

/* The policy tree with one more factor granted authority: idempotent, and
 * deterministic for every legal shape — a lone leaf or an OR gains the leaf
 * at the top; an AND (multi-factor) extends the class group matching the
 * factor's type. Used by the re-enrollment flows to RESTORE a factor's
 * authority together with its freshly acquired device slot. */
function policyWithFactorGranted(policy, factorId, typeOf) {
  const canonical = canonicalExpression(policy);
  if (policyFactorIds(canonical).includes(factorId)) return canonical;
  const leaf = { op: 'factor', factor_id: factorId };
  if (canonical.op === 'factor') {
    return canonicalExpression({ op: 'or', children: [canonical, leaf] });
  }
  if (canonical.op === 'or') {
    return canonicalExpression({ op: 'or', children: [...canonical.children, leaf] });
  }
  const t = typeOf(factorId);
  let extended = false;
  const children = canonical.children.map((child) => {
    if (extended) return child;
    const ids = policyFactorIds(child);
    if (ids.length && ids.every((id) => typeOf(id) === t)) {
      extended = true;
      return child.op === 'or'
        ? { op: 'or', children: [...child.children, leaf] }
        : { op: 'or', children: [child, leaf] };
    }
    return child;
  });
  return canonicalExpression(extended
    ? { op: 'and', children }
    : { op: 'or', children: [canonical, leaf] });
}

export {
  FACTOR_RECIPIENT_PURPOSE,
  policyWithFactorGranted,
  canonicalExpression,
  policyFactorIds,
  policySatisfied,
  policyDigest,
  createPasswordFactor,
  openPasswordFactor,
  importFactorAccessSigningKey,
  parseFactorPolicyArmor,
  recoveryRecipientPublicKey,
  addRecoverySlot,
  replaceRecoverySlot,
  openRootWithRecovery,
  buildFactorPolicyArmor,
  openFactorPolicyArmor,
  signFactorPolicyTransition,
};
