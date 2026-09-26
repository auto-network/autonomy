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

// r7kk4: the sponsor avatar is org-delivered and must be a bounded inline
// image data: URI — one of the three raster types the server adapts owned
// attachment bytes into. Anything else (a remote URL, a path, a non-image
// data: URI, a non-string) is dropped rather than mapped: rendering a remote
// URL here would be a registry-blind IP/UA leak on the exact page that
// promises otherwise. Mirrors the org_icon allowlist, narrowed to the avatar's
// accepted MIMEs.
const SPONSOR_AVATAR_PREFIXES = [
  'data:image/jpeg;base64,',
  'data:image/png;base64,',
  'data:image/webp;base64,',
];

function boundedSponsorAvatar(value) {
  if (typeof value !== 'string') return null;
  return SPONSOR_AVATAR_PREFIXES.some((prefix) => value.startsWith(prefix))
    ? value
    : null;
}

// The profile a claim signs (design §7): the reviewed name, biography and
// initials. Never the photo — the ledger is a permanent hash chain, not a
// blob store; the photo rides the member directory row instead.
function claimProfile(profile) {
  const source = profile && typeof profile === 'object' ? profile : {};
  const out = {};
  for (const key of ['display_name', 'biography', 'initials']) {
    if (typeof source[key] === 'string' && source[key].trim()) out[key] = source[key].trim();
  }
  return out;
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

  // Rung 1 — open the per-link-authenticated channel and read join context. Returns
  // {state:'org', brand, grantedRole, inviteExpiry} on success, or a terminal
  // ('closed' = ledger truth, 'link-lost' = link truth).
  async connect() {
    try {
      this.channel = await this.openChannel(this.inputs);
    } catch (error) {
      if (error && error.autonetKind === 'security') {
        return { state: 'security', reason: 'link-authentication-failed' };
      }
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
      grantedRole: reply.granted_role || null,
      inviteExpiry: reply.invite_expiry || null,
      binding: reply.binding || null,
      approvalPolicy: reply.approval_policy || null,
    };
    this.brand = {
      orgName: reply.org_name || null,
      orgDescription: reply.org_description || null,
      orgColor: reply.org_color || null,
      // Defence in depth: the icon is org-delivered and must be a bounded
      // data: URI (r7kk4). A remote URL here would be a registry-blind
      // IP/UA leak, so anything else is dropped rather than rendered.
      orgIcon:
        typeof reply.org_icon === 'string'
        && reply.org_icon.startsWith('data:image/')
          ? reply.org_icon
          : null,
    };
    this.context.presentation = {
      ...this.brand,
      sponsorName: reply.sponsor_name || null,
      sponsorByline: reply.sponsor_byline || null,
      // Bounded allowlist: only an inline JPEG/PNG/WebP data: URI is mapped
      // (r7kk4). Anything else is dropped, never rendered.
      sponsorAvatar: boundedSponsorAvatar(reply.sponsor_avatar),
      // Secondary provenance: the raw persona key stays available behind the
      // resolved human presentation, never as the headline identity.
      sponsorPub: reply.sponsor_pub || null,
    };
    return {
      state: 'org',
      brand: this.brand,
      grantedRole: reply.granted_role || null,
      inviteExpiry: reply.invite_expiry || null,
      context: this.context,
    };
  }

  // After admission: the organization's install material (ledger events,
  // registry binding, presentation) over the same authenticated channel.
  // The org's fold gates it — a persona it does not admit gets pending.
  async bootstrap() {
    if (!this.ready() || !this.personaPub) {
      throw new Error('bootstrap() needs a connected session and a minted persona');
    }
    // The ledger comes in pages (BOOTSTRAP_EVENT_PAGE server-side); ask
    // again with `after` while the reply says there is more.
    let reply;
    let events = [];
    let checkpoint;
    do {
      reply = await sendOp(this.channel, {
        v: 1, op: 'bootstrap', persona_pub: this.personaPub, after: events.length,
      });
      if (!reply || reply.status !== 'ok' || !Array.isArray(reply.events)) {
        throw new Error('the organization did not release its install material');
      }
      // The first page's checkpoint is the one whose ledger_head every
      // later page's events contain (the sponsor reads it before serving
      // events); a record adopted mid-paging could name a head not served.
      if (checkpoint === undefined) checkpoint = reply.checkpoint === undefined ? null : reply.checkpoint;
      events = events.concat(reply.events);
    } while (reply.more === true && reply.events.length > 0);
    return { ...reply, events, checkpoint };
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
    // The invitee signs its claim ONCE. Under an approval role the server
    // stages it and an approver admits it with an admission event carrying
    // the signed claim unchanged; the server refuses to replace a staged row
    // that already holds approvals, so a repeat accept cannot erase them.
    const minted = await this.runCeremony({
      context: this.context,
      inputs: this.inputs,
      profile: claimProfile(this.joiningProfile),
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
      // Approved but not yet admitted: the approver's admission event is on
      // its way; keep waiting, nothing for the invitee to do.
      return {
        state: 'pending',
        approvals: reply.approvals || null,
        have: reply.have || 0,
        need: reply.need || 0,
      };
    }
    return {
      state: 'closed',
      ledgerStatus: status || 'unknown',
      reason: reply.reason || status || 'unknown',
    };
  }
}
