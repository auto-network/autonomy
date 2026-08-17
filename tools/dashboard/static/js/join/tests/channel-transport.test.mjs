/* The org:join transport layer, proven against a stub SecureChannel with the
 * REAL (unchanged) ceremony/claim.js. No relaykit/core, no relay, no ceremony
 * — just the wire: claim.js's HTTP-shaped calls reframed as sendOp operations,
 * and the ledger-truth / link-truth split kept honest.
 *
 * Run: node channel-transport.test.mjs
 */
import assert from 'node:assert/strict';

import { canonicalJson, sendOp } from '../../lib/relaykit-core.js';
import { makeChannelTransport } from '../channel-transport.js';
import {
  getClaimContext,
  submitClaim,
  getClaimStatus,
} from '../../ceremony/claim.js';

const te = new TextEncoder();
const td = new TextDecoder();

const GENESIS = 'a'.repeat(64);
const HEADS = ['1'.repeat(64), '2'.repeat(64)]; // sorted, unique, 64-hex
const MAXHLC = [12345, 0];
const ORG = '11111111-1111-4111-8111-111111111111';
const INVITE = 'e'.repeat(64);
const PERSONA = 'f'.repeat(64);

// A SecureChannel-shaped stub. Each sendMessage records the parsed request;
// recvMessage answers with replyFor(lastRequest). replyFor may return an
// object (encoded canonical-ish JSON + '\n'), a raw string (a non-{v:1}
// link-level refusal), or throw (an offline channel).
class StubChannel {
  constructor(replyFor) {
    this.replyFor = replyFor;
    this.sent = [];
  }

  async sendMessage(bytes) {
    const text = td.decode(bytes);
    assert.equal(text.endsWith('\n'), true, 'request must be newline-terminated');
    this.sent.push(JSON.parse(text.slice(0, -1)));
  }

  async recvMessage() {
    const reply = this.replyFor(this.last); // may throw (offline)
    if (typeof reply === 'string') return te.encode(reply);
    return te.encode(`${canonicalJson(reply)}\n`);
  }

  get last() {
    return this.sent[this.sent.length - 1];
  }
}

// 1. sendOp round-trips a {v:1,op} request and returns the parsed reply; the
//    op:context reply carries the r7kk4 brand the controller lights up, and no
//    invite_ref ever rides the wire (the grant is scoped to one invitation).
{
  const ch = new StubChannel(() => ({
    v: 1,
    status: 'ok',
    genesis_id: GENESIS,
    heads: HEADS,
    max_hlc: MAXHLC,
    org_name: 'Anchore',
    org_description: 'container security',
    org_icon: 'data:image/png;base64,AAAA',
  }));
  const reply = await sendOp(ch, { v: 1, op: 'context' });
  assert.deepEqual(ch.last, { v: 1, op: 'context' });
  assert.equal(reply.org_name, 'Anchore');
  assert.equal(reply.org_icon.startsWith('data:image/'), true);
}

// 2. claim.js getClaimContext, unchanged, driven over the channel transport.
{
  const ch = new StubChannel(() => ({
    v: 1,
    status: 'ok',
    genesis_id: GENESIS,
    heads: HEADS,
    max_hlc: MAXHLC,
    granted_role: 'member',
    binding: 'bearer',
    invite_expiry: [99999, 0],
  }));
  const transport = makeChannelTransport(ch);
  const ctx = await getClaimContext({ transport, orgSlug: ORG, inviteRef: INVITE });
  assert.deepEqual(ch.last, { v: 1, op: 'context' });
  assert.equal(ctx.genesisId, GENESIS);
  assert.deepEqual(ctx.heads, HEADS);
  assert.deepEqual(ctx.maxHlc, MAXHLC);
}

// 3. submitClaim reframes to op:submit and carries the signed event wire.
{
  const ch = new StubChannel(() => ({ v: 1, status: 'ok', accepted: true }));
  const transport = makeChannelTransport(ch);
  const context = {
    transport, orgSlug: ORG, genesisId: GENESIS, heads: HEADS, maxHlc: MAXHLC,
  };
  const event = { parents: HEADS, kind: 'member.claim', payload: { invite_ref: INVITE } };
  const result = await submitClaim({ context, event });
  assert.equal(ch.last.op, 'submit');
  assert.equal(typeof ch.last.event, 'string');
  assert.equal(JSON.parse(ch.last.event).payload.invite_ref, INVITE);
  assert.equal(result.status, 'ok');
}

// 4. getClaimStatus reframes to op:status with persona_pub (never invite_ref);
//    LEDGER truth (gone) is delivered to the caller, not thrown.
{
  const ch = new StubChannel(() => ({ v: 1, status: 'gone', reason: 'invite-expired' }));
  const transport = makeChannelTransport(ch);
  const context = {
    transport, orgSlug: ORG, genesisId: GENESIS, heads: HEADS, maxHlc: MAXHLC,
  };
  const status = await getClaimStatus({
    context, claimKey: 'c'.repeat(64), inviteRef: INVITE, personaPub: PERSONA,
  });
  assert.equal(ch.last.op, 'status');
  assert.equal(ch.last.persona_pub, PERSONA);
  assert.equal(ch.last.invite_ref, undefined);
  assert.equal(status.status, 'gone');
}

// 5. LINK truth: a non-{v:1} refusal makes the same call REJECT, not resolve —
//    the caller's catch sees a link failure, never a ledger verdict.
{
  const ch = new StubChannel(() => 'REFUSED');
  const transport = makeChannelTransport(ch);
  const context = {
    transport, orgSlug: ORG, genesisId: GENESIS, heads: HEADS, maxHlc: MAXHLC,
  };
  await assert.rejects(
    getClaimStatus({
      context, claimKey: 'c'.repeat(64), inviteRef: INVITE, personaPub: PERSONA,
    }),
    /not valid JSON|malformed|reply must be/,
  );
}

// 6. LINK truth: an offline channel (recvMessage throws) rejects too.
{
  const ch = new StubChannel(() => {
    throw new Error('channel offline');
  });
  const transport = makeChannelTransport(ch);
  await assert.rejects(
    getClaimContext({ transport, orgSlug: ORG, inviteRef: INVITE }),
    /offline/,
  );
}

console.log('channel-transport: all assertions passed');
