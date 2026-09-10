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
  hexToBytes,
  importEd25519RootSigningKey,
} from './primitives.js';

export const FLEET_ROSTER_DOMAIN = 'autonomy.fleet.roster-entry.v1\n';
export const FLEET_APPROVAL_DOMAIN = 'autonomy.fleet.enrollment-approval.v1\n';
export const FLEET_INVITE_DOMAIN = 'autonomy.network.fleet-invite.v1\n';
export const FLEET_MACHINE_KEY_SALT = 'autonomy.identity.machine.v1';
// Per-(org, machine) SERVING key salt (auto-e2ufw). MUST stay byte-identical to
// idkit.persona.SERVING_MACHINE_KEY_SALT: the relay verifies the tunnel hello's
// machine co-signature against the key this derives, so a drift here is an
// org that cannot serve, not a test failure.
export const SERVING_MACHINE_KEY_SALT = 'autonomy.identity.serving-machine.v1';
export const FLEET_MEMBER_ASSIGNMENT = 'personal_root_holder';
export const FLEET_COMPLETION_DOMAIN = 'autonomy.fleet.enrollment-completion.v1\n';
export const IDKIT_CERT_DOMAIN = 'autonomy.idkit.cert.v1\n';
export const FLEET_SYNC_SCOPE = 'fleet:sync';
// 30 days, matched to the serve-cert. The credential is persisted server-side
// and re-minted at unlock when it is older than ~10 days (see the fleet runtime
// renew path), so the long TTL is the correct cadence rather than a 12h memory-
// only window that died on every restart. Must match
// FLEET_RUNTIME_DELEGATION_TTL_SECONDS in tools/network/clock.py.
export const FLEET_RUNTIME_TTL_SECONDS = 30 * 24 * 60 * 60;

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
      !== 'invite_id,machine_id,personal_root_pub'
  ) {
    throw new Error(
      'request must carry exactly machine_id, invite_id, personal_root_pub',
    );
  }
  return {
    machine_id: requireHex64(request.machine_id, 'machine_id'),
    personal_root_pub: requireHex64(request.personal_root_pub, 'personal_root_pub'),
    invite_id: requireHex64(request.invite_id, 'invite_id'),
  };
}

async function sha256(bytes) {
  return new Uint8Array(await webCrypto.subtle.digest('SHA-256', bytes));
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

async function deriveServingMachineSeed(seed, genesisId, machineId) {
  // idkit.persona.derive_serving_machine_key, byte for byte:
  //   HKDF-SHA256(root, salt=SERVING_MACHINE_KEY_SALT,
  //               info=genesis_id + "\0" + machine_id)
  // Both ids are fixed-length lowercase hex and the NUL cannot occur in
  // either, so the concatenation is unambiguous. TextEncoder emits U+0000 as
  // a single 0x00 byte, matching Python's ascii encoding of the same string.
  const key = await webCrypto.subtle.importKey('raw', seed, 'HKDF', false, ['deriveBits']);
  return new Uint8Array(await webCrypto.subtle.deriveBits({
    name: 'HKDF',
    hash: 'SHA-256',
    salt: encoder.encode(SERVING_MACHINE_KEY_SALT),
    info: encoder.encode(`${genesisId}\0${machineId}`),
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

async function mintRuntimeCredential({
  machineSigningKey,
  machineId,
  machinePub,
  personalRootPub,
}) {
  const processSeed = webCrypto.getRandomValues(new Uint8Array(32));
  try {
    const processPub = await ed25519PublicHex(processSeed);
    const now = Math.floor(Date.now() / 1000);
    const certPayload = {
      v: 1,
      child_pub: processPub,
      scope: [FLEET_SYNC_SCOPE],
      org: `personal:${personalRootPub}`,
      subject: { kind: 'machine', id: machineId },
      not_before: Math.max(0, now - 30),
      not_after: now + FLEET_RUNTIME_TTL_SECONDS,
    };
    return {
      machine_id: machineId,
      machine_pub: machinePub,
      process_private_seed: bytesToHex(processSeed),
      delegation_cert: {
        ...certPayload,
        sig: await signHex(
          machineSigningKey,
          domainBytes(IDKIT_CERT_DOMAIN, canonicalJson(certPayload)),
        ),
      },
    };
  } finally {
    processSeed.fill(0);
  }
}

/** Mint a process-only Fleet sync credential during any later root unlock.
 *
 * The durable machine key is re-derived in the browser and signs a short-lived
 * delegation. Python receives only the fresh process seed plus public proof.
 */
export async function mintFleetRuntimeCredential({
  personalRootSeed,
  rootPub,
  machineId,
  machinePub,
  orgUuid = null,
  servingOrgs = [],
} = {}) {
  if (!(personalRootSeed instanceof Uint8Array) || personalRootSeed.length !== 32) {
    throw new Error('personalRootSeed must be a 32-byte Uint8Array');
  }
  const seed = personalRootSeed;
  let machineSeed = null;
  let machineSigningKey = null;
  try {
    const anchor = requireHex64(rootPub, 'rootPub');
    const mid = requireHex64(machineId, 'machineId');
    const authorizedPub = requireHex64(machinePub, 'machinePub');
    if (await ed25519PublicHex(seed) !== anchor) {
      throw new Error('opened personal root does not match this Fleet');
    }
    machineSeed = await deriveMachineSeed(seed, mid);
    if (await ed25519PublicHex(machineSeed) !== authorizedPub) {
      throw new Error('opened personal root does not derive this roster machine key');
    }
    machineSigningKey = await importEd25519RootSigningKey(machineSeed);
    const credential = await mintRuntimeCredential({
      machineSigningKey,
      machineId: mid,
      machinePub: authorizedPub,
      personalRootPub: anchor,
    });
    // When the personal org is registered, ALSO deliver the reachability
    // material: the durable machine key (which signs node:announce/node:lookup,
    // because the registry keys hints by the signer and discovery reads by
    // roster machine_pub) plus a SEPARATE root-direct cert scoped to node
    // discovery under the registered org_uuid. Omitted when unregistered, so the
    // sync-only credential is byte-identical.
    if (orgUuid) {
      credential.machine_private_seed = bytesToHex(machineSeed);
      credential.reachability_cert = await mintReachabilityCert(
        seed, authorizedPub, mid, orgUuid);
    }
    // One SERVING key per organization this machine is provisioned to serve
    // (auto-e2ufw). Keyed by the registry org_uuid, which is what names the
    // connector's warm cache and its --org. The root is only ever open here,
    // in this browser, at unlock: nothing server-side can derive these, which
    // is why an org connector that has never been handed one cannot start at
    // all after a restart clears ramfs.
    if (Array.isArray(servingOrgs) && servingOrgs.length) {
      const seeds = {};
      for (const target of servingOrgs) {
        const genesisId = requireHex64(target?.genesis_id, 'genesis_id');
        const targetUuid = target?.org_uuid;
        if (typeof targetUuid !== 'string' || !targetUuid) {
          throw new Error('servingOrgs entries need an org_uuid');
        }
        const servingSeed = await deriveServingMachineSeed(seed, genesisId, mid);
        try {
          seeds[targetUuid] = bytesToHex(servingSeed);
        } finally {
          servingSeed.fill(0);
        }
      }
      credential.serving_machine_private_seeds = seeds;
    }
    return credential;
  } finally {
    seed.fill(0);
    if (machineSeed) machineSeed.fill(0);
    machineSigningKey = null;
  }
}

/** Mint the SEPARATE reachability cert: root-direct (root -> machine_pub, scope
 *  node:announce+node:lookup, org=the registered org_uuid), byte-parity with
 *  idkit.issue_cert. Signed by the root; delivered as a dict for
 *  DelegationCert.from_dict. */
export async function mintReachabilityCert(
  personalRootSeed, machinePub, machineId, orgUuid,
  { now = Math.floor(Date.now() / 1000), ttlS = 7 * 24 * 3600 } = {},
) {
  requireHex64(machinePub, 'machinePub');
  requireHex64(machineId, 'machineId');
  if (typeof orgUuid !== 'string' || !orgUuid) {
    throw new Error('orgUuid must be the personal org uuid string');
  }
  const payload = {
    v: 1,
    child_pub: machinePub,
    scope: ['node:announce', 'node:lookup'],   // sorted, as idkit requires
    org: orgUuid,
    subject: { kind: 'agent', id: machineId },
    not_before: now - 60,
    not_after: now + ttlS,
  };
  const rootKey = await importEd25519RootSigningKey(personalRootSeed);
  return {
    ...payload,
    sig: await signHex(
      rootKey, domainBytes(IDKIT_CERT_DOMAIN, canonicalJson(payload))),
  };
}

async function verifyHex(publicHex, signatureHex, input) {
  const key = await webCrypto.subtle.importKey(
    'raw', hexToBytes(requireHex64(publicHex, 'public key')),
    { name: 'Ed25519' }, false, ['verify'],
  );
  if (
    typeof signatureHex !== 'string'
    || !/^[0-9a-f]{128}$/.test(signatureHex)
    || !await webCrypto.subtle.verify(
      'Ed25519', key, hexToBytes(signatureHex), input,
    )
  ) {
    throw new Error('fleet enrollment signature did not verify');
  }
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
  localBootstrapMachineId = null,
  issuedAt = 0,
  seq = 0,
} = {}) {
  if (!(personalRootSeed instanceof Uint8Array)) {
    throw new Error('personalRootSeed must be a 32-byte Uint8Array');
  }
  const seed = personalRootSeed;
  let machineSeed = null;
  let localMachineSeed = null;
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

    const machineId = frozen.machine_id;
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

    // A legacy root-holder Dashboard has no Fleet identity yet.  During the
    // first remote approval the same root ceremony enrolls that Dashboard as
    // member one and selects it as the temporary singular tunnel server.  A
    // random machine id is correct for this local bootstrap: unlike a joiner,
    // there is no transient request/channel record that must derive the id.
    let localRosterEntry = null;
    let localRuntime = null;
    if (localBootstrapMachineId !== null) {
      const localMachineId = requireHex64(
        localBootstrapMachineId, 'localBootstrapMachineId',
      );
      localMachineSeed = await deriveMachineSeed(seed, localMachineId);
      const localMachinePub = await ed25519PublicHex(localMachineSeed);
      const localBinding = {
        v: 1,
        personal_root_pub: anchor,
        machine_id: localMachineId,
        machine_pub: localMachinePub,
        assignment: FLEET_MEMBER_ASSIGNMENT,
        kind: 'enroll',
        seq: sequence,
        issued_at: timestamp,
        supersedes: null,
      };
      localRosterEntry = {
        ...localBinding,
        signature: await signHex(
          rootSigningKey,
          domainBytes(FLEET_ROSTER_DOMAIN, canonicalJson(localBinding)),
        ),
      };
      const localSigningKey = await importEd25519RootSigningKey(localMachineSeed);
      localRuntime = await mintRuntimeCredential({
        machineSigningKey: localSigningKey,
        machineId: localMachineId,
        machinePub: localMachinePub,
        personalRootPub: anchor,
      });
    }

    const approvalBinding = {
      v: 1,
      machine_id: frozen.machine_id,
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
    return { rosterEntry, approval, localRosterEntry, localRuntime };
  } finally {
    seed.fill(0);
    if (machineSeed) machineSeed.fill(0);
    if (localMachineSeed) localMachineSeed.fill(0);
    rootSigningKey = null;
  }
}

/** Finish a delivered enrollment entirely in the joining browser.
 *
 * The root seed derives and checks the authorized machine id/key, then the
 * machine key signs a bounded completion proof. Only that public proof is
 * returned to the server; the root and derived machine seed are both zeroed.
 */
export async function completeFleetEnrollment({
  personalRootSeed,
  requestId,
  request,
  channelBinding,
  approval,
  rosterEntry,
} = {}) {
  if (!(personalRootSeed instanceof Uint8Array) || personalRootSeed.length !== 32) {
    throw new Error('personalRootSeed must be a 32-byte Uint8Array');
  }
  const seed = personalRootSeed;
  let machineSeed = null;
  let machineSigningKey = null;
  try {
    const frozen = requireRequest(request);
    const rid = requireHex64(requestId, 'requestId');
    const channel = requireHex64(channelBinding, 'channelBinding');
    if (await ed25519PublicHex(seed) !== frozen.personal_root_pub) {
      throw new Error('opened personal root does not match this Fleet request');
    }
    const machineId = frozen.machine_id;
    machineSeed = await deriveMachineSeed(seed, machineId);
    const machinePub = await ed25519PublicHex(machineSeed);

    const rosterBinding = {
      v: rosterEntry?.v,
      personal_root_pub: rosterEntry?.personal_root_pub,
      machine_id: rosterEntry?.machine_id,
      machine_pub: rosterEntry?.machine_pub,
      assignment: rosterEntry?.assignment,
      kind: rosterEntry?.kind,
      seq: rosterEntry?.seq,
      issued_at: rosterEntry?.issued_at,
      supersedes: rosterEntry?.supersedes,
    };
    if (
      rosterBinding.v !== 1
      || rosterBinding.personal_root_pub !== frozen.personal_root_pub
      || rosterBinding.machine_id !== machineId
      || rosterBinding.machine_pub !== machinePub
      || rosterBinding.assignment !== FLEET_MEMBER_ASSIGNMENT
      || rosterBinding.kind !== 'enroll'
    ) {
      throw new Error('delivered roster entry does not authorize this machine');
    }
    const rosterInput = domainBytes(
      FLEET_ROSTER_DOMAIN, canonicalJson(rosterBinding),
    );
    const rosterEntryId = bytesToHex(await sha256(rosterInput));
    await verifyHex(
      frozen.personal_root_pub, rosterEntry?.signature, rosterInput,
    );

    const approvalBinding = {
      v: approval?.v,
      machine_id: approval?.machine_id,
      personal_root_pub: approval?.personal_root_pub,
      invite_id: approval?.invite_id,
      channel_binding: approval?.channel_binding,
      roster_entry_id: approval?.roster_entry_id,
    };
    if (
      approvalBinding.v !== 1
      || approvalBinding.machine_id !== frozen.machine_id
      || approvalBinding.personal_root_pub !== frozen.personal_root_pub
      || approvalBinding.invite_id !== frozen.invite_id
      || approvalBinding.channel_binding !== channel
      || approvalBinding.roster_entry_id !== rosterEntryId
    ) {
      throw new Error('delivered approval is not bound to this request and channel');
    }
    await verifyHex(
      frozen.personal_root_pub,
      approval?.signature,
      domainBytes(FLEET_APPROVAL_DOMAIN, canonicalJson(approvalBinding)),
    );

    machineSigningKey = await importEd25519RootSigningKey(machineSeed);
    const proof = await signHex(
      machineSigningKey,
      domainBytes(FLEET_COMPLETION_DOMAIN, canonicalJson({
        v: 1,
        request_id: rid,
        roster_entry_id: rosterEntryId,
      })),
    );
    const runtime = await mintRuntimeCredential({
      machineSigningKey,
      machineId,
      machinePub,
      personalRootPub: frozen.personal_root_pub,
    });
    return { request_id: rid, machine_id: machineId, proof, runtime };
  } finally {
    seed.fill(0);
    if (machineSeed) machineSeed.fill(0);
    machineSigningKey = null;
  }
}
