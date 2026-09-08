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
import { sendOp } from '../lib/relaykit-core.js';
import { makeChannelTransport } from './channel-transport.js';
import { submitClaim, getClaimStatus } from '../ceremony/claim.js';

const DEFAULT_POLL_MS = 2000;
const DEFAULT_MAX_POLLS = 600; // ~20 min ceiling at the default cadence

function defaultDelay() {
  return new Promise((resolve) => {
    setTimeout(resolve, DEFAULT_POLL_MS);
  });
}

// How many approvals a minted claim event actually carries. The event is
// canonical JSON bytes or an already-parsed object depending on the mint
// seam; both shapes answer the same question. An unreadable event counts as
// zero, so finalize() refuses rather than submitting something it cannot
// verify.
function countApprovals(event) {
  try {
    const parsed = typeof event === 'string' ? JSON.parse(event) : event;
    const payload = (parsed && parsed.payload) || parsed || {};
    return (payload.approvals || []).length;
  } catch (_) {
    return 0;
  }
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
    // The invitee's own per-org KEM key material, captured from the ceremony
    // seam. NEVER transmitted -- the page persists it locally only once the
    // claim is admitted, so the new member can read org-sealed data (finding b).
    this.kemPrivateKey = null;
    this.kemCredential = null;
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
    // Refuse to submit over a staged claim that already carries approvals.
    // A claim is addressed by claim_key = sha256(invite_ref ‖ persona_pub),
    // stable for this invitation and this joiner, and an approval is stored
    // against that staged row. Minting again produces the same key with a
    // fresh body whose approvals are empty, so submitting it REPLACES the
    // row and discards signatures already gathered — silently, since the
    // reply just reads 'pending' again. This cost a real operator approval
    // on 2026-09-08. Finalizing is finalize(), which carries them.
    const staged = await this._stagedApprovals();
    if (staged && staged.approvals.length) {
      return {
        state: 'already-approved',
        reason: 'submitting again would discard approvals already gathered',
        have: staged.have,
        need: staged.need,
      };
    }
    const minted = await this.runCeremony({
      context: this.context,
      inputs: this.inputs,
      passphrase,
    });
    this.claimKey = minted.claimKey;
    this.personaPub = minted.personaPub;
    this.kemPrivateKey = minted.kemPrivateKey || null;
    this.kemCredential = minted.kemCredential || null;
    let reply;
    try {
      reply = await submitClaim({ context: this.context, event: minted.event });
    } catch (_) {
      return { state: 'link-lost', reason: 'submit-failed' };
    }
    return this._fromLedger(reply);
  }

  // The staged row's approvals and its pinned causal position, or null when
  // nothing is staged yet. Both guards and finalize() read this first.
  async _stagedApprovals() {
    if (!this.claimKey || !this.personaPub) return null;
    let reply;
    try {
      reply = await getClaimStatus({
        context: this.context,
        claimKey: this.claimKey,
        inviteRef: this.inputs.inviteRef,
        personaPub: this.personaPub,
      });
    } catch (_) {
      return null;
    }
    return {
      approvals: (reply && reply.approvals) || [],
      position: (reply && reply.position) || null,
      have: (reply && reply.have) || 0,
      need: (reply && reply.need) || 0,
      status: reply && reply.status,
    };
  }

  // Admission is a SECOND submit: countersigning only marks a staged claim
  // ready, and the joiner must re-mint at the pinned position carrying the
  // approvals for the ledger to admit. Refuses rather than submits whenever
  // it would not be carrying them, so a mistimed call can never replace a
  // signed row with an empty one.
  async finalize(passphrase) {
    if (typeof this.runCeremony !== 'function') {
      throw new Error('acceptance ceremony is not enabled');
    }
    if (!this.ready()) {
      throw new Error('finalize() called before a successful connect()');
    }
    const staged = await this._stagedApprovals();
    if (!staged) return { state: 'link-lost', reason: 'status-failed' };
    if (staged.status === 'admitted') return { state: 'admitted' };
    if (!staged.approvals.length) {
      return { state: 'pending', reason: 'no approvals staged', approvals: [] };
    }
    if (!staged.position) {
      return { state: 'pending', reason: 'no pinned position' };
    }
    if (staged.need && staged.have < staged.need) {
      return { state: 'pending', reason: 'below threshold', have: staged.have, need: staged.need };
    }
    const minted = await this.runCeremony({
      context: this.context,
      inputs: this.inputs,
      passphrase,
      approvals: staged.approvals,
      position: staged.position,
    });
    // Prove the built event carries them before anything is sent: a ceremony
    // that quietly dropped either would otherwise submit the empty body this
    // method exists to prevent.
    const carried = countApprovals(minted.event);
    if (carried !== staged.approvals.length) {
      return {
        state: 'pending',
        reason: 'minted claim did not carry the approvals; not submitting',
        carried,
        expected: staged.approvals.length,
      };
    }
    this.claimKey = minted.claimKey;
    this.personaPub = minted.personaPub;
    this.kemPrivateKey = minted.kemPrivateKey || null;
    this.kemCredential = minted.kemCredential || null;
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
