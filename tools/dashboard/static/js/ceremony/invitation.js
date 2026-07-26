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
  return body;
}

export {
  buildInviteBody,
  generateBearerToken,
};
