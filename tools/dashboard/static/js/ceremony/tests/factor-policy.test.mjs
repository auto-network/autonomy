/* Node tests for factor-policy.js — the browser authority model.
 *
 * These assert the same properties tools/network/idkit/TLA/FactorAuth.tla
 * proves (rootReachable always holds, no dead taps, MFA is the pair), on the
 * models buildModel() produces from real /status + parsed armor. Run:
 *   node --test tools/dashboard/static/js/ceremony/tests/factor-policy.test.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  rootReachable, invalid, level, actionOn, hasAlternative, applyAuthority,
  buildModel,
} from '../factor-policy.js';

// Helpers to fabricate /status + armorData the way the real endpoints shape them.
function status(passkeys, rpId = 'localhost') {
  return { rp_id: rpId, passkeys: passkeys || [] };
}
function armor(factors) { return { factors }; }
function pk(cred, { prf = true, rp = 'localhost' } = {}) {
  return {
    credential_id: cred, label: cred, rp_id: rp,
    provisioning_public_key: prf ? ('ab'.repeat(32)) : null,
  };
}

test('password-only: reachable, full, and the sole opener cannot be removed', () => {
  const m = buildModel(status([]), armor([{ type: 'password' }]));
  assert.equal(rootReachable(m), true);
  assert.equal(level(m, 'pass'), 'b');
  assert.equal(invalid(m), null);
  // no other opener exists → removing/changing the only factor is not offered
  assert.equal(actionOn(m, 'pass'), null);
  assert.equal(actionOn(m, 'both'), null);   // MFA needs a passkey too
});

test('passkey-only (root factor): reachable via the passkey', () => {
  const m = buildModel(status([pk('ip')]), armor([
    { type: 'passkey', credential_id: 'ip' },
  ]));
  assert.equal(rootReachable(m), true);
  assert.equal(level(m, 'face'), 'b');       // full authority
  assert.equal(actionOn(m, 'pass'), 'enroll');
});

test('password + access passkey: passkey is unlock-only and promotable', () => {
  const m = buildModel(status([pk('ip')]), armor([{ type: 'password' }]));
  assert.equal(level(m, 'face'), 'a');       // unlock only
  assert.equal(level(m, 'pass'), 'b');
  assert.equal(actionOn(m, 'face'), 'upgrade');   // promote is offered
  assert.equal(actionOn(m, 'both'), 'enable');    // MFA offerable (have both, prf key)
  assert.equal(rootReachable(m), true);
});

test('non-prf passkey cannot be promoted and cannot pair', () => {
  const m = buildModel(status([pk('ip', { prf: false })]),
    armor([{ type: 'password' }]));
  assert.equal(actionOn(m, 'both'), null);        // no prf key → no MFA
  // forcing the non-prf key to full authority is an invalid state the guard names
  const n = JSON.parse(JSON.stringify(m));
  n.keys[0].auth = 'full';
  assert.equal(invalid(n), 'A passkey that cannot derive cannot hold full authority.');
});

test('MFA on: the pair reaches root, singles offer no standalone action', () => {
  const m = buildModel(
    status([pk('ip')]),
    armor([{ type: 'combined', credential_id: 'ip', kem_pub: 'ab'.repeat(32) }]),
  );
  assert.equal(m.mfa, true);
  assert.equal(rootReachable(m), true);           // capable passkey + password half
  assert.equal(level(m, 'both'), 'b');
  assert.equal(actionOn(m, 'face'), null);        // a single cannot be raised alone
  assert.equal(actionOn(m, 'pass'), null);
  assert.equal(invalid(m), null);
});

test('every reachable authority choice from a rich state stays valid', () => {
  // password + a promotable passkey: exhaustively apply every authority combo
  // and assert none yields an unopenable state that the model would accept.
  const m = buildModel(status([pk('ip')]), armor([{ type: 'password' }]));
  for (let f = 0; f < 2; f += 1) {
    for (let p = 0; p < 2; p += 1) {
      for (let b = 0; b < 2; b += 1) {
        const n = applyAuthority(JSON.parse(JSON.stringify(m)), f, p, b);
        // applyAuthority may produce an illegal candidate; the model's job is
        // that invalid() FLAGS it (commit refuses it). What must never happen:
        // a candidate that invalid() passes yet leaves the root unreachable.
        if (invalid(n) === null) assert.equal(rootReachable(n), true);
      }
    }
  }
});

test('MFA requires both halves — losing the password would be refused', () => {
  const m = buildModel(
    status([pk('ip')]),
    armor([{ type: 'combined', credential_id: 'ip', kem_pub: 'ab'.repeat(32) }]),
  );
  const n = JSON.parse(JSON.stringify(m));
  n.pass.on = false;                              // simulate dropping the password
  assert.equal(rootReachable(n), false);
  assert.equal(invalid(n), 'Nothing would be able to reach your root.');
});
