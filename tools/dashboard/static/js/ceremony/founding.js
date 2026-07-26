/*
 * Client-side organization-ledger founding.
 *
 * The organization root signs the constitutional events in the client. The
 * server receives canonical, pre-signed wire events and contributes no
 * authority. Default founding omits a KEM credential; callers that opt into
 * one receive the private X25519 key for their external device store.
 */

import {
  bytesToHex,
  canonicalJson,
  domainBytes,
} from './primitives.js';
import {
  buildEvent,
  derivePersona,
  eventId,
  signEvent,
} from './ledger-event.js';

const CREDENTIAL_DOMAIN = 'autonomy.storage.persona-kem-credential.v1\n';
const KEM_DERIVE_INFO_PREFIX = 'autonomy.idkit.encap-key.v1\n';
const KEM_PURPOSE_PREFIX = 'autonomy/persona-kem/v1/';
const X25519_PKCS8_PREFIX = [
  0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06,
  0x03, 0x2b, 0x65, 0x6e, 0x04, 0x22, 0x04, 0x20,
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
  throw new Error('organization founding requires WebCrypto');
}

const textEncoder = new TextEncoder();

function requireLowerHex(value, length, name) {
  const pattern = new RegExp(`^[0-9a-f]{${length}}$`);
  if (typeof value !== 'string' || !pattern.test(value)) {
    throw new Error(`${name} must be ${length} lowercase hex chars`);
  }
  return value;
}

function normalizeHlc(hlc) {
  if (
    !Array.isArray(hlc)
    || hlc.length !== 2
    || !hlc.every(
      (value) => Number.isSafeInteger(value) && value >= 0,
    )
  ) {
    throw new Error('createdHlc must be two non-negative safe integers');
  }
  return [hlc[0], hlc[1]];
}

function normalizeHeads(heads) {
  if (!Array.isArray(heads)) {
    throw new Error('authorityHeads must be an array');
  }
  return Array.from(new Set(heads.map(
    (head) => requireLowerHex(head, 64, 'authority head'),
  ))).sort();
}

function base64UrlToBytes(value) {
  let base64 = value.replace(/-/g, '+').replace(/_/g, '/');
  while (base64.length % 4) base64 += '=';
  const binary = atob(base64);
  const output = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    output[index] = binary.charCodeAt(index);
  }
  return output;
}

async function sha256Hex(value) {
  return bytesToHex(await webCrypto.subtle.digest(
    'SHA-256',
    textEncoder.encode(value),
  ));
}

async function deriveX25519EncapsulationKey(kemSeed, genesisId) {
  const seed = new Uint8Array(kemSeed);
  if (seed.length < 32) {
    throw new Error('kemSeed must contain at least 32 bytes');
  }
  const purpose = KEM_PURPOSE_PREFIX + genesisId;
  let material;
  try {
    material = await webCrypto.subtle.importKey(
      'raw',
      seed,
      'HKDF',
      false,
      ['deriveBits'],
    );
  } finally {
    seed.fill(0);
  }
  const privateBytes = new Uint8Array(await webCrypto.subtle.deriveBits(
    {
      name: 'HKDF',
      hash: 'SHA-256',
      // cryptography HKDF(salt=None) uses an all-zero digest-sized salt.
      salt: new Uint8Array(32),
      info: textEncoder.encode(KEM_DERIVE_INFO_PREFIX + purpose),
    },
    material,
    256,
  ));
  const pkcs8 = new Uint8Array(
    X25519_PKCS8_PREFIX.length + privateBytes.length,
  );
  pkcs8.set(X25519_PKCS8_PREFIX, 0);
  pkcs8.set(privateBytes, X25519_PKCS8_PREFIX.length);
  let jwk;
  try {
    const privateKey = await webCrypto.subtle.importKey(
      'pkcs8',
      pkcs8,
      { name: 'X25519' },
      true,
      ['deriveBits'],
    );
    jwk = await webCrypto.subtle.exportKey('jwk', privateKey);
    return {
      privateHex: bytesToHex(privateBytes),
      publicHex: bytesToHex(base64UrlToBytes(jwk.x)),
    };
  } finally {
    privateBytes.fill(0);
    pkcs8.fill(0);
    if (jwk) {
      delete jwk.d;
      delete jwk.x;
    }
  }
}

async function buildPersonaKemCredential({
  persona,
  genesisId,
  kemSeed,
  authorityHeads,
  createdHlc,
}) {
  if (
    !persona
    || typeof persona.publicHex !== 'string'
    || !persona.signingKey
  ) {
    throw new Error('persona must carry publicHex and signingKey');
  }
  requireLowerHex(genesisId, 64, 'genesisId');
  requireLowerHex(persona.publicHex, 64, 'persona public key');
  const encapsulation = await deriveX25519EncapsulationKey(
    kemSeed,
    genesisId,
  );
  const binding = {
    version: 1,
    suite_id: 1,
    genesis_id: genesisId,
    persona: persona.publicHex,
    kem_public_key: encapsulation.publicHex,
    authority_heads: normalizeHeads(authorityHeads),
    created_hlc: normalizeHlc(createdHlc),
  };
  const kemKeyId = await sha256Hex(canonicalJson(binding));
  const signed = {
    ...binding,
    kem_key_id: kemKeyId,
  };
  const signature = await webCrypto.subtle.sign(
    'Ed25519',
    persona.signingKey,
    domainBytes(CREDENTIAL_DOMAIN, canonicalJson(signed)),
  );
  return {
    credential: {
      ...signed,
      signature: bytesToHex(signature),
    },
    kemPrivateKey: encapsulation.privateHex,
  };
}

async function buildFoundingBatch({
  orgId,
  rootPub,
  rootSigningKey,
  personalRootSeed,
  now,
  kemSeed = null,
}) {
  if (typeof orgId !== 'string' || !orgId) {
    throw new Error('orgId must be the stable local organization id');
  }
  requireLowerHex(rootPub, 64, 'rootPub');
  if (!Number.isSafeInteger(now) || now < 0) {
    throw new Error('now must be a non-negative integer timestamp');
  }

  const genesis = await signEvent(buildEvent({
    authorKey: rootPub,
    parents: [],
    hlc: [now, 0],
    payload: {
      type: 'genesis',
      org: orgId,
      root_pub: rootPub,
    },
  }), rootSigningKey);
  const genesisId = await eventId(genesis);
  const persona = await derivePersona(personalRootSeed, genesisId);

  const roleDefine = await signEvent(buildEvent({
    authorKey: rootPub,
    parents: [genesisId],
    hlc: [now, 1],
    payload: {
      type: 'role.define',
      name: 'owner',
      scope_set: ['*'],
      claim_requires: 'self',
      version: 1,
    },
  }), rootSigningKey);
  const roleDefineId = await eventId(roleDefine);

  const invitation = await signEvent(buildEvent({
    authorKey: rootPub,
    parents: [roleDefineId],
    hlc: [now, 2],
    payload: {
      type: 'invite',
      granted_role: 'owner',
      expiry: now,
      sponsor: rootPub,
      invite_pub: persona.publicHex,
    },
  }), rootSigningKey);
  const invitationId = await eventId(invitation);

  let kemCredential = null;
  let kemPrivateKey = null;
  if (kemSeed !== null && kemSeed !== undefined) {
    const built = await buildPersonaKemCredential({
      persona,
      genesisId,
      kemSeed,
      authorityHeads: [genesisId],
      createdHlc: [now, 0],
    });
    kemCredential = built.credential;
    kemPrivateKey = built.kemPrivateKey;
  }

  const claimPayload = {
    type: 'member.claim',
    invite_ref: invitationId,
    persona_pub: persona.publicHex,
    profile: {},
    approvals: [],
  };
  if (kemCredential !== null) {
    claimPayload.kem_credential = kemCredential;
  }
  const claim = await signEvent(buildEvent({
    authorKey: persona.publicHex,
    parents: [invitationId],
    hlc: [now, 3],
    payload: claimPayload,
  }), persona.signingKey);
  const claimId = await eventId(claim);
  const events = [genesis, roleDefine, invitation, claim];

  return {
    genesisId,
    founderPersonaPub: persona.publicHex,
    eventIds: [
      genesisId,
      roleDefineId,
      invitationId,
      claimId,
    ],
    events,
    wires: events.map((event) => canonicalJson(event)),
    kemCredential,
    kemPrivateKey,
  };
}

async function responseBody(response) {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

async function foundOrganization({
  org,
  transport,
  ...foundingInputs
}) {
  if (typeof org !== 'string' || !org) {
    throw new Error('org must be the local organization slug');
  }
  if (!transport || typeof transport.fetch !== 'function') {
    throw new Error('transport adapter is missing method fetch');
  }
  const batch = await buildFoundingBatch(foundingInputs);
  const response = await transport.fetch(
    '/api/network/ledger/found',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        org,
        events: batch.wires,
      }),
    },
  );
  const result = await responseBody(response);
  if (!response.ok) {
    throw new Error(
      `founding endpoint failed with ${response.status}: `
      + `${typeof result === 'string' ? result : JSON.stringify(result)}`,
    );
  }
  if (
    !result
    || result.ok !== true
    || result.genesis_id !== batch.genesisId
    || JSON.stringify(result.event_ids) !== JSON.stringify(batch.eventIds)
  ) {
    throw new Error('founding endpoint returned inconsistent event ids');
  }
  return {
    ...batch,
    server: result,
  };
}

export {
  buildFoundingBatch,
  buildPersonaKemCredential,
  foundOrganization,
};
