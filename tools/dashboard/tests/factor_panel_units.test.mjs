/* Unit coverage for the factor panel's two pure seams:
 *
 *   buildModelV3        server v3 factor-policy view → the design's model shape
 *   stagedOperations    baseline diff → the frozen factor-policy op union
 *   desiredPolicy       model state → the root-policy tree it means
 *
 * Pure logic only — the jsdom suite drives the same functions through the real
 * rendered panel.
 *
 *   node --test tools/dashboard/tests/factor_panel_units.test.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  buildModelV3, stagedOperations, desiredPolicy, factorAuthority, credentialsPanel,
  satisfyingSets, describeStagedChanges,
} from '../static/js/factor-management.js';

const HEXA = 'a'.repeat(64);
const HEXB = 'b'.repeat(64);
const HEXC = 'c'.repeat(64);

function pwFactorView(id, extra = {}) {
  return {
    factor_id: id, type: 'password', label: 'Password', access: 'enabled',
    root_role: 'individual', kdf: { name: 'PBKDF2', hash: 'SHA-256', iterations: 600000 },
    ...extra,
  };
}
function pkFactorView(id, cred, recipients, extra = {}) {
  return {
    factor_id: id, type: 'passkey', label: 'Passkey', access: 'enabled',
    root_role: 'individual', credential_id: cred, transports: ['internal'],
    backed_up: false, created_at: '2026-08-01T00:00:00Z', recipients,
    ...extra,
  };
}
const recipA = { recipient_public_key: HEXB, label: 'This Mac', created_at: '2026-08-01T00:00:00Z' };
const recipB = { recipient_public_key: HEXC, label: 'iPhone', created_at: '2026-08-02T00:00:00Z' };

function orView() {
  return {
    generation: 3, root_pub: HEXA,
    root_policy: { op: 'or', children: [{ op: 'factor', factor_id: 'pw.1' }, { op: 'factor', factor_id: 'pk.1' }] },
    factors: [pwFactorView('pw.1'), pkFactorView('pk.1', 'credA', [recipA])],
  };
}
function panelFrom(view) {
  const c = credentialsPanel();
  Object.assign(c, buildModelV3(view, { rp_id: 'localhost' }));
  c._committedPolicy = view.root_policy;
  c.initBaseline();
  return c;
}

test('factorAuthority folds the server roles', () => {
  assert.equal(factorAuthority('individual', 'enabled'), 'full');
  assert.equal(factorAuthority('none', 'enabled'), 'unlock');
  assert.equal(factorAuthority('none', 'disabled'), 'none');
  assert.equal(factorAuthority('mfa-member', 'enabled'), 'unlock');
});

test('buildModelV3: one slot row per passkey recipient', () => {
  const view = orView();
  view.factors[1].recipients = [recipA, recipB];
  const m = buildModelV3(view, { rp_id: 'localhost' });
  const rows = m.passkeys.filter((k) => k.factorId === 'pk.1');
  assert.equal(rows.length, 2);
  assert.deepEqual(rows.map((r) => r.device).sort(), ['This Mac', 'iPhone']);
  assert.ok(rows.every((r) => r.credId === 'credA'));
});

test('buildModelV3: a recipient-less factor renders one unpaired row without authority', () => {
  const view = orView();
  view.factors.push(pkFactorView('pk.2', 'credB', [], { root_role: 'none' }));
  const m = buildModelV3(view, { rp_id: 'localhost' });
  const row = m.passkeys.find((k) => k.factorId === 'pk.2');
  assert.ok(row.unpaired);
  assert.notEqual(row.authority, 'full');
});

test('buildModelV3: mfaMode reads the TREE, not role presence', () => {
  // AND over both full classes → 'any'
  const anyView = {
    generation: 4, root_pub: HEXA,
    root_policy: { op: 'and', children: [{ op: 'factor', factor_id: 'pw.1' }, { op: 'factor', factor_id: 'pk.1' }] },
    factors: [
      pwFactorView('pw.1', { root_role: 'mfa-member' }),
      pkFactorView('pk.1', 'credA', [recipA], { root_role: 'mfa-member' }),
    ],
  };
  assert.equal(buildModelV3(anyView, {}).mfaMode, 'any');
  // same AND shape but a second password left OUT of the tree → 'specific'
  const specView = {
    ...anyView,
    factors: [...anyView.factors, pwFactorView('pw.2', { root_role: 'none' })],
  };
  const m = buildModelV3(specView, {});
  assert.equal(m.mfaOn, true);
  assert.equal(m.mfaMode, 'specific');
});

test('demote to unlock-only stages exactly one policy shrink', () => {
  const c = panelFrom(orView());
  c.passkeys[0].authority = 'unlock';
  const ops = stagedOperations(c);
  assert.deepEqual(ops.map((o) => o.op), ['set_root_policy']);
  assert.deepEqual(ops[0].policy, { op: 'factor', factor_id: 'pw.1' });
});

test('removing the only slot removes the factor and shrinks the policy', () => {
  const c = panelFrom(orView());
  c.remove(c.passkeys, c.passkeys[0], null);
  assert.equal(c.passkeys[0].pending, 'removed');
  const ops = stagedOperations(c);
  assert.deepEqual(ops.map((o) => o.op), ['remove_factor', 'set_root_policy']);
});

test('removing one of two slots stages remove_passkey_recipient, not remove_factor', () => {
  const view = orView();
  view.factors[1].recipients = [recipA, recipB];
  const c = panelFrom(view);
  const slot = c.passkeys.find((k) => k.device === 'iPhone');
  c.remove(c.passkeys, slot, null);
  const ops = stagedOperations(c);
  assert.deepEqual(ops.map((o) => o.op), ['remove_passkey_recipient']);
  assert.equal(ops[0].recipient_public_key, HEXC);
});

test('the solver refuses removing the last full-authority factor', () => {
  const c = panelFrom(orView());
  c.passkeys[0].authority = 'unlock';
  assert.equal(c.canRemove(c.passwords[0]), false);
  c.remove(c.passwords, c.passwords[0], null);
  assert.notEqual(c.passwords[0].pending, 'removed');   // refused, not staged
});

test('unpaired factors cannot be granted authority', () => {
  const view = orView();
  view.factors.push(pkFactorView('pk.2', 'credB', [], { root_role: 'none' }));
  const c = panelFrom(view);
  const row = c.passkeys.find((k) => k.unpaired);
  c.toggleAuthority(row, null);
  assert.notEqual(row.authority, 'full');
});

test('a changed password stages change_password with its derived factor', () => {
  const c = panelFrom(orView());
  c.passwords[0].pwChanged = true;
  c.passwords[0]._factor = { factor_id: 'pw.1' };   // stand-in; commit path derives the real one
  const ops = stagedOperations(c);
  assert.deepEqual(ops.map((o) => o.op), ['change_password']);
  assert.equal(ops[0].factor_id, 'pw.1');
});

test('toggling sign-in off stages set_access disabled', () => {
  const c = panelFrom(orView());
  c.passkeys[0].authority = 'none';   // full → none: leaves policy AND disables access
  const ops = stagedOperations(c);
  assert.deepEqual(ops.map((o) => o.op).sort(), ['set_access', 'set_root_policy']);
  const acc = ops.find((o) => o.op === 'set_access');
  assert.deepEqual(acc, { op: 'set_access', factor_id: 'pk.1', enabled: false });
});

test('enabling any-mode MFA stages the AND-of-class-ORs tree', () => {
  const c = panelFrom(orView());
  c.pickMode = 'any';
  c.enableMfa();
  const ops = stagedOperations(c);
  const pol = ops.find((o) => o.op === 'set_root_policy');
  assert.ok(pol, 'policy op staged');
  assert.equal(pol.policy.op, 'and');
  const leaves = pol.policy.children.map((n) => n.factor_id).sort();
  assert.deepEqual(leaves, ['pk.1', 'pw.1']);
});

test('desiredPolicy refuses an empty root', () => {
  const c = panelFrom(orView());
  c.passwords[0].authority = 'unlock';
  c.passkeys[0].authority = 'unlock';
  assert.throws(() => desiredPolicy(c), /full authority/);
});

test('MFA cannot be enabled around a slotless class member', () => {
  const view = orView();
  view.factors[1].recipients = [];   // the only passkey has no device slot
  const c = panelFrom(view);
  c.startMfaSetup();
  assert.equal(c.canEnableMfa, false, 'setup screen refuses');
  c.enableMfa();
  assert.equal(c.mfaOn, false, 'enable is a no-op');
  assert.equal(c.changeCount, 0, 'nothing staged');
});

test('satisfyingSets: every policy shape the armor can hold', () => {
  const F = (id) => ({ op: 'factor', factor_id: id });
  // one password
  assert.deepEqual(satisfyingSets(F('pw.1')), [['pw.1']]);
  // any one of N passwords
  assert.deepEqual(satisfyingSets({ op: 'or', children: [F('pw.1'), F('pw.2')] }),
    [['pw.1'], ['pw.2']]);
  // either a password or a passkey
  assert.deepEqual(satisfyingSets({ op: 'or', children: [F('pw.1'), F('pk.1')] }),
    [['pk.1'], ['pw.1']]);
  // one of each (MFA any): AND of the class ORs
  assert.deepEqual(satisfyingSets({ op: 'and', children: [
    { op: 'or', children: [F('pw.1'), F('pw.2')] },
    { op: 'or', children: [F('pk.1'), F('pk.2')] },
  ] }).length, 4);
  // specific ones of each
  assert.deepEqual(satisfyingSets({ op: 'and', children: [F('pw.2'), F('pk.1')] }),
    [['pk.1', 'pw.2']]);
  // nested: a lone master password OR a pair
  assert.deepEqual(satisfyingSets({ op: 'or', children: [
    F('pw.master'), { op: 'and', children: [F('pw.day'), F('pk.mac')] },
  ] }).map((s) => s.join('+')).sort(), ['pk.mac+pw.day', 'pw.master']);
});

test('dead-end detection: passkey-only policy on a device with no WebAuthn', () => {
  const c = credentialsPanel();
  c.envelope = {
    policy: { op: 'factor', factor_id: 'pk.1' },
    factors: [{ factor_id: 'pk.1', type: 'passkey', credential_id: 'credA', recipients: [recipA] }],
  };
  c._viewFactors = [pkFactorView('pk.1', 'credA', [recipA], { label: 'iCloud Passkey' })];
  c.requireRoot('Commit 1 change');   // node has no window.PublicKeyCredential
  assert.equal(c.authDeadEnd, true, 'known dead end immediately');
  assert.equal(c.authShowMissing, true);
  assert.deepEqual(c.authMissingPasskeys, [{ label: 'iCloud Passkey', devices: 'This Mac' }]);
  assert.match(c.authMissingLead, /required but missing/);
});

test('dead-end phrasing: any-one of several passkeys', () => {
  const c = credentialsPanel();
  c.envelope = {
    policy: { op: 'or', children: [{ op: 'factor', factor_id: 'pk.1' }, { op: 'factor', factor_id: 'pk.2' }] },
    factors: [
      { factor_id: 'pk.1', type: 'passkey', credential_id: 'credA', recipients: [recipA] },
      { factor_id: 'pk.2', type: 'passkey', credential_id: 'credB', recipients: [recipB] },
    ],
  };
  c._viewFactors = [
    pkFactorView('pk.1', 'credA', [recipA], { label: 'Work passkey' }),
    pkFactorView('pk.2', 'credB', [recipB], { label: 'Home passkey' }),
  ];
  c.requireRoot('Commit 1 change');
  assert.equal(c.authDeadEnd, true);
  assert.match(c.authMissingLead, /At least one of the following/);
  assert.deepEqual(c.authMissingPasskeys.map((m) => m.devices), ['This Mac', 'iPhone']);
});

test('describeStagedChanges narrates the staged diff in words', () => {
  let c = panelFrom(orView());
  c.passkeys[0].authority = 'unlock';
  assert.deepEqual(describeStagedChanges(c), ['“Passkey”: Full authority → Unlock only']);

  c = panelFrom(orView());
  c.remove(c.passkeys, c.passkeys[0], null);
  assert.deepEqual(describeStagedChanges(c), ['Remove passkey “Passkey”']);

  c = panelFrom(orView());
  c.passwords[0].pwChanged = true;
  assert.deepEqual(describeStagedChanges(c), ['Change password “Password”']);

  c = panelFrom(orView());
  c.pickMode = 'any';
  c.enableMfa();
  const lines = describeStagedChanges(c);
  assert.ok(lines.some((l) => l.startsWith('Turn on multi-factor — any one password')), lines.join('|'));
});
