/* The org:join controller, driven end to end against a scripted stub channel
 * and a stub ceremony seam. No relaykit/core, no relay, no real ceremony — the
 * whole ladder's SHAPE is exercised, so the seams drop in later with no
 * surprises. Run: node accept-controller.test.mjs
 */
import assert from 'node:assert/strict';

import { JoinSession } from '../accept-controller.js';
import { canonicalJson } from '../../lib/relaykit-core.js';

const te = new TextEncoder();
const td = new TextDecoder();

const ORG = '11111111-1111-4111-8111-111111111111';
const GENESIS = 'b'.repeat(64);
const HEADS = ['1'.repeat(64), '2'.repeat(64)];
const MAXHLC = [12345, 0];
const INVITE = 'e'.repeat(64);
const PERSONA = 'f'.repeat(64);
const CLAIMKEY = 'c'.repeat(64);

const INPUTS = {
  org: ORG, channelPub: 'a'.repeat(64), inviteRef: INVITE,
  channelToken: 'd'.repeat(32), bearer: 'b'.repeat(64),
};

// Ledger approval readiness must leave the polling loop for finalization.
{
  const session = new JoinSession({ inputs: INPUTS });
  const approved = { status: 'pending', have: 1, need: 1,
    approvals: [{ signer: 'owner' }], admitting: ['owner'],
    position: { parents: HEADS, hlc: MAXHLC } };
  session.pollOnce = async () => session._fromLedger(approved);
  assert.equal((await session.pollUntilTerminal({ maxPolls: 1,
    delay: async () => { throw new Error('Approved claim must finalize, not keep polling'); },
  })).state, 'already-approved');
  assert.equal(session._fromLedger({ ...approved, have: 0 }).state, 'pending');
  assert.equal(session._fromLedger({ ...approved, position: null }).state, 'pending');
}

// A SecureChannel-shaped stub whose replies are scripted per op and per Nth
// call to that op. A handler may return an object (encoded reply), a raw
// string (a non-{v:1} link refusal), or throw (an offline channel).
class ScriptedChannel {
  constructor(handler) {
    this.handler = handler;
    this.sent = [];
    this.calls = { context: 0, submit: 0, status: 0 };
  }

  async sendMessage(bytes) {
    this._last = JSON.parse(td.decode(bytes).replace(/\n$/, ''));
    this.sent.push(this._last);
  }

  async recvMessage() {
    const req = this._last;
    this.calls[req.op] += 1;
    const reply = this.handler(req, this.calls[req.op]); // may throw
    if (typeof reply === 'string') return te.encode(reply);
    return te.encode(`${canonicalJson(reply)}\n`);
  }
}

const okContext = (over = {}) => ({
  v: 1, status: 'ok', genesis_id: GENESIS, heads: HEADS, max_hlc: MAXHLC,
  org_name: 'Anchore', org_description: 'container security',
  org_icon: 'data:image/png;base64,AAAA', granted_role: 'member',
  invite_expiry: [99999, 0], ...over,
});

const stubCeremony = async ({ context, inputs, passphrase }) => {
  // The seam gets the context and inputs; the passphrase passes THROUGH the
  // controller untouched (it must never inspect or retain it).
  assert.equal(passphrase, 'correct horse');
  assert.equal(context.orgSlug, ORG);
  return {
    event: { parents: HEADS, kind: 'member.claim', payload: { invite_ref: inputs.inviteRef } },
    personaPub: PERSONA,
    claimKey: CLAIMKEY,
    kemPrivateKey: { secret: 'per-org-kem-private' },
    kemCredential: { pub: 'per-org-kem-public' },
  };
};

// 1. connect -> 'org', brand lit, context stashed, one op:context sent.
{
  const ch = new ScriptedChannel((req) => (req.op === 'context' ? okContext() : {}));
  const s = new JoinSession({ inputs: INPUTS, openChannel: async () => ch });
  const r = await s.connect();
  assert.equal(r.state, 'org');
  assert.equal(r.brand.orgName, 'Anchore');
  assert.equal(r.brand.orgIcon.startsWith('data:image/'), true);
  assert.equal(r.grantedRole, 'member');
  assert.equal(s.ready(), true);
  assert.deepEqual(ch.sent[0], { v: 1, op: 'context' });
  assert.equal(ch.sent.length, 1);
  assert.deepEqual(s.context, {
    transport: s.transport, orgSlug: ORG, genesisId: GENESIS,
    heads: HEADS, maxHlc: MAXHLC, grantedRole: 'member',
    inviteExpiry: [99999, 0], binding: null, approvalPolicy: null,
    presentation: {
      orgName: 'Anchore', orgDescription: 'container security',
      orgColor: null, orgIcon: 'data:image/png;base64,AAAA',
      sponsorName: null, sponsorByline: null, sponsorAvatar: null,
      sponsorPub: null,
    },
  });
}

// 1b. Sponsor presentation maps through connect(): byline retained, avatar
// accepted only on the bounded image allowlist, sponsorPub kept as secondary
// provenance. (r7kk4)
{
  const ch = new ScriptedChannel((req) => (req.op === 'context'
    ? okContext({
        sponsor_pub: 'a'.repeat(64), sponsor_name: 'Ada',
        sponsor_byline: 'founder',
        sponsor_avatar: 'data:image/webp;base64,QQ==',
      })
    : {}));
  const s = new JoinSession({ inputs: INPUTS, openChannel: async () => ch });
  await s.connect();
  assert.deepEqual(s.context.presentation, {
    orgName: 'Anchore', orgDescription: 'container security',
    orgColor: null, orgIcon: 'data:image/png;base64,AAAA',
    sponsorName: 'Ada', sponsorByline: 'founder',
    sponsorAvatar: 'data:image/webp;base64,QQ==',
    sponsorPub: 'a'.repeat(64),
  });
}

// 1c. A sponsor_avatar that is not a bounded jpeg/png/webp data: URI is
// DROPPED (never rendered): remote URLs, non-image data: URIs, non-strings.
for (const bad of [
  'https://cdn.example/a.png',
  '/uploads/a.png',
  'data:image/gif;base64,QQ==',
  'data:text/html;base64,QQ==',
  12345,
]) {
  const ch = new ScriptedChannel((req) => (req.op === 'context'
    ? okContext({ sponsor_pub: 'a'.repeat(64), sponsor_avatar: bad })
    : {}));
  const s = new JoinSession({ inputs: INPUTS, openChannel: async () => ch });
  await s.connect();
  assert.equal(s.context.presentation.sponsorAvatar, null);
  // sponsorPub survives as secondary provenance even when the avatar is dropped.
  assert.equal(s.context.presentation.sponsorPub, 'a'.repeat(64));
}

// 1d. Each allowed avatar MIME is accepted verbatim.
for (const good of [
  'data:image/jpeg;base64,QQ==',
  'data:image/png;base64,QQ==',
  'data:image/webp;base64,QQ==',
]) {
  const ch = new ScriptedChannel((req) => (req.op === 'context'
    ? okContext({ sponsor_avatar: good })
    : {}));
  const s = new JoinSession({ inputs: INPUTS, openChannel: async () => ch });
  await s.connect();
  assert.equal(s.context.presentation.sponsorAvatar, good);
}

// Authentication failure is security truth and sends no encrypted operation.
{
  let channelUsed = false;
  const error = new Error('bad transcript signature');
  error.autonetKind = 'security';
  const s = new JoinSession({
    inputs: INPUTS,
    openChannel: async () => { channelUsed = true; throw error; },
    runCeremony: async () => { throw new Error('must not run'); },
  });
  const r = await s.connect();
  assert.equal(channelUsed, true);
  assert.deepEqual(r, { state: 'security', reason: 'link-authentication-failed' });
  assert.equal(s.ready(), false);
  await assert.rejects(s.accept(), /before a successful connect/);
}

// 2. connect -> 'closed' (LEDGER truth) on a gone invite.
{
  const ch = new ScriptedChannel(() => ({ v: 1, status: 'gone', reason: 'invite-already-claimed' }));
  const s = new JoinSession({ inputs: INPUTS, openChannel: async () => ch });
  const r = await s.connect();
  assert.equal(r.state, 'closed');
  assert.equal(r.reason, 'invite-already-claimed');
  assert.equal(s.ready(), false);
}

// 3. connect -> 'link-lost' (LINK truth) when the org is unreachable.
{
  const s = new JoinSession({
    inputs: INPUTS,
    openChannel: async () => { throw new Error('no route to org'); },
  });
  const r = await s.connect();
  assert.equal(r.state, 'link-lost');
  assert.equal(r.reason, 'org-unreachable');
}

// 4. connect -> 'link-lost' when the channel returns a non-{v:1} refusal.
{
  const ch = new ScriptedChannel(() => 'REFUSED');
  const s = new JoinSession({ inputs: INPUTS, openChannel: async () => ch });
  const r = await s.connect();
  assert.equal(r.state, 'link-lost');
  assert.equal(r.reason, 'link-failed');
}

// 5. A remote-URL org_icon is dropped, never rendered (r7kk4 defence).
{
  const ch = new ScriptedChannel(() => okContext({ org_icon: 'https://evil.example/pixel.png' }));
  const s = new JoinSession({ inputs: INPUTS, openChannel: async () => ch });
  const r = await s.connect();
  assert.equal(r.state, 'org');
  assert.equal(r.brand.orgIcon, null);
}

// 6. accept() with no ceremony seam refuses (HELD) rather than pretending.
{
  const ch = new ScriptedChannel(() => okContext());
  const s = new JoinSession({ inputs: INPUTS, openChannel: async () => ch });
  await s.connect();
  await assert.rejects(s.accept('correct horse'), /not enabled/);
}

// 7. Full ladder: connect -> accept (submit pending) -> poll -> admitted.
{
  const ch = new ScriptedChannel((req, nth) => {
    if (req.op === 'context') return okContext();
    if (req.op === 'submit') return { v: 1, status: 'pending', approvals: { have: 0, need: 2 } };
    return nth < 2
      ? { v: 1, status: 'pending', approvals: { have: 1, need: 2 } }
      : { v: 1, status: 'admitted' };
  });
  const s = new JoinSession({ inputs: INPUTS, openChannel: async () => ch, runCeremony: stubCeremony });
  assert.equal((await s.connect()).state, 'org');
  const accepted = await s.accept('correct horse');
  assert.equal(accepted.state, 'pending');
  assert.equal(ch.sent.some((m) => m.op === 'submit' && typeof m.event === 'string'), true);
  const progress = [];
  const terminal = await s.pollUntilTerminal({
    delay: () => Promise.resolve(),
    onProgress: (p) => progress.push(p),
  });
  assert.equal(terminal.state, 'admitted');
  assert.equal(progress.length >= 1, true); // saw at least one pending tick
  // finding b: the invitee's own KEM private key is captured for the page to
  // persist locally on admission, but is NEVER put on the wire.
  assert.deepEqual(s.kemPrivateKey, { secret: 'per-org-kem-private' });
  assert.equal(
    JSON.stringify(ch.sent).includes('per-org-kem-private'),
    false,
    'kemPrivateKey must never be transmitted over the channel',
  );
  // the submitted event's parents matched the context heads (claim.js enforces)
  const submit = ch.sent.find((m) => m.op === 'submit');
  assert.deepEqual(JSON.parse(submit.event).parents, HEADS);
  // status op carried persona_pub, never invite_ref
  const status = ch.sent.find((m) => m.op === 'status');
  assert.equal(status.persona_pub, PERSONA);
  assert.equal(status.invite_ref, undefined);
}

// 8. accept() -> 'closed' (LEDGER truth) when submit is rejected.
{
  const ch = new ScriptedChannel((req) => {
    if (req.op === 'context') return okContext();
    return { v: 1, status: 'rejected', reason: 'invite-not-in-ancestry' };
  });
  const s = new JoinSession({ inputs: INPUTS, openChannel: async () => ch, runCeremony: stubCeremony });
  await s.connect();
  const r = await s.accept('correct horse');
  assert.equal(r.state, 'closed');
  assert.equal(r.ledgerStatus, 'rejected');
  assert.equal(r.reason, 'invite-not-in-ancestry');
}

// 9. accept() -> 'link-lost' (LINK truth) when submit throws mid-flight.
{
  const ch = new ScriptedChannel((req) => {
    if (req.op === 'context') return okContext();
    throw new Error('channel dropped');
  });
  const s = new JoinSession({ inputs: INPUTS, openChannel: async () => ch, runCeremony: stubCeremony });
  await s.connect();
  const r = await s.accept('correct horse');
  assert.equal(r.state, 'link-lost');
  assert.equal(r.reason, 'submit-failed');
}

console.log('accept-controller: all assertions passed');

// bootstrap() pages the ledger: it asks again with `after` while the reply
// says there is more, and returns the concatenated event list.
{
  const session = new JoinSession({ inputs: INPUTS });
  session.context = {};
  session.personaPub = PERSONA;
  const channel = new ScriptedChannel((req) => {
    assert.equal(req.op, 'bootstrap');
    if (req.after === 0) return { v: 1, status: 'ok', events: ['e0', 'e1'], more: true, binding: { org_uuid: ORG } };
    if (req.after === 2) return { v: 1, status: 'ok', events: ['e2'], more: false, binding: { org_uuid: ORG } };
    throw new Error(`unexpected page after=${req.after}`);
  });
  channel.calls.bootstrap = 0;
  session.channel = channel;
  const material = await session.bootstrap();
  assert.deepEqual(material.events, ['e0', 'e1', 'e2']);
  assert.equal(material.binding.org_uuid, ORG);
  assert.deepEqual(channel.sent.map((m) => m.after), [0, 2]);
}
