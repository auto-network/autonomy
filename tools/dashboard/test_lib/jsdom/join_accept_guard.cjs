// JoinSession.accept(): the invitee signs its claim ONCE and submits it.
//
// History (auto-ixd9m): a second, joiner-side submit to carry approvals once
// discarded a real operator approval (2026-09-08), and this script pinned the
// client guard against it. Since 8038f999 (auto-qrmlg.3, rule P3) the approver
// admits the staged claim with a member.admission event carrying it unchanged,
// so the joiner never submits again (finalize() is gone), and the guard that
// keeps a staged row's approvals is the ledger store's
// (store.stage_pending_claim; tools/network/ledger/tests/test_member_claim_flow.py).
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
  // A first accept mints once and submits once; the ledger's reply decides.
  {
    const staged = { status: 'pending', approvals: [], have: 0, need: 1, position: null };
    const { JoinSession, calls } = loadController({ status: staged, submitted: { status: 'pending' } });
    const s = session(JoinSession, calls);
    const result = await s.accept('pw');
    assert.equal(result.state, 'pending');
    assert.equal(calls.mints.length, 1, 'the claim is signed once');
    assert.equal(calls.submits.length, 1, 'and submitted once');
  }

  // The controller has no second submit path to carry approvals.
  {
    const { JoinSession } = loadController({ status: null, submitted: null });
    assert.equal(typeof JoinSession.prototype.finalize, 'undefined',
      'the joiner never re-submits; the approver admits the staged claim');
  }

  console.log('PASS join accept guard');
  process.exit(0);
}

main().catch((error) => { console.error(error); process.exit(1); });
