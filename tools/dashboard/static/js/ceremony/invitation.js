/*
 * Shared invitation payload construction.
 *
 * Routine invitations are signed by the sponsor's per-organization persona,
 * never by the organization root. This module owns only the binding material
 * and payload shape; ledger-event.js owns signing and canonical wire bytes.
 */

import { bytesToHex } from './primitives.js';

let webCrypto = globalThis.crypto;
if (
  !webCrypto
  && typeof process !== 'undefined'
  && process.versions?.node
) {
  ({ webcrypto: webCrypto } = await import('node:crypto'));
}
if (!webCrypto?.getRandomValues || !webCrypto?.subtle) {
  throw new Error('invitation issuance requires WebCrypto');
}

const textEncoder = new TextEncoder();
const ROLE_PATTERN = /^[a-z0-9._-]{1,64}$/;
const KEY_PATTERN = /^[0-9a-f]{64}$/;
const UUID_PATTERN = (
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
);

function requireRole(value) {
  if (typeof value !== 'string' || !ROLE_PATTERN.test(value)) {
    throw new Error('grantedRole must use 1-64 characters from [a-z0-9-._]');
  }
  return value;
}

function requireKey(value, name) {
  if (typeof value !== 'string' || !KEY_PATTERN.test(value)) {
    throw new Error(`${name} must be 64 lowercase hex chars`);
  }
  return value;
}

async function generateBearerToken() {
  const token = bytesToHex(webCrypto.getRandomValues(new Uint8Array(32)));
  const digest = await webCrypto.subtle.digest(
    'SHA-256',
    textEncoder.encode(token),
  );
  return {
    token,
    tokenHash: bytesToHex(new Uint8Array(digest)),
  };
}

function buildInviteBody({
  grantedRole,
  expiry,
  sponsorPub,
  invitePub = null,
  tokenHash = null,
  maxUses = null,
}) {
  if (!Number.isSafeInteger(expiry) || expiry < 0) {
    throw new Error('expiry must be a non-negative integer unix-ms timestamp');
  }
  const hasInvitePub = invitePub !== null && invitePub !== undefined;
  const hasTokenHash = tokenHash !== null && tokenHash !== undefined;
  if (hasInvitePub === hasTokenHash) {
    throw new Error('invite requires exactly one key or bearer-token binding');
  }

  const body = {
    type: 'invite',
    granted_role: requireRole(grantedRole),
    expiry,
    sponsor: requireKey(sponsorPub, 'sponsorPub'),
  };
  if (hasInvitePub) {
    body.invite_pub = requireKey(invitePub, 'invitePub');
  } else {
    body.token_hash = requireKey(tokenHash, 'tokenHash');
  }
  // Optional multi-use bound. Absent means single-use (the event omits the
  // field entirely, byte-identical to legacy invites). Only a shareable
  // token_hash link may carry it; a key-bound invite is inherently single-use.
  if (maxUses !== null && maxUses !== undefined) {
    if (!Number.isSafeInteger(maxUses) || maxUses < 1) {
      throw new Error('maxUses must be an integer >= 1');
    }
    if (hasInvitePub) {
      throw new Error('maxUses is valid only with a bearer token, not invitePub');
    }
    body.max_uses = maxUses;
  }
  return body;
}

function buildOrgJoinGrantPayload({
  orgUuid,
  inviteId,
  inviteExpiry,
}) {
  if (typeof orgUuid !== 'string' || !UUID_PATTERN.test(orgUuid)) {
    throw new Error('orgUuid must be a canonical lowercase UUID');
  }
  requireKey(inviteId, 'inviteId');
  if (!Number.isSafeInteger(inviteExpiry) || inviteExpiry < 0) {
    throw new Error(
      'inviteExpiry must be a non-negative integer unix-ms timestamp',
    );
  }
  return {
    org: orgUuid,
    target_uuid: orgUuid,
    target_type: 'org:join',
    invite_ref: inviteId,
    // Absolute-to-absolute: no client/registry clock skew can shorten the
    // link below the ledger invitation's own redemption lifetime.
    expires_at: inviteExpiry,
  };
}

export {
  buildInviteBody,
  buildOrgJoinGrantPayload,
  generateBearerToken,
};
