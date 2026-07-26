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

function normalizePosition(position) {
  if (
    !position
    || typeof position !== 'object'
    || Array.isArray(position)
    || Object.keys(position).sort().join(',') !== 'hlc,parents'
  ) {
    throw new Error('position must be exactly {parents, hlc}');
  }
  return {
    parents: normalizeHeads(position.parents, 'position.parents'),
    hlc: normalizeHlc(position.hlc, 'position.hlc'),
  };
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
  approvals = [],
  kemSeed,
  nowMs = Date.now(),
  credentialHlc = null,
  position = null,
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
  const eventPosition = position === null
    ? {
      parents: resolved.heads,
      hlc: tickHlc(resolved.maxHlc, nowMs),
    }
    : normalizePosition(position);
  const eventHlc = eventPosition.hlc;
  const requestedCredentialHlc = credentialHlc === null
    ? eventHlc
    : normalizeHlc(credentialHlc, 'credentialHlc');
  if (
    position !== null
    && JSON.stringify(requestedCredentialHlc) !== JSON.stringify(eventHlc)
  ) {
    throw new Error('credentialHlc must equal position.hlc when finalizing');
  }
  const createdHlc = position === null ? requestedCredentialHlc : eventHlc;
  const seed = new Uint8Array(personalRootSeed);
  const encapsulationSeed = new Uint8Array(kemSeed);
  if (seed.length !== 32) {
    throw new Error('personalRootSeed must be exactly 32 raw bytes');
  }
  if (encapsulationSeed.length !== 32) {
    seed.fill(0);
    throw new Error('kemSeed must be exactly 32 raw bytes');
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
    approvals: normalizeApprovals(approvals),
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

async function submitClaim({ context, event, position = null }) {
  const resolved = requireContext(context);
  const expected = position === null
    ? { parents: resolved.heads, hlc: null }
    : normalizePosition(position);
  if (
    !event
    || typeof event !== 'object'
    || JSON.stringify(event.parents) !== JSON.stringify(expected.parents)
  ) {
    throw new Error(
      position === null
        ? 'claim event parents must equal context.heads'
        : 'claim event parents must equal position.parents',
    );
  }
  if (
    expected.hlc !== null
    && JSON.stringify(event.hlc) !== JSON.stringify(expected.hlc)
  ) {
    throw new Error('claim event hlc must equal position.hlc');
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
  signClaimApproval,
  submitClaim,
  submitClaimApproval,
  tickHlc,
};
