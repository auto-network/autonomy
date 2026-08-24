/* Personal-root vault anchor, byte-compatible with tools/vault/root_anchor.py.
 *
 * Passwords and passkeys stop at opening the personal root armor. This module
 * turns that freshly opened root into the stable recipient seed used by a
 * root-reachable policy class. Neither the root seed nor the anchor seed is
 * persisted or sent except for the anchor seed's one approval-bound opener.
 */
import {
  bytesToHex,
  canonicalJson,
  deriveEncapsulationKeypair,
  hexToBytes,
  openSealedArmor,
  sealToEncapsulationKey,
} from './primitives.js';

export const PERSONAL_ROOT_RECIPIENT = 'personal-root-anchor';
export const POLICY_RECIPIENT_PURPOSE = 'autonomy/vault-policy-recipient/v1';
export const ROOT_ANCHOR_WRAP_PURPOSE = 'autonomy/vault-root-anchor-wrap/v1';
export const ROOT_ANCHOR_ENROLL_DOMAIN = 'autonomy/vault-root-anchor-enroll/v1\n';

const encoder = new TextEncoder();

function concat(left, right) {
  const out = new Uint8Array(left.length + right.length);
  out.set(left, 0); out.set(right, left.length);
  return out;
}

async function sha256Hex(bytes) {
  return bytesToHex(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)));
}

export async function rootAnchorWrapPurpose(anchorId, rootPub) {
  const digest = await sha256Hex(encoder.encode(canonicalJson({
    anchor_id: anchorId,
    root_pub: rootPub,
    v: 1,
  })));
  return `${ROOT_ANCHOR_WRAP_PURPOSE}|${digest}`;
}

function recipientPurpose(kind) {
  return `${POLICY_RECIPIENT_PURPOSE}|${kind}`;
}

export async function createRootAnchorEnvelope(
  openedRoot,
  {
    anchorId = 'personal-root-default',
    displayName = 'Personal root vault access',
    createdAt = new Date().toISOString(),
    anchorSeed = null,
  } = {},
) {
  if (!openedRoot?.seed || !openedRoot?.signingKey || !openedRoot?.rootPub) {
    throw new Error('a freshly opened personal root is required');
  }
  const seed = anchorSeed
    ? new Uint8Array(anchorSeed)
    : crypto.getRandomValues(new Uint8Array(32));
  if (seed.length !== 32) {
    seed.fill(0);
    throw new Error('root anchor seed must be exactly 32 bytes');
  }
  try {
    const purpose = await rootAnchorWrapPurpose(anchorId, openedRoot.rootPub);
    const rootRecipient = await deriveEncapsulationKeypair(openedRoot.seed, purpose);
    const anchorRecipient = await deriveEncapsulationKeypair(
      seed, recipientPurpose(PERSONAL_ROOT_RECIPIENT),
    );
    const sealed = await sealToEncapsulationKey(
      seed, rootRecipient.publicKeyHex, purpose,
    );
    const unsigned = {
      v: 1,
      anchor_id: anchorId,
      display_name: displayName,
      root_pub: openedRoot.rootPub,
      public_key: anchorRecipient.publicKeyHex,
      sealed_seed: bytesToHex(sealed),
      created_at: createdAt,
    };
    const signature = bytesToHex(new Uint8Array(await crypto.subtle.sign(
      'Ed25519',
      openedRoot.signingKey,
      concat(
        encoder.encode(ROOT_ANCHOR_ENROLL_DOMAIN),
        encoder.encode(canonicalJson(unsigned)),
      ),
    )));
    return { ...unsigned, signature };
  } finally {
    seed.fill(0);
  }
}

export async function openRootAnchorEnvelope(anchor, rootSeed) {
  const fields = [
    'anchor_id', 'created_at', 'display_name', 'public_key', 'root_pub',
    'sealed_seed', 'signature', 'v',
  ];
  if (!anchor || typeof anchor !== 'object'
      || Object.keys(anchor).sort().join('|') !== fields.sort().join('|')
      || anchor.v !== 1 || typeof anchor.anchor_id !== 'string'
      || typeof anchor.display_name !== 'string' || typeof anchor.created_at !== 'string'
      || typeof anchor.root_pub !== 'string' || !/^[0-9a-f]{64}$/.test(anchor.root_pub)
      || typeof anchor.public_key !== 'string' || !/^[0-9a-f]{64}$/.test(anchor.public_key)
      || typeof anchor.sealed_seed !== 'string' || !/^[0-9a-f]+$/.test(anchor.sealed_seed)
      || typeof anchor.signature !== 'string' || !/^[0-9a-f]{128}$/.test(anchor.signature)) {
    throw new Error('root anchor envelope is malformed');
  }
  const unsigned = {
    v: anchor.v,
    anchor_id: anchor.anchor_id,
    display_name: anchor.display_name,
    root_pub: anchor.root_pub,
    public_key: anchor.public_key,
    sealed_seed: anchor.sealed_seed,
    created_at: anchor.created_at,
  };
  const verificationKey = await crypto.subtle.importKey(
    'raw', hexToBytes(anchor.root_pub), { name: 'Ed25519' }, false, ['verify'],
  );
  const signatureValid = await crypto.subtle.verify(
    'Ed25519',
    verificationKey,
    hexToBytes(anchor.signature),
    concat(
      encoder.encode(ROOT_ANCHOR_ENROLL_DOMAIN),
      encoder.encode(canonicalJson(unsigned)),
    ),
  );
  if (!signatureValid) {
    throw new Error('root anchor is not signed by this personal root');
  }
  const purpose = await rootAnchorWrapPurpose(anchor.anchor_id, anchor.root_pub);
  const seed = await openSealedArmor({
    sealed_root_key: anchor.sealed_seed,
    seal_purpose: purpose,
  }, rootSeed);
  try {
    if (seed.length !== 32) throw new Error('root anchor opened malformed material');
    const recipient = await deriveEncapsulationKeypair(
      seed, recipientPurpose(PERSONAL_ROOT_RECIPIENT),
    );
    if (recipient.publicKeyHex !== anchor.public_key) {
      throw new Error('root anchor seed does not match its published recipient');
    }
    return new Uint8Array(seed);
  } finally {
    seed.fill(0);
  }
}
