/* The organization-role ceremonies' crypto core (auto-oazv4).
 *
 * Three browser/node-portable functions over the ledger's role vocabulary
 * (roles design of record graph://d1b3db8f-879):
 *
 *   defineRole  — signs a `role.define` with the ORG ROOT. The root seed is
 *                 unsealed from the org-key armor with the personal root seed
 *                 (the same step registry registration performs), imported as
 *                 an Ed25519 signing key, used once, and zeroed. Only the root
 *                 can define a role that carries scopes: role-held scopes are
 *                 never delegable, so an owner persona would fold as
 *                 role-define-overreach for anything but an empty set.
 *   grantRole   — signs a `role.grant` with the PERSONA derived from the
 *                 personal root, exactly like the invite mint.
 *   revokeRole  — signs a `role.revoke` with the persona.
 *
 * Each builds the event at the current heads, posts it to its route, and
 * re-signs through 409 races. The route trial-folds and refuses an
 * unauthorized change with the fold's reason; that reason is surfaced on
 * the thrown error as `error.reason`. No secret rides any request: only the
 * signed event crosses the wire. Proven cross-implementation by
 * org-role-vector.mjs against a real founded ledger
 * (test_org_role_browser_ceremony.py).
 */
import {
  buildEvent,
  derivePersona,
  importDerivedPersonaKey,
  signEvent,
} from './ledger-event.js';
import { openSealedArmor } from './sealing.js';
import { canonicalJson } from './primitives.js';

const ROUTES = {
  'role.define': '/api/network/ledger/role-define',
  'role.grant': '/api/network/ledger/role-grant',
  'role.revoke': '/api/network/ledger/role-revoke',
};

const CLAIM_REQUIRES = ['self', 'sponsor', 'admin-ack'];

async function readJson(response) {
  const body = await response.json().catch(() => ({}));
  if (!response.ok || body.ok === false) {
    const error = new Error(body.error || ('HTTP ' + response.status));
    error.status = response.status;
    if (body.reason) error.reason = body.reason;
    if (body.expected_version != null) error.expectedVersion = body.expected_version;
    throw error;
  }
  return body;
}

function requireFetch(fetchImpl) {
  if (typeof fetchImpl !== 'function') throw new Error('fetchImpl must be a fetch function');
}

function requireSlug(org) {
  if (typeof org !== 'string' || !org) throw new Error('org must be a non-empty slug');
}

function requireName(value, what) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(value)) {
    throw new Error(what + ' must be a role name: letters, digits, "_", ".", "-"');
  }
  return value;
}

function requireKeyHex(value, what) {
  if (typeof value !== 'string' || !/^[0-9a-f]{64}$/.test(value)) {
    throw new Error(what + ' must be 64 lowercase hex characters');
  }
  return value;
}

/** The `role.define` payload, validated the way events.py validates it. */
export function buildRoleDefinition({
  name,
  scopeSet = [],
  claimRequires = 'admin-ack',
  version,
  approverThreshold = null,
}) {
  requireName(name, 'name');
  if (!Array.isArray(scopeSet)) throw new Error('scopeSet must be an array of scopes');
  const scopes = Array.from(new Set(scopeSet.map((scope) => {
    if (typeof scope !== 'string' || !scope) throw new Error('every scope must be a non-empty string');
    return scope;
  }))).sort();
  if (!CLAIM_REQUIRES.includes(claimRequires)) {
    throw new Error('claimRequires must be one of ' + CLAIM_REQUIRES.join(', '));
  }
  if (!Number.isInteger(version) || version < 1) {
    throw new Error('version must be a positive integer');
  }
  const payload = {
    type: 'role.define',
    name: name,
    scope_set: scopes,
    claim_requires: claimRequires,
    version: version,
  };
  if (approverThreshold != null) {
    if (claimRequires !== 'admin-ack') {
      throw new Error('approverThreshold applies only to admin-ack roles');
    }
    if (!Number.isInteger(approverThreshold) || approverThreshold < 1) {
      throw new Error('approverThreshold must be a positive integer');
    }
    payload.approver_threshold = { kind: 'static', count: approverThreshold };
  }
  return payload;
}

/** Sign `payload` as `authorKey` at the current heads and post it to the
 * kind's route, re-signing through 409 races. Returns the route's body. */
async function postSignedRoleEvent({
  fetchImpl,
  serverUrl,
  org,
  kind,
  authorKey,
  signingKey,
  payload,
  attempts,
}) {
  const route = ROUTES[kind];
  if (!route) throw new Error('unknown role event kind: ' + kind);
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const heads = await readJson(await fetchImpl(
      serverUrl + '/api/network/ledger/heads?org=' + encodeURIComponent(org),
      { headers: { Accept: 'application/json' } },
    ));
    const event = await signEvent(buildEvent({
      authorKey: authorKey,
      parents: heads.heads,
      hlc: [Date.now(), 0],
      payload: payload,
    }), signingKey);
    const response = await fetchImpl(serverUrl + route, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ org: org, event: canonicalJson(event) }),
    });
    if (response.status === 409 && attempt < attempts - 1) continue;
    return readJson(response);
  }
  throw new Error('the authority ledger kept advancing; try again');
}

/** Fetch the org-key armor the server holds for `org`. */
async function fetchOrgKeyArmor(fetchImpl, serverUrl, org) {
  const response = await fetchImpl(
    serverUrl + '/api/network/org-key?org=' + encodeURIComponent(org),
    { headers: { Accept: 'application/json' } },
  );
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || ('HTTP ' + response.status));
  return body;
}

/** Define a role, or a new version of one, signed by the org root.
 *
 * `version` may be omitted: the first attempt is signed as version 1 and,
 * when the route answers with `expected_version`, the definition is
 * re-signed at that version. A caller that pins `version` gets no retry.
 */
export async function defineRole({
  fetchImpl,
  serverUrl = '',
  org,
  orgKeyArmor = null,
  personalRootSeed,
  name,
  scopeSet = [],
  claimRequires = 'admin-ack',
  version = null,
  approverThreshold = null,
  attempts = 3,
}) {
  requireFetch(fetchImpl);
  requireSlug(org);
  const personalSeed = new Uint8Array(personalRootSeed);
  let rootSeed = null;
  try {
    const armor = orgKeyArmor || await fetchOrgKeyArmor(fetchImpl, serverUrl, org);
    if (!armor || !armor.sealed_root_key) {
      throw new Error('this organization has no sealed root key to open');
    }
    rootSeed = await openSealedArmor({
      sealed_root_key: armor.sealed_root_key,
      seal_purpose: armor.seal_purpose,
    }, personalSeed);
    const root = await importDerivedPersonaKey(rootSeed);
    if (armor.root_pub && root.publicHex !== armor.root_pub) {
      throw new Error('the unsealed key is not this organization\'s root');
    }
    const pinned = version != null;
    let attemptVersion = pinned ? version : 1;
    for (let round = 0; round < attempts; round += 1) {
      const payload = buildRoleDefinition({
        name, scopeSet, claimRequires, version: attemptVersion, approverThreshold,
      });
      try {
        const posted = await postSignedRoleEvent({
          fetchImpl, serverUrl, org, kind: 'role.define',
          authorKey: root.publicHex, signingKey: root.signingKey, payload, attempts,
        });
        return { eventId: posted.event_id, version: attemptVersion, rootPub: root.publicHex };
      } catch (error) {
        if (!pinned && error.expectedVersion != null && round < attempts - 1) {
          attemptVersion = error.expectedVersion;
          continue;
        }
        throw error;
      }
    }
    throw new Error('could not settle the role version; try again');
  } finally {
    if (rootSeed) rootSeed.fill(0);
    personalSeed.fill(0);
  }
}

async function personaRoleEvent({
  fetchImpl,
  serverUrl = '',
  org,
  genesisId,
  personalRootSeed,
  kind,
  persona,
  role,
  attempts = 3,
}) {
  requireFetch(fetchImpl);
  requireSlug(org);
  requireKeyHex(persona, 'persona');
  requireName(role, 'role');
  const seed = new Uint8Array(personalRootSeed);
  try {
    const signer = await derivePersona(seed, genesisId);
    const posted = await postSignedRoleEvent({
      fetchImpl, serverUrl, org, kind,
      authorKey: signer.publicHex, signingKey: signer.signingKey,
      payload: { type: kind, persona: persona, role: role },
      attempts,
    });
    return { eventId: posted.event_id, signer: signer.publicHex };
  } finally {
    seed.fill(0);
  }
}

/** Confer `role` on `persona`, signed by the caller's persona (which must
 * hold `role:grant:<role>`; the fold refuses otherwise). */
export function grantRole(options) {
  return personaRoleEvent({ ...options, kind: 'role.grant' });
}

/** Strip `role` from `persona`, signed by the caller's persona (root, the
 * persona itself, or a holder of `role:grant:<role>`). */
export function revokeRole(options) {
  return personaRoleEvent({ ...options, kind: 'role.revoke' });
}

export default defineRole;
