/* The org:join controller, driven end to end against a scripted stub channel
 * and a stub ceremony seam. No relaykit/core, no relay, no real ceremony — the
 * whole ladder's SHAPE is exercised, so the seams drop in later with no
 * surprises. Run: node accept-controller.test.mjs
 */
import assert from 'node:assert/strict';

import { JoinSession } from '../accept-controller.js';

const te = new TextEncoder();
const td = new TextDecoder();

const ORG = '11111111-1111-4111-8111-111111111111';
const ROOT = 'a'.repeat(64);
const GENESIS = 'b'.repeat(64);
const HEADS = ['1'.repeat(64), '2'.repeat(64)];
const MAXHLC = [12345, 0];
const INVITE = 'e'.repeat(64);
const PERSONA = 'f'.repeat(64);
const CLAIMKEY = 'c'.repeat(64);

const INPUTS = {
  org: ORG, rootPub: ROOT, inviteRef: INVITE, channelToken: 'd'.repeat(64), bearer: 'tok',
};

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
    return te.encode(`${JSON.stringify(reply)}\n`);
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
