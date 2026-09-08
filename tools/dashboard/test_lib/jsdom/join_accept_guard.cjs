// JoinSession must never replace a staged claim that already carries
// approvals (auto-ixd9m). Admission is a SECOND submit: countersigning only
// marks a claim ready, and the joiner re-mints at the pinned position
// carrying the signatures. Calling accept() to do that discarded them — it
// cost a real operator approval during the live join on 2026-09-08.
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');

const ROOT = resolve(__dirname, '../../static/js');

// The controller imports three modules; stub them so the guard logic itself
// is what is under test, not the network or the crypto.
function loadController({ status, submitted }) {
  const src = readFileSync(resolve(ROOT, 'join/accept-controller.js'), 'utf8');
  const calls = { submits: [], mints: [] };
  const shim = src
    .replace(/^import[^;]+;$/gm, '')
    .replace(/^export class/m, 'class')
    + '\nreturn { JoinSession, calls };';
  const factory = new Function(
    'sendOp', 'makeChannelTransport', 'submitClaim', 'getClaimStatus', 'calls',
    shim.replace('return { JoinSession, calls };', 'return { JoinSession };'),
  );
  const submitClaim = async ({ event }) => {
    calls.submits.push(event);
    return submitted;
  };
  const getClaimStatus = async () => status;
  const { JoinSession } = factory(
    async () => ({ status: 'ok' }), () => ({}), submitClaim, getClaimStatus, calls,
  );
  return { JoinSession, calls };
}

function session(JoinSession, calls, { approvals = [], position = null } = {}) {
  const s = new JoinSession({
    inputs: { inviteRef: 'a'.repeat(64), bearer: 'b'.repeat(64), org: 'testorg' },
    openChannel: async () => ({}),
    runCeremony: async (args) => {
      calls.mints.push(args);
      return {
        event: JSON.stringify({
          payload: { approvals: args.approvals || [], position: args.position || null },
        }),
        personaPub: 'c'.repeat(64),
        claimKey: 'd'.repeat(64),
      };
    },
  });
  // Stand in for a completed connect(): the guard reads staged state, not
  // the channel.
  s.context = { orgSlug: 'testorg', heads: [], maxHlc: [1, 0] };
  s.claimKey = 'd'.repeat(64);
  s.personaPub = 'c'.repeat(64);
  return s;
}

async function main() {
  // 1. THE REGRESSION. A staged claim carrying the operator's approval must
  //    not be submitted over. Before the fix this minted and submitted a
  //    fresh empty-approvals claim, silently discarding the signature.
  {
    const staged = {
      status: 'pending', approvals: [{ key: 'e'.repeat(64), sig: 'f'.repeat(128) }],
      have: 1, need: 1, position: { parents: ['a'.repeat(64)], hlc: [1, 0] },
    };
    const { JoinSession, calls } = loadController({ status: staged, submitted: { status: 'pending' } });
    const s = session(JoinSession, calls);
    const result = await s.accept('pw');
    assert.equal(result.state, 'already-approved', 'accept() must refuse over an approved claim');
    assert.equal(calls.submits.length, 0, 'nothing may be submitted');
    assert.equal(calls.mints.length, 0, 'nothing may even be minted');
  }

  // 2. accept() still works normally when nothing is staged yet.
  {
    const staged = { status: 'pending', approvals: [], have: 0, need: 1, position: null };
    const { JoinSession, calls } = loadController({ status: staged, submitted: { status: 'pending' } });
    const s = session(JoinSession, calls);
    const result = await s.accept('pw');
    assert.equal(result.state, 'pending');
    assert.equal(calls.submits.length, 1, 'a first accept still submits');
  }

  // 3. finalize() carries the approvals AND the pinned position.
  {
    const position = { parents: ['a'.repeat(64)], hlc: [7, 0] };
    const approvals = [{ key: 'e'.repeat(64), sig: 'f'.repeat(128) }];
    const staged = { status: 'pending', approvals, have: 1, need: 1, position };
    const { JoinSession, calls } = loadController({ status: staged, submitted: { status: 'admitted' } });
    const s = session(JoinSession, calls);
    const result = await s.finalize('pw');
    assert.equal(result.state, 'admitted');
    assert.equal(calls.mints.length, 1);
    assert.deepEqual(calls.mints[0].approvals, approvals, 'the mint must carry the approvals');
    assert.deepEqual(calls.mints[0].position, position, 'the mint must carry the pinned position');
  }

  // 4. finalize() refuses when there is nothing to carry, rather than
  //    submitting an empty claim over the staged row.
  {
    const staged = { status: 'pending', approvals: [], have: 0, need: 1, position: null };
    const { JoinSession, calls } = loadController({ status: staged, submitted: { status: 'pending' } });
    const s = session(JoinSession, calls);
    const result = await s.finalize('pw');
    assert.equal(result.state, 'pending');
    assert.match(result.reason, /no approvals staged/);
    assert.equal(calls.submits.length, 0);
  }

  // 5. finalize() refuses if the minted event dropped the approvals — the
  //    verification that stops a faulty ceremony from destroying the row.
  {
    const approvals = [{ key: 'e'.repeat(64), sig: 'f'.repeat(128) }];
    const staged = {
      status: 'pending', approvals, have: 1, need: 1,
      position: { parents: ['a'.repeat(64)], hlc: [1, 0] },
    };
    const { JoinSession, calls } = loadController({ status: staged, submitted: { status: 'admitted' } });
    const s = session(JoinSession, calls);
    s.runCeremony = async () => ({
      event: JSON.stringify({ payload: { approvals: [] } }),
      personaPub: 'c'.repeat(64), claimKey: 'd'.repeat(64),
    });
    const result = await s.finalize('pw');
    assert.match(result.reason, /did not carry the approvals/);
    assert.equal(calls.submits.length, 0, 'an unverified claim is never submitted');
  }

  // 6. An already-admitted claim finalizes idempotently.
  {
    const staged = { status: 'admitted', approvals: [], have: 1, need: 1, position: null };
    const { JoinSession, calls } = loadController({ status: staged, submitted: { status: 'admitted' } });
    const s = session(JoinSession, calls);
    assert.equal((await s.finalize('pw')).state, 'admitted');
    assert.equal(calls.submits.length, 0);
  }

  console.log('PASS join accept guard');
  process.exit(0);
}

main().catch((error) => { console.error(error); process.exit(1); });
