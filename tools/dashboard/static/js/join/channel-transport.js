/* A `transport.fetch` for ceremony/claim.js, backed by an org:join channel.
 *
 * claim.js is transport-neutral: it calls `context.transport.fetch(url, opts)`
 * with HTTP-shaped routes. On an invitee's own dashboard those routes would
 * hit the invitee's OWN ledger — the wrong org. The join reaches the INVITING
 * org over the root-pinned relaykit SecureChannel instead, so this adapter
 * reframes claim.js's three ledger calls as `sendOp` operations and hands back
 * a minimal fetch-shaped response. claim.js itself is not touched.
 *
 * invite_ref is never sent: an org:join channel's grant is scoped to exactly
 * one invitation, and _serve_join binds invite_ref server-side (defence in
 * depth). We send only what each op needs beyond that.
 *
 * The ledger-truth / link-truth split (auto-66v3w) is structural here:
 *   - a returned reply means the channel round-tripped; the org's answer
 *     (`result.status` = ok / gone / …) is the caller's to read — resolve ok.
 *   - a channel failure (offline, revoked, malformed reply) rejects, so the
 *     caller's catch sees LINK truth and never mistakes it for the ledger's.
 */
import { sendOp } from './channel-op.js';

export function makeChannelTransport(channel) {
  async function fetch(url, options = {}) {
    const request = requestForRoute(url, options);
    const reply = await sendOp(channel, request); // throws => LINK truth (rejects)
    return {
      ok: true, // channel round-trip succeeded; LEDGER truth lives in the body
      status: 200,
      async text() {
        return JSON.stringify(reply);
      },
    };
  }
  return { fetch };
}

function requestForRoute(url, options) {
  const method = (options.method || 'GET').toUpperCase();
  const [path, query = ''] = String(url).split('?');
  const params = new URLSearchParams(query);

  if (path.endsWith('/claim/context') && method === 'GET') {
    return { v: 1, op: 'context' };
  }
  if (path.endsWith('/ledger/claim') && method === 'POST') {
    const body = JSON.parse(options.body || '{}');
    if (typeof body.event !== 'string' || !body.event) {
      throw new Error('submit requires an event wire string');
    }
    return { v: 1, op: 'submit', event: body.event };
  }
  if (/\/ledger\/claim\/[0-9a-f]+$/.test(path) && method === 'GET') {
    const personaPub = params.get('persona_pub');
    if (!personaPub) {
      throw new Error('status requires persona_pub');
    }
    return { v: 1, op: 'status', persona_pub: personaPub };
  }
  if (/\/ledger\/claim\/[0-9a-f]+\/approval$/.test(path)) {
    // The invitee never countersigns their own claim — existing members
    // approve from their OWN nodes. The join channel serves no approval op
    // (_serve_join dispatches context / submit / status only).
    throw new Error('approvals are not served over the org:join channel');
  }
  throw new Error(`unroutable claim transport request: ${method} ${path}`);
}
