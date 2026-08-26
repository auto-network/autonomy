/* The one production signing path for Dashboard-access approvals.
 *
 * Both the legacy compatibility dialog and the Central inbox pass only the
 * server-frozen public grant. Factor selection and root opening stay inside
 * openRoot; the plaintext seed is always zeroed before this function returns.
 */
import { openRoot } from './open-root.js';

const GRANT_DOMAIN = 'autonomy.identity.dashboard-access-grant.v1\n';

export async function signDashboardAccessGrant(grant) {
  if (!grant || typeof grant !== 'object' || Array.isArray(grant)) {
    throw new Error('This access request has no server-frozen grant. Decline it and request a new one.');
  }
  const session = window.AutonomyNetworkSession;
  if (!session || !session._internals ||
      typeof session._internals.canonicalJson !== 'function' ||
      typeof session._internals.bytesToHex !== 'function') {
    throw new Error('Personal approval is unavailable in this browser. Reload and try again.');
  }
  const opened = await openRoot({
    title: 'Approve dashboard access',
    detail: 'Unlock your personal root to sign this access grant.',
  });
  if (!opened) throw new Error('Approval cancelled.');
  try {
    const input = new TextEncoder().encode(
      GRANT_DOMAIN + session._internals.canonicalJson(grant));
    const signature = await crypto.subtle.sign('Ed25519', opened.signingKey, input);
    return {
      grant,
      signature: session._internals.bytesToHex(new Uint8Array(signature)),
    };
  } finally {
    if (opened.seed) {
      opened.seed.fill(0);
      opened.seed = null;
    }
  }
}
