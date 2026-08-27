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
  satisfyingSets, describeStagedChanges, requiredSlotEnrollments,
} from '../static/js/factor-management.js';
import { policyWithFactorGranted } from '../static/js/ceremony/root-factor-policy.js';
import { applyOps } from './factor_test_helpers.mjs';

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

test('buildModelV3: a recipient-less factor renders one row at its server authority', () => {
  const view = orView();
  view.factors.push(pkFactorView('pk.2', 'credB', [], { root_role: 'none' }));
  const m = buildModelV3(view, { rp_id: 'localhost' });
  const row = m.passkeys.find((k) => k.factorId === 'pk.2');
  assert.ok(row.unpaired, 'display detail: no device column');
  assert.equal(row.authority, 'unlock', 'enrolled + sign-in = Unlock only');
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

test('slotless tap: stages Full authority; commit acquires the slot', () => {
  // the operator's post-migration state: enrolled credential, no PRF slot yet
  const view = orView();
  view.factors.push(pkFactorView('pk.2', 'credB', [], { root_role: 'none' }));
  const c = panelFrom(view);
  const row = c.passkeys.find((k) => k.factorId === 'pk.2');
  assert.equal(row.authority, 'unlock');
  // the single ladder applies to every enrolled factor: tap → Full authority
  c.authCellClick(row, null);
  assert.equal(row.authority, 'full', 'tap stages full — no invented refusal, no silent demote');
  assert.equal(c.changeCount, 1);
  // the ending state needs key material this factor lacks → commit must mint it
  const need = requiredSlotEnrollments(c);
  assert.deepEqual(need.map((r) => r.factorId), ['pk.2']);
  // simulate exactly what the commit ceremony does: stage the derived slot…
  c._stageSlotRow(need[0], 'd'.repeat(64));
  assert.deepEqual(requiredSlotEnrollments(c), [], 'material acquired');
  // …and the batch now expresses the ending state in one committable step
  const ops = stagedOperations(c);
  assert.deepEqual(ops.map((o) => o.op).sort(), ['add_passkey_recipient', 'set_root_policy']);
  const projected = applyOps({ generation: 3,
    factors: view.factors.map((f) => (f.type === 'password' ? {
      factor_id: f.factor_id, type: 'password',
      recipient_public_key: HEXB, access_public_key: HEXC,
      protector: { kdf: { name: 'PBKDF2', hash: 'SHA-256', iterations: 600000, salt: 'AAAAAAAAAAAAAAAAAAAAAA==' },
        cipher: 'AES-256-GCM', iv: 'AAAAAAAAAAAAAAAA', wrapped_seed: 'A'.repeat(64) },
    } : { factor_id: f.factor_id, type: 'passkey', credential_id: f.credential_id, recipients: f.recipients })),
    access: ['pk.1', 'pk.2', 'pw.1'],
    policy: view.root_policy }, ops);
  const leaves = JSON.stringify(projected.policy);
  assert.ok(leaves.includes('pk.2'), 'full authority landed in the committed policy');
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

test('MFA enables around a slotless member; commit acquires its slot', () => {
  const view = orView();
  view.factors[1].recipients = [];   // enrolled passkey, no device slot yet
  const c = panelFrom(view);
  c.startMfaSetup();
  assert.equal(c.canEnableMfa, true, 'an enrolled factor anchors MFA');
  c.enableMfa();
  assert.equal(c.mfaOn, true);
  assert.deepEqual(requiredSlotEnrollments(c).map((r) => r.factorId), ['pk.1']);
  c._stageSlotRow(requiredSlotEnrollments(c)[0], 'd'.repeat(64));
  const ops = stagedOperations(c);
  assert.ok(ops.some((o) => o.op === 'set_root_policy' && o.policy.op === 'and'));
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

test('narration: a placeholder replaced by its first slot is an enrollment, never a removal', () => {
  // The bug from the first live iPhone screenshot: a recipient-less passkey
  // (one "unpaired" placeholder row) promoted to authority gets its device
  // slot staged at commit; the placeholder swap must narrate as ONE enroll
  // line — no fictional "Remove device" (stagedOperations stages no remove
  // for a row with no recipientPub), and no redundant "for" clause when the
  // device name equals the credential label.
  const view = orView();
  view.factors[1].recipients = [];          // no key material on record
  view.factors[1].label = 'iPhone';
  const c = panelFrom(view);
  const placeholder = c.passkeys.find((k) => k.unpaired);
  assert.ok(placeholder, 'recipient-less factor renders one placeholder row');
  placeholder.authority = 'full';
  c.thisDevice = () => 'iPhone';            // device name == credential label
  c._stageSlotRow(placeholder, 'ab'.repeat(32));
  const lines = describeStagedChanges(c);
  assert.ok(!lines.some((l) => l.startsWith('Remove device')), lines.join('|'));
  assert.ok(lines.includes('Enroll this device (\u201ciPhone\u201d)'), lines.join('|'));
});

test('policyWithFactorGranted restores authority for every legal shape', () => {
  const F = (id) => ({ op: 'factor', factor_id: id });
  const t = (id) => (id.startsWith('pk') ? 'passkey' : 'password');
  // lone leaf → OR of both
  assert.deepEqual(
    policyWithFactorGranted(F('pw.1'), 'pk.1', t).children.map((n) => n.factor_id).sort(),
    ['pk.1', 'pw.1']);
  // OR → gains the leaf
  const or3 = policyWithFactorGranted({ op: 'or', children: [F('pw.1'), F('pk.1')] }, 'pk.2', t);
  assert.equal(or3.children.length, 3);
  // AND (MFA) → the passkey CLASS gains the leaf
  const and2 = policyWithFactorGranted({ op: 'and', children: [F('pw.1'), F('pk.1')] }, 'pk.2', t);
  assert.equal(and2.op, 'and');
  const pkClass = and2.children.find((c) => c.op === 'or');
  assert.deepEqual(pkClass.children.map((n) => n.factor_id).sort(), ['pk.1', 'pk.2']);
  // idempotent
  assert.deepEqual(policyWithFactorGranted(or3, 'pk.2', t), or3);
});

test('leaving a screen closes the camera: back() stops the scan stream and detaches the preview', () => {
  const c = panelFrom(orView());
  let stopped = 0;
  let removed = false;
  c.recoveryScanning = true;
  c._recoveryStream = { getTracks: () => [{ stop: () => { stopped += 1; } }] };
  c._recoveryVideo = { pause: () => {}, remove: () => { removed = true; } };
  c.push({ s: 'recovery-verify' });
  c.back();
  assert.equal(c.recoveryScanning, false, 'scanning flag cleared');
  assert.equal(stopped, 1, 'camera track stopped');
  assert.ok(removed, 'preview video detached');
  assert.equal(c._recoveryStream, null);
});
