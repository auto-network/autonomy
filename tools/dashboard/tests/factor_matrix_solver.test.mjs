/* Exhaustive transition matrix over the panel's REAL handlers — the solver
 * tier (no crypto, so the whole space is cheap to walk).
 *
 * The method is the one the design's own validation used and the design note
 * (graph://3d778aef-ebf) demands: explore with the SCREENS' OWN decision
 * functions, never transition rules written in the test. Every edge applies a
 * real component handler (authCellClick / remove / enableMfa / disableMfa /
 * the add-and-enroll row mutations) to a component built from a committed
 * state, then runs the REAL write seam (stagedOperations) and the projected
 * server contract (applyOps) to land on the next committed state.
 *
 * Proved here, over every reachable state (bounded: 1 password, 3 passkeys,
 * per-device slots, MFA any-mode):
 *   1. the UI never stages an uncommittable batch — if a handler accepted an
 *      edit, stagedOperations + the server projection accept it too;
 *   2. a refused edit leaves the model bit-identical (changeCount 0);
 *   3. every landed state keeps the root reachable (the projection validates
 *      that each policy member can derive root material);
 *   4. completeness: every valid fully-slotted state is reached;
 *   5. strong connectivity: every reached state can return to the start state
 *      (no one-way doors, the 64-state machine's headline property).
 *
 * MFA specific-mode and the physical ceremonies are covered by the unit and
 * jsdom/ceremony suites; this tier is the state-space sweep.
 *
 *   node --test tools/dashboard/tests/factor_matrix_solver.test.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  credentialsPanel, buildModelV3, stagedOperations, requiredSlotEnrollments,
} from '../static/js/factor-management.js';
import { canonicalExpression } from '../static/js/ceremony/root-factor-policy.js';
import { applyOps, viewFrom } from './factor_test_helpers.mjs';

const ROOT_PUB = 'a'.repeat(64);
function hex64(seed) {
  let out = '';
  for (let i = 0; i < 64; i += 1) out += ((seed.charCodeAt(i % seed.length) + i * 7) % 16).toString(16);
  return out;
}
const PW = { fid: 'pw.a', recip: hex64('pw-recip'), access: hex64('pw-access') };
const PKS = [
  { fid: 'pk.a', cred: 'credA', recip: hex64('slot-a') },
  { fid: 'pk.b', cred: 'credB', recip: hex64('slot-b') },
  { fid: 'pk.c', cred: 'credC', recip: hex64('slot-c') },
];
const NOW = '2026-08-01T00:00:00Z';

function pwFactor() {
  return {
    factor_id: PW.fid, type: 'password',
    recipient_public_key: PW.recip, access_public_key: PW.access,
    protector: {
      kdf: { name: 'PBKDF2', hash: 'SHA-256', iterations: 600000, salt: 'AAAAAAAAAAAAAAAAAAAAAA==' },
      cipher: 'AES-256-GCM', iv: 'AAAAAAAAAAAAAAAA', wrapped_seed: 'A'.repeat(64),
    },
  };
}
function pkFactor(def, slotted) {
  return {
    factor_id: def.fid, type: 'passkey', credential_id: def.cred,
    recipients: slotted
      ? [{ recipient_public_key: def.recip, label: 'This Mac', created_at: NOW }]
      : [],
  };
}
function orOf(ids) {
  const leaves = ids.map((id) => ({ op: 'factor', factor_id: id }));
  return leaves.length === 1 ? leaves[0] : { op: 'or', children: leaves };
}

// ── abstract state ⇄ committed server state ────────────────────────────────
// state = { mfa: bool,
//           pw: null | { authority | signinOff },
//           pks: [ { slot: bool, authority | signinOff } ] }   (≤3, order = PKS)
function committedOf(state) {
  const factors = [];
  const access = [];
  if (state.pw) {
    factors.push(pwFactor());
    const on = state.mfa ? !state.pw.signinOff : state.pw.authority !== 'none';
    if (on) access.push(PW.fid);
  }
  state.pks.forEach((k, i) => {
    factors.push(pkFactor(PKS[i], k.slot));
    // an unpaired factor is never a policy member, so even under MFA its
    // access reads through `authority` (matching abstractOf)
    const on = state.mfa && k.signinOff !== undefined
      ? !k.signinOff : k.authority !== 'none';
    if (on) access.push(PKS[i].fid);
  });
  let policy;
  if (state.mfa) {
    const pkIds = state.pks.map((k, i) => (k.slot ? PKS[i].fid : null)).filter(Boolean);
    policy = { op: 'and', children: [orOf([PW.fid]), orOf(pkIds)] };
  } else {
    const full = [];
    if (state.pw && state.pw.authority === 'full') full.push(PW.fid);
    state.pks.forEach((k, i) => { if (k.authority === 'full') full.push(PKS[i].fid); });
    policy = orOf(full);
  }
  return {
    generation: 1,
    factors: factors.sort((a, b) => a.factor_id.localeCompare(b.factor_id)),
    access: access.sort(),
    policy: canonicalExpression(policy),
  };
}

function abstractOf(projected) {
  const m = buildModelV3(viewFrom(projected, ROOT_PUB), { rp_id: 'localhost' });
  const state = { mfa: m.mfaOn, pw: null, pks: [] };
  if (m.passwords.length) {
    const p = m.passwords[0];
    state.pw = m.mfaOn
      ? { signinOff: p.signin === false }
      : { authority: p.authority };
  }
  const byFactor = new Map();
  m.passkeys.forEach((r) => {
    if (!byFactor.has(r.factorId)) byFactor.set(r.factorId, r);
  });
  for (const def of PKS) {
    const r = byFactor.get(def.fid);
    if (!r) continue;
    const slot = !r.unpaired;
    // an unpaired factor is never a policy member, so even under MFA it reads
    // through its access flag
    if (m.mfaOn && slot) state.pks.push({ slot, signinOff: r.signin === false });
    else state.pks.push({ slot, authority: r.authority });
  }
  return state;
}
function canon(state) {
  const pk = (k) => (k.slot ? 'S' : 'U') + (k.signinOff !== undefined
    ? (k.signinOff ? 'x' : 'o') : ':' + k.authority);
  return JSON.stringify({
    mfa: state.mfa,
    pw: state.pw ? (state.pw.signinOff !== undefined
      ? (state.pw.signinOff ? 'x' : 'o') : state.pw.authority) : null,
    pks: state.pks.map(pk).sort(),
  });
}

function componentFrom(state) {
  const committed = committedOf(state);
  const view = viewFrom(committed, ROOT_PUB);
  const c = credentialsPanel();
  Object.assign(c, buildModelV3(view, { rp_id: 'localhost' }));
  c._committedPolicy = view.root_policy;
  c.initBaseline();
  return { c, committed };
}

// ── the edges: each applies a REAL handler (or replicates exactly the row
// mutation an async ceremony handler performs, for add/enroll) ──────────────
function edgesFor(state) {
  const edges = [];
  const { c } = componentFrom(state);
  const liveRows = [...c.passwords, ...c.passkeys].filter((r) => r.pending !== 'removed');
  liveRows.forEach((row) => {
    edges.push({
      name: 'auth ' + row.id,
      apply: (cc) => {
        const r = [...cc.passwords, ...cc.passkeys].find((x) => x.id === row.id);
        cc.authCellClick(r, null);
      },
    });
    edges.push({
      name: 'remove ' + row.id,
      apply: (cc) => {
        const r = cc.passwords.find((x) => x.id === row.id);
        if (r) { cc.remove(cc.passwords, r, null); return; }
        const k = cc.passkeys.find((x) => x.id === row.id);
        cc.remove(cc.passkeys, k, null);
      },
    });
  });
  if (!c.passwords.length) {
    edges.push({
      name: 'add password',
      apply: (cc) => {
        // the exact row addPassword() pushes after its (async) key derivation
        cc.passwords.push({
          id: PW.fid, factorId: PW.fid, label: 'Password', kdf: 'PBKDF2',
          iterations: 600000, created: NOW, authority: 'full', _factor: pwFactor(),
        });
      },
    });
  }
  const presentFids = new Set(c.passkeys.map((r) => r.factorId));
  const absent = PKS.find((d) => !presentFids.has(d.fid));
  if (absent) {
    edges.push({
      name: 'enroll passkey ' + absent.fid,
      apply: (cc) => {
        // the exact row _createPasskey() pushes after the WebAuthn ceremony
        const recipient = { recipient_public_key: absent.recip, label: 'This Mac', created_at: NOW };
        cc.passkeys.push({
          id: 'pk-new-' + (cc._newId++), factorId: absent.fid, credId: absent.cred,
          device: 'This Mac', synced: false, label: 'This Mac', transports: ['internal'],
          created: NOW, authority: 'full', unpaired: false,
          recipientPub: absent.recip,
          _enroll: { recipient, registered: true },
        });
      },
    });
  }
  c.passkeys.filter((r) => r.unpaired).forEach((row) => {
    const def = PKS.find((d) => d.fid === row.factorId);
    edges.push({
      name: 'enroll-this-device ' + row.factorId,
      apply: (cc) => {
        // the exact mutations enrollThisDevice() performs after its get()+PRF
        const k = cc.passkeys.find((x) => x.id === row.id);
        cc.passkeys.push({
          id: 'pk-new-' + (cc._newId++), factorId: k.factorId, credId: k.credId,
          device: 'This Mac', synced: k.synced, label: 'This Mac',
          transports: (k.transports || []).slice(), created: NOW,
          authority: k.authority, recipientPub: def.recip,
          _addRecipient: {
            factorId: k.factorId,
            recipient: { recipient_public_key: def.recip, label: 'This Mac', created_at: NOW },
          },
        });
        const i = cc.passkeys.indexOf(k); if (i > -1) cc.passkeys.splice(i, 1);
      },
    });
  });
  if (!state.mfa) {
    edges.push({
      name: 'enable MFA (any)',
      apply: (cc) => { cc.startMfaSetup(); cc.enableMfa(); },
    });
  } else {
    edges.push({ name: 'disable MFA', apply: (cc) => cc.disableMfa() });
  }
  return edges;
}

function step(state, edge) {
  const { c, committed } = componentFrom(state);
  edge.apply(c);
  if (c.changeCount === 0) return { refused: true };
  // what commit() does before building operations: acquire a device slot for
  // every factor the ending state grants authority to without material —
  // through the REAL staging path, with synthetic ceremony material
  requiredSlotEnrollments(c).forEach((row, i) => {
    c._stageSlotRow(row, hex64('minted-' + row.factorId + '-' + i));
  });
  let ops;
  try {
    ops = stagedOperations(c);
  } catch (e) {
    throw new Error(`UI staged an uncommittable batch: ${edge.name} from ${canon(state)} — ${e.message}`);
  }
  let projected;
  try {
    projected = applyOps(committed, ops);
  } catch (e) {
    throw new Error(`server refused a staged batch the UI allowed: ${edge.name} from ${canon(state)} — ${e.message}`);
  }
  return { next: abstractOf(projected) };
}

// ── the valid-state generator (for the completeness claim) ─────────────────
function* allValidStates() {
  // Valid committed display states per the operator's ruling: outside MFA a
  // factor is Full authority or Unlock only — 'No authority' exists ONLY
  // under MFA (a member whose sign-in is off). A slotless factor can hold
  // Unlock only (enrolled, material pending); a committed policy MEMBER
  // always holds material (the commit ceremony acquires it), so slotless
  // full/member states do not exist as committed states.
  const pkOff = [null,
    { slot: true, authority: 'full' }, { slot: true, authority: 'unlock' },
    { slot: false, authority: 'unlock' }];
  const pkOn = [null,
    { slot: true, signinOff: false }, { slot: true, signinOff: true }];
  // mfa off. A password's ladder outside MFA is full↔unlock only (the design's
  // single-ladder cell), and disableMfa always restores the password to full —
  // so password-'none' without MFA is deliberately never offered (were the
  // server ever to present it, the cell recovers it: none → full).
  for (const pw of [null, { authority: 'full' }, { authority: 'unlock' }]) {
    for (let i = 0; i < pkOff.length; i += 1) {
      for (let j = i; j < pkOff.length; j += 1) {
        for (let k = j; k < pkOff.length; k += 1) {
          const pks = [pkOff[i], pkOff[j], pkOff[k]].filter(Boolean);
          const anyFull = (pw && pw.authority === 'full') || pks.some((x) => x.authority === 'full');
          if (!anyFull) continue;
          yield { mfa: false, pw, pks };
        }
      }
    }
  }
  // mfa on (any mode): needs the password and ≥1 slotted passkey
  for (const pw of [{ signinOff: false }, { signinOff: true }]) {
    for (let i = 0; i < pkOn.length; i += 1) {
      for (let j = i; j < pkOn.length; j += 1) {
        for (let k = j; k < pkOn.length; k += 1) {
          const pks = [pkOn[i], pkOn[j], pkOn[k]].filter(Boolean);
          if (!pks.some((x) => x.slot)) continue;
          yield { mfa: true, pw, pks };
        }
      }
    }
  }
}

test('exhaustive matrix: every handler edge from every reachable state', () => {
  const seedA = { mfa: false, pw: { authority: 'full' }, pks: [] };
  const seedB = { mfa: false, pw: { authority: 'full' }, pks: [{ slot: false, authority: 'unlock' }] };
  const queue = [seedA, seedB];
  const seen = new Map([[canon(seedA), seedA], [canon(seedB), seedB]]);
  const out = new Map();   // canon → Set of canon (edges)
  let edgeCount = 0; let refusedCount = 0;

  while (queue.length) {
    const state = queue.shift();
    const key = canon(state);
    // self-consistency: committing nothing round-trips the state
    assert.equal(canon(abstractOf(committedOf(state))), key, 'round-trip stable: ' + key);
    const targets = new Set();
    for (const edge of edgesFor(state)) {
      const r = step(state, edge);
      if (r.refused) { refusedCount += 1; continue; }
      const nk = canon(r.next);
      edgeCount += 1;
      targets.add(nk);
      if (!seen.has(nk)) { seen.set(nk, r.next); queue.push(r.next); }
    }
    out.set(key, targets);
  }

  // completeness: every valid fully-slotted state is reachable (unpaired
  // factors can only ever come FROM the server — migration — never be created
  // locally, so the unpaired portion of the space is seeded, not generated)
  const missing = [];
  let validCount = 0;
  for (const s of allValidStates()) {
    const fullySlotted = s.pks.every((k) => k.slot);
    if (!fullySlotted) continue;
    validCount += 1;
    if (!seen.has(canon(s))) missing.push(canon(s));
  }
  assert.deepEqual(missing, [], 'unreachable valid states');

  // strong connectivity: every reached state returns to seedA
  const reverse = new Map();
  for (const [from, tos] of out) {
    for (const to of tos) {
      if (!reverse.has(to)) reverse.set(to, new Set());
      reverse.get(to).add(from);
    }
  }
  const back = new Set([canon(seedA)]);
  const bq = [canon(seedA)];
  while (bq.length) {
    const k = bq.shift();
    for (const from of (reverse.get(k) || [])) {
      if (!back.has(from)) { back.add(from); bq.push(from); }
    }
  }
  const stranded = [...seen.keys()].filter((k) => !back.has(k));
  assert.deepEqual(stranded, [], 'states that cannot return to start');

  console.error(`matrix: ${seen.size} states reached (${validCount} fully-slotted valid), `
    + `${edgeCount} accepted transitions, ${refusedCount} solver refusals — all committable, all reversible`);
});
