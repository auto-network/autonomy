/*
 * Discovery-agnostic member-claim ceremony.
 *
 * The caller supplies the org-node transport and explicit authority context.
 * This module derives the invitee persona from the PERSONAL root, mints the
 * persona KEM credential, signs the claim, and exposes the countersign/poll/
 * re-mint protocol. It never stores KEM private material: kemPrivateKey is
 * returned to the caller for its external device store.
 */

import {
  bytesToHex,
  canonicalJson,
  domainBytes,
} from './primitives.js';
import {
  buildEvent,
  derivePersona,
  signEvent,
} from './ledger-event.js';
import { buildPersonaKemCredential } from './founding.js';

const APPROVAL_DOMAIN = 'autonomy.ledger.approval.v1\n';
const HEX_64 = /^[0-9a-f]{64}$/;

let webCrypto = globalThis.crypto;
if (
  !webCrypto
  && typeof process !== 'undefined'
  && process.versions?.node
) {
  ({ webcrypto: webCrypto } = await import('node:crypto'));
}
if (!webCrypto?.subtle) {
  throw new Error('member claims require WebCrypto');
}

const textEncoder = new TextEncoder();

function requireHash(value, name) {
  if (typeof value !== 'string' || !HEX_64.test(value)) {
    throw new Error(`${name} must be 64 lowercase hex chars`);
  }
  return value;
}

function normalizeHeads(heads, name = 'context.heads') {
  if (!Array.isArray(heads)) {
    throw new Error(`${name} must be an array`);
  }
  const normalized = heads.map((head) => requireHash(head, `${name} entry`));
  const unique = Array.from(new Set(normalized)).sort();
  if (JSON.stringify(unique) !== JSON.stringify(normalized)) {
    throw new Error(`${name} must be sorted and duplicate-free`);
  }
  return unique;
}

function normalizeHlc(hlc, name = 'hlc') {
  if (
    !Array.isArray(hlc)
    || hlc.length !== 2
    || !hlc.every(
      (value) => Number.isSafeInteger(value) && value >= 0,
    )
  ) {
    throw new Error(`${name} must be two non-negative safe integers`);
  }
  return [hlc[0], hlc[1]];
}

function normalizeApprovals(approvals) {
  if (!Array.isArray(approvals)) {
    throw new Error('approvals must be an array');
  }
  const normalized = approvals.map((entry) => {
    if (
      !entry
      || typeof entry !== 'object'
      || Array.isArray(entry)
      || Object.keys(entry).sort().join(',') !== 'key,sig'
    ) {
      throw new Error('each approval must be exactly {key, sig}');
    }
    requireHash(entry.key, 'approval key');
    if (
      typeof entry.sig !== 'string'
      || !/^[0-9a-f]{128}$/.test(entry.sig)
    ) {
      throw new Error('approval sig must be 128 lowercase hex chars');
    }
    return { key: entry.key, sig: entry.sig };
  }).sort((left, right) => (
    left.key < right.key ? -1 : left.key > right.key ? 1 : 0
  ));
  if (new Set(normalized.map((entry) => entry.key)).size !== normalized.length) {
    throw new Error('approvals must have distinct keys');
  }
  return normalized;
}

function requireContext(context) {
  if (
    !context
    || typeof context !== 'object'
    || !context.transport
    || typeof context.transport.fetch !== 'function'
  ) {
    throw new Error('context.transport must provide fetch');
  }
  if (typeof context.orgSlug !== 'string' || !context.orgSlug) {
    throw new Error('context.orgSlug must be a non-empty string');
  }
  return {
    transport: context.transport,
    orgSlug: context.orgSlug,
    genesisId: requireHash(context.genesisId, 'context.genesisId'),
    heads: normalizeHeads(context.heads),
    maxHlc: normalizeHlc(context.maxHlc, 'context.maxHlc'),
  };
}

function tickHlc(maxHlc, nowMs = Date.now()) {
  const observed = normalizeHlc(maxHlc, 'maxHlc');
  if (!Number.isSafeInteger(nowMs) || nowMs < 0) {
    throw new Error('nowMs must be a non-negative safe integer');
  }
  if (nowMs > observed[0]) return [nowMs, 0];
  return [observed[0], observed[1] + 1];
}

function approvalCore({ inviteRef, personaPub }) {
  return {
    kind: 'member.claim',
    invite_ref: requireHash(inviteRef, 'inviteRef'),
    persona_pub: requireHash(personaPub, 'personaPub'),
  };
}

async function claimKey(inviteRef, personaPub) {
  const digest = await webCrypto.subtle.digest(
    'SHA-256',
    textEncoder.encode(
      `${requireHash(inviteRef, 'inviteRef')}`
      + `${requireHash(personaPub, 'personaPub')}`,
    ),
  );
  return bytesToHex(new Uint8Array(digest));
}

async function mintMemberClaim({
  context,
  personalRootSeed,
  inviteRef,
  token = null,
  profile = {},
  kemSeed,
  nowMs = Date.now(),
  credentialHlc = null,
}) {
  const resolved = requireContext(context);
  requireHash(inviteRef, 'inviteRef');
  if (token !== null && (typeof token !== 'string' || !token || token.length > 128)) {
    throw new Error('token must be a non-empty string of at most 128 chars');
  }
  if (!profile || typeof profile !== 'object' || Array.isArray(profile)) {
    throw new Error('profile must be an object');
  }
  canonicalJson(profile);
  // The invitee signs its claim ONCE, at the current heads. Under an approval
  // role an approver later admits it with an admission event that carries
  // this signed claim unchanged; the invitee never re-mints.
  const eventPosition = {
    parents: resolved.heads,
    hlc: tickHlc(resolved.maxHlc, nowMs),
  };
  const eventHlc = eventPosition.hlc;
  const createdHlc = credentialHlc === null
    ? eventHlc
    : normalizeHlc(credentialHlc, 'credentialHlc');
  const seed = new Uint8Array(personalRootSeed);
  const encapsulationSeed = new Uint8Array(kemSeed);
  if (seed.length !== 32 || encapsulationSeed.length !== 32) {
    // Zero BOTH copies on either malformed-length exit -- neither the root
    // seed nor the kem seed may survive a rejected mint.
    const message = seed.length !== 32
      ? 'personalRootSeed must be exactly 32 raw bytes'
      : 'kemSeed must be exactly 32 raw bytes';
    seed.fill(0);
    encapsulationSeed.fill(0);
    throw new Error(message);
  }

  let persona;
  let credential;
  let kemPrivateKey;
  try {
    persona = await derivePersona(seed, resolved.genesisId);
    ({ credential, kemPrivateKey } = await buildPersonaKemCredential({
      persona,
      genesisId: resolved.genesisId,
      kemSeed: encapsulationSeed,
      authorityHeads: eventPosition.parents,
      createdHlc,
    }));
  } finally {
    seed.fill(0);
    encapsulationSeed.fill(0);
  }

  const payload = {
    type: 'member.claim',
    invite_ref: inviteRef,
    persona_pub: persona.publicHex,
    profile: { ...profile },
    approvals: [],
    kem_credential: credential,
  };
  if (token !== null) payload.token = token;
  const event = await signEvent(buildEvent({
    authorKey: persona.publicHex,
    parents: eventPosition.parents,
    hlc: eventHlc,
    payload,
  }), persona.signingKey);
  return {
    event,
    wire: canonicalJson(event),
    claimKey: await claimKey(inviteRef, persona.publicHex),
    personaPub: persona.publicHex,
    kemCredential: credential,
    kemPrivateKey,
  };
}

async function buildClaimApproval({
  approverPub,
  approverSigningKey,
  inviteRef,
  personaPub,
}) {
  requireHash(approverPub, 'approverPub');
  if (
    !approverSigningKey
    || approverSigningKey.type !== 'private'
    || approverSigningKey.algorithm?.name !== 'Ed25519'
    || !approverSigningKey.usages?.includes('sign')
  ) {
    throw new Error('approverSigningKey must be an Ed25519 signing key');
  }
  const sig = await webCrypto.subtle.sign(
    'Ed25519',
    approverSigningKey,
    domainBytes(
      APPROVAL_DOMAIN,
      canonicalJson(approvalCore({ inviteRef, personaPub })),
    ),
  );
  return { key: approverPub, sig: bytesToHex(sig) };
}

// The approver's admission event (OrgAdmission.tla admission event): the
// invitee's signed claim wire UNCHANGED plus the counted approvals, authored
// by this approver at the heads the server named. `admission` is exactly
// what the countersign route returned as `ready.admission`.
async function signAdmission({
  context,
  personalRootSeed,
  admission,
  nowMs = Date.now(),
}) {
  const resolved = requireContext(context);
  if (!admission || typeof admission !== 'object'
      || typeof admission.claim !== 'string' || !admission.claim) {
    throw new Error('admission.claim must be the invitee-signed claim wire');
  }
  const seed = new Uint8Array(personalRootSeed);
  if (seed.length !== 32) {
    throw new Error('personalRootSeed must be exactly 32 raw bytes');
  }
  try {
    const persona = await derivePersona(seed, resolved.genesisId);
    const payload = {
      type: 'member.admission',
      claim: admission.claim,
      approvals: normalizeApprovals(admission.approvals || []),
    };
    const event = await signEvent(buildEvent({
      authorKey: persona.publicHex,
      parents: normalizeHeads(admission.parents, 'admission.parents'),
      hlc: [nowMs, 0],
      payload,
    }), persona.signingKey);
    return { event, wire: canonicalJson(event), personaPub: persona.publicHex };
  } finally {
    seed.fill(0);
  }
}

const CHECKPOINT_DOMAIN = 'autonomy.network.membership.checkpoint.v1\n';

// The persona-signed advancing checkpoint (membership_checkpoint.checkpoint_due,
// sign_with persona), signed here when the admitting node holds no
// checkpoint-scoped delegate: the approver's persona is open in this window
// (OrgAdmissionEvent.tla ApprovalPublishesCheckpoint).
async function signCheckpointRecord({ context, personalRootSeed, record }) {
  const resolved = requireContext(context);
  if (!record || typeof record !== 'object' || Array.isArray(record)) {
    throw new Error('record must be the assembled checkpoint object');
  }
  const seed = new Uint8Array(personalRootSeed);
  if (seed.length !== 32) {
    throw new Error('personalRootSeed must be exactly 32 raw bytes');
  }
  try {
    const persona = await derivePersona(seed, resolved.genesisId);
    if (record.signer !== persona.publicHex) {
      throw new Error('checkpoint record names another signer');
    }
    const unsigned = { ...record };
    delete unsigned.sig;
    const sig = await webCrypto.subtle.sign('Ed25519', persona.signingKey,
      domainBytes(CHECKPOINT_DOMAIN, canonicalJson(unsigned)));
    return { ...unsigned, sig: bytesToHex(sig) };
  } finally {
    seed.fill(0);
  }
}

async function submitCheckpoint({ context, record }) {
  const resolved = requireContext(context);
  return fetchJson(
    resolved,
    '/api/network/membership-checkpoint',
    {
      method: 'POST',
      headers: requestHeaders(resolved, true),
      body: JSON.stringify({ org: resolved.orgSlug, record }),
    },
    'checkpoint publish',
  );
}

async function submitAdmission({ context, wire }) {
  const resolved = requireContext(context);
  return fetchJson(
    resolved,
    '/api/network/ledger/admission',
    {
      method: 'POST',
      headers: requestHeaders(resolved, true),
      body: JSON.stringify({ org: resolved.orgSlug, event: wire }),
    },
    'admission submit',
  );
}

async function signClaimApproval({
  context,
  personalRootSeed,
  inviteRef,
  personaPub,
}) {
  const resolved = requireContext(context);
  const seed = new Uint8Array(personalRootSeed);
  if (seed.length !== 32) {
    throw new Error('personalRootSeed must be exactly 32 raw bytes');
  }
  try {
    const persona = await derivePersona(seed, resolved.genesisId);
    return buildClaimApproval({
      approverPub: persona.publicHex,
      approverSigningKey: persona.signingKey,
      inviteRef,
      personaPub,
    });
  } finally {
    seed.fill(0);
  }
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

async function fetchJson(context, route, options, action) {
  const resolved = requireContext(context);
  const response = await resolved.transport.fetch(route, options);
  const result = await responseBody(response);
  if (!response.ok) {
    const error = new Error(
      `${action} failed with ${response.status}: `
      + `${typeof result === 'string' ? result : JSON.stringify(result)}`,
    );
    error.status = response.status;
    error.body = result;
    throw error;
  }
  return result;
}

async function getClaimContext({
  transport,
  orgSlug,
  inviteRef,
}) {
  if (!transport || typeof transport.fetch !== 'function') {
    throw new Error('transport must provide fetch');
  }
  if (typeof orgSlug !== 'string' || !orgSlug) {
    throw new Error('orgSlug must be a non-empty string');
  }
  requireHash(inviteRef, 'inviteRef');
  const response = await transport.fetch(
    '/api/network/ledger/claim/context'
      + `?org=${encodeURIComponent(orgSlug)}`
      + `&invite_ref=${encodeURIComponent(inviteRef)}`,
    { headers: { 'X-Graph-Org': orgSlug } },
  );
  const result = await responseBody(response);
  if (!response.ok) {
    const error = new Error(
      `claim context failed with ${response.status}: `
      + `${typeof result === 'string' ? result : JSON.stringify(result)}`,
    );
    error.status = response.status;
    error.body = result;
    throw error;
  }
  if (
    !result
    || result.status !== 'ok'
    || !Array.isArray(result.heads)
    || !Array.isArray(result.max_hlc)
  ) {
    throw new Error('claim context response is malformed');
  }
  return {
    transport,
    orgSlug,
    genesisId: result.genesis_id,
    heads: result.heads,
    maxHlc: result.max_hlc,
    grantedRole: result.granted_role,
    binding: result.binding,
    inviteExpiry: result.invite_expiry,
  };
}

function requestHeaders(context, json = false) {
  const headers = { 'X-Graph-Org': context.orgSlug };
  if (json) headers['Content-Type'] = 'application/json';
  return headers;
}

async function submitClaim({ context, event }) {
  const resolved = requireContext(context);
  if (
    !event
    || typeof event !== 'object'
    || JSON.stringify(event.parents) !== JSON.stringify(resolved.heads)
  ) {
    throw new Error('claim event parents must equal context.heads');
  }
  return fetchJson(
    resolved,
    '/api/network/ledger/claim',
    {
      method: 'POST',
      headers: requestHeaders(resolved, true),
      body: JSON.stringify({
        org: resolved.orgSlug,
        event: canonicalJson(event),
      }),
    },
    'claim submit',
  );
}

async function getClaimStatus({
  context,
  claimKey: key,
  inviteRef,
  personaPub,
}) {
  const resolved = requireContext(context);
  requireHash(key, 'claimKey');
  requireHash(inviteRef, 'inviteRef');
  requireHash(personaPub, 'personaPub');
  return fetchJson(
    resolved,
    `/api/network/ledger/claim/${key}`
      + `?org=${encodeURIComponent(resolved.orgSlug)}`
      + `&invite_ref=${encodeURIComponent(inviteRef)}`
      + `&persona_pub=${encodeURIComponent(personaPub)}`,
    { headers: requestHeaders(resolved) },
    'claim status',
  );
}

async function submitClaimApproval({
  context,
  claimKey: key,
  inviteRef,
  personaPub,
  approval,
}) {
  const resolved = requireContext(context);
  requireHash(key, 'claimKey');
  requireHash(inviteRef, 'inviteRef');
  requireHash(personaPub, 'personaPub');
  return fetchJson(
    resolved,
    `/api/network/ledger/claim/${key}/approval`,
    {
      method: 'POST',
      headers: requestHeaders(resolved, true),
      body: JSON.stringify({
        org: resolved.orgSlug,
        invite_ref: inviteRef,
        persona_pub: personaPub,
        approval,
      }),
    },
    'claim approval',
  );
}

function admittingApprovals(approvals, admitting) {
  const normalized = normalizeApprovals(approvals);
  if (
    !Array.isArray(admitting)
    || admitting.some((key) => !HEX_64.test(key))
    || admitting.length !== new Set(admitting).size
  ) {
    throw new Error('admitting must be a distinct approval-key array');
  }
  const wanted = new Set(admitting);
  const selected = normalized.filter((entry) => wanted.has(entry.key));
  if (selected.length !== wanted.size) {
    throw new Error('status approvals do not contain every admitting key');
  }
  return selected;
}

export {
  admittingApprovals,
  approvalCore,
  buildClaimApproval,
  claimKey,
  getClaimContext,
  getClaimStatus,
  mintMemberClaim,
  signAdmission,
  signCheckpointRecord,
  signClaimApproval,
  submitAdmission,
  submitCheckpoint,
  submitClaim,
  submitClaimApproval,
  tickHlc,
};
