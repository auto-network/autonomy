/* The organization-invitation mint ceremony's crypto core (auto-aopjw).
 *
 * One browser/node-portable function: derive the sponsor persona from the
 * personal root seed, mint a bearer token, build and sign the invite event at
 * the current heads (re-signing through 409 races), and append it through the
 * dashboard route. The bearer never rides any request — only its hash enters
 * the signed event; the caller alone holds the secret for the join link's
 * fragment. Proven cross-implementation by org-invite-vector.mjs against a
 * real founded ledger (test_org_invite_browser_ceremony.py).
 */
import {
  buildEvent,
  derivePersona,
  signEvent,
} from './ledger-event.js';
import {
  buildInviteBody,
  generateBearerToken,
} from './invitation.js';
import { canonicalJson } from './primitives.js';

async function readJson(response) {
  const body = await response.json().catch(() => ({}));
  if (!response.ok || body.ok === false) {
    const error = new Error(body.error || ('HTTP ' + response.status));
    error.status = response.status;
    throw error;
  }
  return body;
}

export async function mintOrgInvite({
  fetchImpl,
  serverUrl = '',
  org,
  genesisId,
  personalRootSeed,
  role,
  expiry,
  maxUses = null,
  attempts = 3,
}) {
  if (typeof fetchImpl !== 'function') throw new Error('fetchImpl must be a fetch function');
  if (typeof org !== 'string' || !org) throw new Error('org must be a non-empty slug');
  if (typeof role !== 'string' || !role) throw new Error('role must be a non-empty role name');
  const seed = new Uint8Array(personalRootSeed);
  try {
    const persona = await derivePersona(seed, genesisId);
    const token = await generateBearerToken();
    const body = buildInviteBody({
      grantedRole: role,
      expiry: expiry,
      sponsorPub: persona.publicHex,
      tokenHash: token.tokenHash,
      maxUses: maxUses && maxUses > 1 ? maxUses : null,
    });
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      const heads = await readJson(await fetchImpl(
        serverUrl + '/api/network/ledger/heads?org=' + encodeURIComponent(org),
        { headers: { Accept: 'application/json' } },
      ));
      const event = await signEvent(buildEvent({
        authorKey: persona.publicHex,
        parents: heads.heads,
        hlc: [Date.now(), 0],
        payload: body,
      }), persona.signingKey);
      const response = await fetchImpl(serverUrl + '/api/network/ledger/invite', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ org: org, event: canonicalJson(event) }),
      });
      if (response.status === 409 && attempt < attempts - 1) continue;
      const posted = await readJson(response);
      return { inviteId: posted.invite_id, bearer: token.token, expiry: expiry };
    }
    throw new Error('the authority ledger kept advancing; try again');
  } finally {
    seed.fill(0);
  }
}

export default mintOrgInvite;
