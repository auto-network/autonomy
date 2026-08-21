/* Browser-only fleet enrollment signing ceremony.
 *
 * The personal root seed enters this function from a locally opened armor,
 * signs two public records, and is zeroed on every exit. No server route ever
 * receives it. The returned records mirror tools.network.fleet_roster and
 * tools.network.fleet_enroll byte-for-byte.
 */

import {
  bytesToHex,
  canonicalJson,
  domainBytes,
  importEd25519RootSigningKey,
} from './primitives.js';

export const FLEET_ROSTER_DOMAIN = 'autonomy.fleet.roster-entry.v1\n';
export const FLEET_APPROVAL_DOMAIN = 'autonomy.fleet.enrollment-approval.v1\n';
export const FLEET_INVITE_DOMAIN = 'autonomy.network.fleet-invite.v1\n';
export const FLEET_MACHINE_ID_DOMAIN = 'autonomy.network.fleet-machine-id.v1\n';
export const FLEET_MACHINE_KEY_SALT = 'autonomy.identity.machine.v1';
export const FLEET_MEMBER_ASSIGNMENT = 'personal_root_holder';

const PKCS8_ED25519_PREFIX = new Uint8Array([
  0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06,
  0x03, 0x2b, 0x65, 0x70, 0x04, 0x22, 0x04, 0x20,
]);
const HEX64 = /^[0-9a-f]{64}$/;
const encoder = new TextEncoder();

let webCrypto = globalThis.crypto;
if (!webCrypto && typeof process !== 'undefined' && process.versions?.node) {
  ({ webcrypto: webCrypto } = await import('node:crypto'));
}
if (!webCrypto?.subtle) throw new Error('fleet enrollment requires WebCrypto');

function requireHex64(value, name) {
  if (typeof value !== 'string' || !HEX64.test(value)) {
    throw new Error(`${name} must be 64 lowercase hex chars`);
  }
  return value;
}

function requireSafeInt(value, name) {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new Error(`${name} must be a non-negative safe integer`);
  }
  return value;
}

function requireRequest(request) {
  if (
    !request
    || typeof request !== 'object'
    || Array.isArray(request)
    || Object.keys(request).sort().join(',')
      !== 'enrollment_nonce,invite_id,personal_root_pub'
  ) {
    throw new Error('request must carry exactly enrollment_nonce, invite_id, personal_root_pub');
  }
  return {
    enrollment_nonce: requireHex64(request.enrollment_nonce, 'enrollment_nonce'),
    personal_root_pub: requireHex64(request.personal_root_pub, 'personal_root_pub'),
    invite_id: requireHex64(request.invite_id, 'invite_id'),
  };
}

async function sha256(bytes) {
  return new Uint8Array(await webCrypto.subtle.digest('SHA-256', bytes));
}

async function deriveMachineId(seed, request) {
  const key = await webCrypto.subtle.importKey(
    'raw', seed, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'],
  );
  const material = canonicalJson({
    v: 2,
    enrollment_nonce: request.enrollment_nonce,
    personal_root_pub: request.personal_root_pub,
    invite_id: request.invite_id,
  });
  const signed = await webCrypto.subtle.sign(
    'HMAC', key, domainBytes(FLEET_MACHINE_ID_DOMAIN, material),
  );
  return bytesToHex(new Uint8Array(signed));
}

async function deriveMachineSeed(seed, machineId) {
  const key = await webCrypto.subtle.importKey('raw', seed, 'HKDF', false, ['deriveBits']);
  return new Uint8Array(await webCrypto.subtle.deriveBits({
    name: 'HKDF',
    hash: 'SHA-256',
    salt: encoder.encode(FLEET_MACHINE_KEY_SALT),
    info: encoder.encode(machineId),
  }, key, 256));
}

async function ed25519PublicHex(seed) {
  const pkcs8 = new Uint8Array(PKCS8_ED25519_PREFIX.length + seed.length);
  pkcs8.set(PKCS8_ED25519_PREFIX, 0);
  pkcs8.set(seed, PKCS8_ED25519_PREFIX.length);
  try {
    const key = await webCrypto.subtle.importKey(
      'pkcs8', pkcs8, { name: 'Ed25519' }, true, ['sign'],
    );
    const jwk = await webCrypto.subtle.exportKey('jwk', key);
    const b64 = jwk.x.replace(/-/g, '+').replace(/_/g, '/');
    const padded = b64 + '='.repeat((4 - (b64.length % 4)) % 4);
    let raw;
    if (typeof Buffer !== 'undefined') {
      raw = new Uint8Array(Buffer.from(padded, 'base64'));
    } else {
      const binary = atob(padded);
      raw = Uint8Array.from(binary, (ch) => ch.charCodeAt(0));
    }
    return bytesToHex(raw);
  } finally {
    pkcs8.fill(0);
  }
}

async function signHex(signingKey, input) {
  return bytesToHex(new Uint8Array(
    await webCrypto.subtle.sign('Ed25519', signingKey, input),
  ));
}

function requireGrantRendezvous(value) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error('rendezvous must be an https fleet grant URL');
  }
  if (
    parsed.protocol !== 'https:'
    || parsed.username
    || parsed.password
    || parsed.search
    || parsed.hash
    || !/^\/l\/[0-9a-f]{32}$/.test(parsed.pathname)
  ) {
    throw new Error('rendezvous must be an exact https fleet grant URL');
  }
  return parsed.href;
}

function base64Url(bytes) {
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** Mint the public fleet invitation after link publication returns its URL.
 *
 * The grant exists first, but accepts no request until the signed result is
 * registered with the origin Dashboard. The personal-root seed is zeroed on
 * every path and only signed public bytes leave this function.
 */
export async function mintFleetInvite({
  personalRootSeed,
  rootPub,
  rendezvous,
  inviteId,
  expiresAt = 0,
} = {}) {
  if (!(personalRootSeed instanceof Uint8Array)) {
    throw new Error('personalRootSeed must be a 32-byte Uint8Array');
  }
  const seed = personalRootSeed;
  let rootSigningKey = null;
  try {
    if (seed.length !== 32) {
      throw new Error('personalRootSeed must be a 32-byte Uint8Array');
    }
    const anchor = requireHex64(rootPub, 'rootPub');
    if (await ed25519PublicHex(seed) !== anchor) {
      throw new Error('opened personal root does not match rootPub');
    }
    const target = requireGrantRendezvous(rendezvous);
    const correlation = inviteId === undefined
      ? bytesToHex(webCrypto.getRandomValues(new Uint8Array(32)))
      : requireHex64(inviteId, 'inviteId');
    const expiry = requireSafeInt(expiresAt, 'expiresAt');
    const binding = {
      v: 1,
      kind: 'fleet-machine',
      personal_root_pub: anchor,
      rendezvous: target,
      invite_id: correlation,
      expires_at: expiry,
    };
    rootSigningKey = await importEd25519RootSigningKey(seed);
    const signature = await signHex(
      rootSigningKey,
      domainBytes(FLEET_INVITE_DOMAIN, canonicalJson(binding)),
    );
    const wire = { ...binding, sig: signature };
    const raw = encoder.encode(canonicalJson(wire));
    const checksum = bytesToHex(await sha256(raw)).slice(0, 8);
    return {
      invite: {
        personal_root_pub: anchor,
        rendezvous: target,
        invite_id: correlation,
        expires_at: expiry,
        signature,
      },
      code: `${base64Url(raw)}.${checksum}`,
    };
  } finally {
    seed.fill(0);
    rootSigningKey = null;
  }
}

export async function mintFleetEnrollmentEvidence({
  personalRootSeed,
  rootPub,
  request,
  channelBinding,
  issuedAt = 0,
  seq = 0,
} = {}) {
  if (!(personalRootSeed instanceof Uint8Array)) {
    throw new Error('personalRootSeed must be a 32-byte Uint8Array');
  }
  const seed = personalRootSeed;
  let machineSeed = null;
  let rootSigningKey = null;
  try {
    if (seed.length !== 32) {
      throw new Error('personalRootSeed must be a 32-byte Uint8Array');
    }
    const frozen = requireRequest(request);
    const anchor = requireHex64(rootPub, 'rootPub');
    if (frozen.personal_root_pub !== anchor) {
      throw new Error('request does not match the opened personal root');
    }
    if (await ed25519PublicHex(seed) !== anchor) {
      throw new Error('opened personal root does not match rootPub');
    }
    const channel = requireHex64(channelBinding, 'channelBinding');
    const timestamp = requireSafeInt(issuedAt, 'issuedAt');
    const sequence = requireSafeInt(seq, 'seq');

    const machineId = await deriveMachineId(seed, frozen);
    machineSeed = await deriveMachineSeed(seed, machineId);
    const machinePub = await ed25519PublicHex(machineSeed);
    rootSigningKey = await importEd25519RootSigningKey(seed);

    const rosterBinding = {
      v: 1,
      personal_root_pub: anchor,
      machine_id: machineId,
      machine_pub: machinePub,
      assignment: FLEET_MEMBER_ASSIGNMENT,
      kind: 'enroll',
      seq: sequence,
      issued_at: timestamp,
      supersedes: null,
    };
    const rosterInput = domainBytes(
      FLEET_ROSTER_DOMAIN, canonicalJson(rosterBinding),
    );
    const entryId = bytesToHex(await sha256(rosterInput));
    const rosterEntry = {
      ...rosterBinding,
      signature: await signHex(rootSigningKey, rosterInput),
    };

    const approvalBinding = {
      v: 1,
      enrollment_nonce: frozen.enrollment_nonce,
      personal_root_pub: anchor,
      invite_id: frozen.invite_id,
      channel_binding: channel,
      roster_entry_id: entryId,
    };
    const approval = {
      ...approvalBinding,
      signature: await signHex(
        rootSigningKey,
        domainBytes(FLEET_APPROVAL_DOMAIN, canonicalJson(approvalBinding)),
      ),
    };
    return { rosterEntry, approval };
  } finally {
    seed.fill(0);
    if (machineSeed) machineSeed.fill(0);
    rootSigningKey = null;
  }
}
