/* The org:join flow, as a DOM-free orchestrator.
 *
 * It owns the sequence (connect -> read org context -> accept -> submit ->
 * poll to a terminal) and, above all, the ledger-truth / link-truth
 * discipline: a reply that arrives is the ORG speaking (admitted / pending /
 * gone / rejected / absent) and is the caller's to act on; a call that throws
 * is the LINK failing (org offline, channel revoked, malformed) and is never
 * dressed up as a ledger verdict. The page renders from the {state, ...}
 * objects each step returns.
 *
 * Two seams cross a boundary and are injected, never hard-wired here:
 *   openChannel(inputs) -> Promise<SecureChannel>
 *       relaykit/core's openSocket + performHandshake, once served own-origin.
 *   runCeremony({context, inputs, passphrase}) -> Promise<{event, personaPub, claimKey}>
 *       the HELD acceptance ceremony — decrypt armor, derive persona, sign the
 *       member.claim, ALL in the browser. This controller never receives the
 *       passphrase or the seed, only the signed public claim the seam returns.
 *       Supplied only once the operator has approved the acceptance contract;
 *       absent, accept() refuses rather than pretends.
 */
import { sendOp } from './channel-op.js';
import { makeChannelTransport } from './channel-transport.js';
import { submitClaim, getClaimStatus } from '../ceremony/claim.js';

const DEFAULT_POLL_MS = 2000;
const DEFAULT_MAX_POLLS = 600; // ~20 min ceiling at the default cadence

function defaultDelay() {
  return new Promise((resolve) => {
    setTimeout(resolve, DEFAULT_POLL_MS);
  });
}

export class JoinSession {
  constructor({ inputs, openChannel, runCeremony = null }) {
    this.inputs = inputs;
    this.openChannel = openChannel;
    this.runCeremony = runCeremony;
    this.channel = null;
    this.transport = null;
    this.context = null;
    this.brand = null;
    this.claimKey = null;
    this.personaPub = null;
  }

  ready() {
    return this.context !== null;
  }

  // Rung 1 — open the org-pinned channel and read the join context. Returns
  // {state:'org', brand, grantedRole, inviteExpiry} on success, or a terminal
  // ('closed' = ledger truth, 'link-lost' = link truth).
  async connect() {
    try {
      this.channel = await this.openChannel(this.inputs);
    } catch (_) {
      return { state: 'link-lost', reason: 'org-unreachable' };
    }
    let reply;
    try {
      reply = await sendOp(this.channel, { v: 1, op: 'context' });
    } catch (_) {
      return { state: 'link-lost', reason: 'link-failed' };
    }
    if (!reply || reply.status !== 'ok') {
      return {
        state: 'closed',
        ledgerStatus: (reply && reply.status) || 'gone',
        reason: (reply && reply.reason) || 'invite-not-found',
      };
    }
    this.transport = makeChannelTransport(this.channel);
    this.context = {
      transport: this.transport,
      orgSlug: this.inputs.org,
      genesisId: reply.genesis_id,
      heads: reply.heads,
      maxHlc: reply.max_hlc,
    };
    this.brand = {
      orgName: reply.org_name || null,
      orgDescription: reply.org_description || null,
      // Defence in depth: the icon is org-delivered and must be a bounded
      // data: URI (r7kk4). A remote URL here would be a registry-blind
      // IP/UA leak, so anything else is dropped rather than rendered.
      orgIcon:
        typeof reply.org_icon === 'string'
        && reply.org_icon.startsWith('data:image/')
          ? reply.org_icon
          : null,
    };
    return {
      state: 'org',
      brand: this.brand,
      grantedRole: reply.granted_role || null,
      inviteExpiry: reply.invite_expiry || null,
    };
  }

  // Rungs 2/3 — HELD. Mint the claim in the browser via the injected seam,
  // submit it, and report the first terminal or pending outcome.
  async accept(passphrase) {
    if (typeof this.runCeremony !== 'function') {
      throw new Error('acceptance ceremony is not enabled');
    }
    if (!this.ready()) {
      throw new Error('accept() called before a successful connect()');
    }
    const minted = await this.runCeremony({
      context: this.context,
      inputs: this.inputs,
      passphrase,
    });
    this.claimKey = minted.claimKey;
    this.personaPub = minted.personaPub;
    let reply;
    try {
      reply = await submitClaim({ context: this.context, event: minted.event });
    } catch (_) {
      return { state: 'link-lost', reason: 'submit-failed' };
    }
    return this._fromLedger(reply);
  }

  async pollOnce() {
    if (!this.claimKey || !this.personaPub) {
      throw new Error('pollOnce() called before accept()');
    }
    let reply;
    try {
      reply = await getClaimStatus({
        context: this.context,
        claimKey: this.claimKey,
        inviteRef: this.inputs.inviteRef,
        personaPub: this.personaPub,
      });
    } catch (_) {
      return { state: 'link-lost', reason: 'status-failed' };
    }
    return this._fromLedger(reply);
  }

  async pollUntilTerminal({ onProgress, delay, maxPolls } = {}) {
    const step = typeof delay === 'function' ? delay : defaultDelay;
    const limit = Number.isInteger(maxPolls) ? maxPolls : DEFAULT_MAX_POLLS;
    for (let i = 0; i < limit; i += 1) {
      const result = await this.pollOnce();
      if (result.state !== 'pending') {
        return result;
      }
      if (typeof onProgress === 'function') {
        onProgress(result);
      }
      await step();
    }
    return { state: 'pending-timeout' };
  }

  // A single ledger reply -> a terminal or pending state. 'admitted' is
  // success; 'pending' keeps the link-with-this-page open; everything else the
  // org returns (gone / rejected / absent) is an authoritative close, carried
  // verbatim so the page can say WHY.
  _fromLedger(reply) {
    const status = reply && reply.status;
    if (status === 'admitted') {
      return { state: 'admitted' };
    }
    if (status === 'pending') {
      return {
        state: 'pending',
        approvals: reply.approvals || null,
      };
    }
    return {
      state: 'closed',
      ledgerStatus: status || 'unknown',
      reason: reply.reason || status || 'unknown',
    };
  }
}
