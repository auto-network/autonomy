/* Browser-only fleet KICK signing ceremony.
 *
 * Revoking a machine is a personal-root signature, exactly like enrollment: the
 * root seed enters this function from a locally opened armor (openRoot()), signs
 * ONE public tombstone RosterEntry, and is zeroed on every exit. No server route
 * ever receives the seed. The minted entry mirrors tools.network.fleet_roster
 * kick() byte-for-byte (same domain, canonicalization, and binding fields), so
 * fleet_roster.verify accepts it and resolve() drops the machine — a KICK is
 * absorbing against any concurrent or later renewal.
 *
 * A kick names the target machine as the fleet already knows it (the projection
 * row: entryId, machineId, machinePublicKey, seq); it does NOT re-derive the
 * target's machine key, because a machine may need removing precisely when its
 * key is compromised or legacy. Authority comes from the root signature; the
 * server independently re-checks it against the stored anchor and refuses a
 * local/serving/inactive/stale target.
 */

import {
  bytesToHex,
  canonicalJson,
  domainBytes,
  importEd25519RootSigningKey,
} from './primitives.js';
import { FLEET_ROSTER_DOMAIN, FLEET_MEMBER_ASSIGNMENT } from './fleet-enrollment.js';

const HEX64 = /^[0-9a-f]{64}$/;

let webCrypto = globalThis.crypto;
if (!webCrypto && typeof process !== 'undefined' && process.versions?.node) {
  ({ webcrypto: webCrypto } = await import('node:crypto'));
}
if (!webCrypto?.subtle) throw new Error('fleet kick requires WebCrypto');

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

async function sha256Hex(bytes) {
  return bytesToHex(new Uint8Array(await webCrypto.subtle.digest('SHA-256', bytes)));
}

async function signHex(signingKey, input) {
  return bytesToHex(new Uint8Array(
    await webCrypto.subtle.sign('Ed25519', signingKey, input),
  ));
}

/**
 * Mint a personal-root-signed KICK tombstone for one fleet machine.
 *
 * @param {{seed:Uint8Array, signingKey?:CryptoKey, rootPub:string}} openedRoot
 *   The result of openRoot(): the raw personal-root seed, an optional ready
 *   signing key, and the fleet anchor public key. The seed is zeroed here (the
 *   openRoot caller contract), so the caller must not reuse it after this call.
 * @param {{machineId:string, machinePublicKey:string, seq:number}} target
 *   The projection row of the machine to remove. `seq` is that machine's
 *   current roster-entry sequence; the kick is minted at `seq + 1` so it beats
 *   the entry it revokes (the route also enforces this).
 * @param {{issuedAt?:number}} [options] `issuedAt` (unix ms, informational —
 *   not a merge input) defaults to now; pass it for deterministic vectors.
 * @returns {Promise<{roster_entry:object, entry_id:string}>}
 */
export async function signFleetKick(openedRoot, target, { issuedAt } = {}) {
  if (!openedRoot || !(openedRoot.seed instanceof Uint8Array)) {
    throw new Error('openedRoot must carry the opened personal-root seed');
  }
  const seed = openedRoot.seed;
  let signingKey = openedRoot.signingKey || null;
  try {
    const anchor = requireHex64(openedRoot.rootPub, 'rootPub');
    const machineId = requireHex64(target?.machineId, 'machineId');
    const machinePub = requireHex64(target?.machinePublicKey, 'machinePublicKey');
    const currentSeq = requireSafeInt(target?.seq, 'target seq');
    const stamp = issuedAt === undefined
      ? Date.now()
      : requireSafeInt(issuedAt, 'issuedAt');

    if (seed.length !== 32) {
      throw new Error('personal-root seed must be 32 bytes');
    }
    if (!signingKey) signingKey = await importEd25519RootSigningKey(seed);

    const binding = {
      v: 1,
      personal_root_pub: anchor,
      machine_id: machineId,
      machine_pub: machinePub,
      assignment: FLEET_MEMBER_ASSIGNMENT,
      kind: 'kick',
      // Per-machine sequence: beat the entry this tombstone revokes.
      seq: currentSeq + 1,
      issued_at: stamp,
      // A kick cites no supersedes; only a re-enrolment does (fleet_roster).
      supersedes: null,
    };
    const rosterInput = domainBytes(FLEET_ROSTER_DOMAIN, canonicalJson(binding));
    const signature = await signHex(signingKey, rosterInput);
    return {
      roster_entry: { ...binding, signature },
      entry_id: await sha256Hex(rosterInput),
    };
  } finally {
    seed.fill(0);
    signingKey = null;
  }
}

export default signFleetKick;
