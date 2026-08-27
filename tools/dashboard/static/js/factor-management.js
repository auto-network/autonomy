/* Manage credentials — the production mount of the approved design.
 *
 * The Alpine component, markup, and styles below are the Design Studio design
 * of record (design f27e2660-c2cf-46ba-b71b-5285b058311d, final revision
 * 0dcfd62e-c2ef-415b-85ba-175c2e813d75) taken VERBATIM: the same staging model
 * (edit freely, ONE root-authorized commit at the end, change count is a NET
 * diff vs the committed baseline), the same solver that refuses invalid states
 * (always ≥1 way to reach the root), the same screens and strings.
 *
 * Exactly these hooks are real instead of the design's fakes:
 *   load       — GET /api/identity/{status,personal,factor-policy} → the
 *                design's model shape (buildModelV3)
 *   rename     — factor / recipient metadata PATCH (instant, personal
 *                metadata, outside the batch)
 *   verify     — the typed password opens its v3 password factor client-side
 *   passkey    — real WebAuthn create()/get() + PRF; the design's one-button
 *                ceremony stays, the button now runs the real thing
 *   commit     — baseline diff → factor-policy operations → authorize screen
 *                collects the CURRENT policy's factors → preview →
 *                buildFactorPolicyArmor → signFactorPolicyTransition → commit
 *
 * v3 ONLY. A migration_required identity renders a notice; the one-time
 * v2→v3 upgrade lives in the login path (unlock.js), nowhere else.
 */
import * as primitives from './ceremony/primitives.js';
import {
  prfEvalExtension, prfOutputFromResults, evaluatePrf, attestedCredential,
  deriveProvisioningKey, mintEnrollmentStatement,
} from './ceremony/enrollment.js';
import {
  FACTOR_RECIPIENT_PURPOSE, canonicalExpression, policyFactorIds,
  policySatisfied, policyWithFactorGranted, createPasswordFactor,
  openPasswordFactor, parseFactorPolicyArmor, buildFactorPolicyArmor,
  openFactorPolicyArmor, signFactorPolicyTransition,
  recoverySlot, recoveryRecipientPublicKey, openRootWithRecovery,
} from './ceremony/root-factor-policy.js';
import {
  generateRecoveryCode, deriveRecoveryFactors, encodeRecoveryCode, decodeRecoveryCode,
} from './ceremony/recovery.js';

// ── small helpers ──────────────────────────────────────────────────────────
function b64uToBytes(s) {
  let b = s.replace(/-/g, '+').replace(/_/g, '/'); while (b.length % 4) b += '=';
  const bin = atob(b); const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
  return out;
}
function bytesToB64u(bytes) {
  const v = new Uint8Array(bytes); let bin = '';
  for (let i = 0; i < v.length; i += 1) bin += String.fromCharCode(v[i]);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}
function randHex(n) {
  const b = crypto.getRandomValues(new Uint8Array(n));
  return Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
}
function nowIso() { return new Date().toISOString().replace(/\.\d+Z$/, 'Z'); }
// Canonical-order JSON for policy-tree comparison (keys sorted recursively).
function stableJson(v) {
  if (Array.isArray(v)) return '[' + v.map(stableJson).join(',') + ']';
  if (v && typeof v === 'object') {
    return '{' + Object.keys(v).sort().map((k) => JSON.stringify(k) + ':' + stableJson(v[k])).join(',') + '}';
  }
  return JSON.stringify(v);
}
async function fetchJson(path) {
  const r = await fetch(path, {
    credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' },
  });
  return r.json();
}
async function postJson(path, body) {
  const r = await fetch(path, {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
  });
  return r.json().catch(() => ({ ok: false, error: 'unreadable server response' }));
}
function deviceLabel() {
  const p = (navigator.userAgentData && navigator.userAgentData.platform)
    || navigator.platform || '';
  if (/mac/i.test(p)) return 'This Mac';
  if (/iphone/i.test(p)) return 'iPhone';
  if (/ipad/i.test(p)) return 'iPad';
  if (/win/i.test(p)) return 'This PC';
  if (/android/i.test(p)) return 'This phone';
  return 'This device';
}
// Diagnostics only — error text + non-secret context; never key material.
function reportCeremonyError(ceremony, action, err) {
  try {
    fetch('/api/identity/ceremony-error', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ceremony,
        action: action || '',
        name: (err && err.name) || '',
        message: (err && err.message) || String(err),
        stack: (err && err.stack) || '',
        context: {},
      }),
    }).catch(() => {});
  } catch (e) { /* diagnostics must never throw */ }
}

// ── the read seam: server v3 factor-policy view → the design's model shape ──
// (Consumed from session/auto-0812-211339 commit 33c09cce, with the mfaMode
// inference replaced by a policy-tree comparison and the per-device slot rows
// added: one row per passkey RECIPIENT, exactly the design's slot model.)
export function factorAuthority(rootRole, access) {
  if (rootRole === 'individual') return 'full';
  return access === 'enabled' ? 'unlock' : 'none';
}
function orOfLeaves(ids) {
  const leaves = ids.map((id) => ({ op: 'factor', factor_id: id }));
  return leaves.length === 1 ? leaves[0] : { op: 'or', children: leaves };
}
export function buildModelV3(view, status) {
  const factors = (view && view.factors) || [];
  const passwords = factors.filter((f) => f.type === 'password').map((f) => ({
    id: f.factor_id,
    factorId: f.factor_id,
    label: f.label || 'Password',
    kdf: (f.kdf && f.kdf.name) || 'PBKDF2',
    iterations: (f.kdf && f.kdf.iterations) || 0,
    created: f.created_at || '',
    authority: factorAuthority(f.root_role, f.access),
    signin: f.root_role === 'mfa-member' && f.access !== 'enabled' ? false : undefined,
  }));
  // A row is a SLOT, keyed by (credential, device recipient) — the design's
  // per-device model. A factor with no recipients yet renders one "unpaired"
  // row: it can sign in, but cannot hold authority until a device enrolls.
  const passkeys = [];
  factors.filter((f) => f.type === 'passkey').forEach((f) => {
    const authority = factorAuthority(f.root_role, f.access);
    const shared = {
      factorId: f.factor_id,
      factorLabel: f.label || 'Passkey',
      credId: f.credential_id,
      synced: !!f.backed_up,
      transports: f.transports || [],
      authority,
      signin: f.root_role === 'mfa-member' && f.access !== 'enabled' ? false : undefined,
    };
    const recips = f.recipients || [];
    if (!recips.length) {
      passkeys.push({
        ...shared,
        id: f.factor_id + '@unpaired',
        label: f.label || 'Passkey',
        device: null,
        created: f.created_at || '',
        recipientPub: null,
        unpaired: true,
      });
      return;
    }
    recips.forEach((r) => {
      passkeys.push({
        ...shared,
        id: f.factor_id + '@' + r.recipient_public_key.slice(0, 12),
        label: r.label,
        device: r.label,
        created: r.created_at || f.created_at || '',
        recipientPub: r.recipient_public_key,
      });
    });
  });
  const mfaPws = passwords.filter((p) => factors.find((f) => f.factor_id === p.factorId).root_role === 'mfa-member').map((p) => p.id);
  const mfaPks = passkeys.filter((k) => factors.find((f) => f.factor_id === k.factorId).root_role === 'mfa-member').map((k) => k.id);
  const mfaOn = mfaPws.length > 0 || mfaPks.length > 0;
  // Mode is read from the TREE SHAPE, never inferred from role presence: 'any'
  // iff the current policy equals AND[OR(all passwords), OR(all slot-bearing
  // passkeys)] — otherwise a specific selection.
  let mfaMode = 'any';
  if (mfaOn) {
    const pwIds = passwords.map((p) => p.factorId);
    const pkIds = [...new Set(passkeys.filter((k) => !k.unpaired).map((k) => k.factorId))];
    let anyTree = null;
    if (pwIds.length && pkIds.length) {
      try {
        anyTree = canonicalExpression({ op: 'and', children: [orOfLeaves(pwIds), orOfLeaves(pkIds)] });
      } catch (e) { anyTree = null; }
    }
    let current = null;
    try { current = canonicalExpression(view.root_policy); } catch (e) { current = null; }
    mfaMode = (anyTree && current && stableJson(anyTree) === stableJson(current)) ? 'any' : 'specific';
  }
  return {
    passwords,
    passkeys,
    mfaOn,
    mfaMode,
    mfaPws,
    mfaPks,
    generation: view ? view.generation : 0,
    rootPub: view ? view.root_pub : null,
    currentRpId: (status && status.rp_id) || '',
    minIterations: 600000,
  };
}

// ── the write seam: baseline diff → the frozen factor-policy operation union ─
// Pure over (model-like, baseline-like) so the transition matrix can drive it
// directly. Order matters for server-side projection: enrolls first, policy last.
export function stagedOperations(m) {
  const b = m._baseline;
  const ops = { enroll: [], change: [], addRec: [], removeRec: [], removeFactor: [], access: [], policy: [] };
  const liveOf = (rows) => rows.filter((r) => r.pending !== 'removed');
  const accessOf = (rows) => rows.some((r) => (m.inMfaRoot(r) ? r.signin !== false : r.authority !== 'none'));

  // passwords
  b.p.forEach((bp) => {
    const live = m.passwords.find((x) => x.id === bp.id);
    if (!live || live.pending === 'removed') ops.removeFactor.push({ op: 'remove_factor', factor_id: bp.factorId });
  });
  liveOf(m.passwords).forEach((p) => {
    const base = b.p.find((x) => x.id === p.id);
    if (!base) {
      if (!p._factor) throw new Error('a new password is missing its staged key material — remove and re-add it');
      ops.enroll.push({ op: 'enroll_password', factor: p._factor, access: accessOf([p]) });
    } else if (p.pwChanged) {
      if (!p._factor) throw new Error('a changed password is missing its staged key material — re-enter it');
      ops.change.push({ op: 'change_password', factor_id: p.factorId, factor: p._factor });
    }
  });

  // passkeys, grouped to factor level (rows are per-device slots)
  const factorIds = [...new Set([...b.k.map((r) => r.factorId), ...m.passkeys.map((r) => r.factorId)])];
  factorIds.forEach((fid) => {
    const had = b.k.filter((r) => r.factorId === fid);
    const have = liveOf(m.passkeys.filter((r) => r.factorId === fid));
    if (had.length && !have.length) {
      ops.removeFactor.push({ op: 'remove_factor', factor_id: fid });
      return;
    }
    if (!had.length && have.length) {
      const en = have.find((r) => r._enroll);
      if (!en) throw new Error('a new passkey is missing its staged ceremony — remove and re-add it');
      ops.enroll.push({
        op: 'enroll_passkey',
        factor: {
          factor_id: fid,
          type: 'passkey',
          credential_id: en.credId,
          recipients: en._enroll.recipient ? [en._enroll.recipient] : [],
        },
        access: accessOf(have),
      });
      return;
    }
    had.forEach((br) => {
      if (!have.find((r) => r.id === br.id) && br.recipientPub) {
        ops.removeRec.push({ op: 'remove_passkey_recipient', factor_id: fid, recipient_public_key: br.recipientPub });
      }
    });
    have.forEach((r) => {
      if (r._addRecipient) ops.addRec.push({ op: 'add_passkey_recipient', factor_id: fid, recipient: r._addRecipient.recipient });
    });
  });

  // dashboard access — surviving, pre-existing factors whose derived access moved
  const groups = [];
  b.p.forEach((bp) => {
    const rows = liveOf(m.passwords.filter((x) => x.id === bp.id));
    if (rows.length) groups.push({ fid: bp.factorId, rows, base: bp.access !== false && bp.authority !== 'none' && !(bp.signin === false) });
  });
  [...new Set(b.k.map((r) => r.factorId))].forEach((fid) => {
    const rows = liveOf(m.passkeys.filter((r) => r.factorId === fid));
    const baseRows = b.k.filter((r) => r.factorId === fid);
    if (rows.length) groups.push({ fid, rows, base: baseRows.some((r) => r.authority !== 'none' && !(r.signin === false)) });
  });
  groups.forEach((g) => {
    const want = accessOf(g.rows);
    if (want !== g.base) ops.access.push({ op: 'set_access', factor_id: g.fid, enabled: want });
  });

  // root policy — the model's desired tree vs the committed one
  const desired = desiredPolicy(m);
  const current = canonicalExpression(m._committedPolicy);
  if (stableJson(desired) !== stableJson(current)) {
    ops.policy.push({ op: 'set_root_policy', policy: desired });
  }

  return [...ops.enroll, ...ops.change, ...ops.addRec, ...ops.removeRec,
    ...ops.removeFactor, ...ops.access, ...ops.policy];
}

// The root-policy tree the model's current state means. Mirrors the design:
// no MFA → OR over full-authority factors; MFA 'any' → AND of the two class
// ORs over every enrolled factor; 'specific' → AND over the checked ones.
// A passkey factor participates only when it has ≥1 device slot.
export function desiredPolicy(m) {
  const livePw = m.passwords.filter((p) => p.pending !== 'removed');
  const livePk = m.passkeys.filter((k) => k.pending !== 'removed');
  if (m.mfaOn) {
    const pwIds = (m.mfaMode === 'any' ? livePw : livePw.filter((p) => m.mfaPws.includes(p.id)))
      .map((p) => p.factorId);
    const pkIds = [...new Set(
      (m.mfaMode === 'any' ? livePk : livePk.filter((k) => m.mfaPks.includes(k.id)))
        .map((k) => k.factorId),
    )];
    if (!pwIds.length || !pkIds.length) {
      throw new Error('Multi-factor needs at least one enrolled password and one enrolled passkey');
    }
    return canonicalExpression({ op: 'and', children: [orOfLeaves([...new Set(pwIds)]), orOfLeaves(pkIds)] });
  }
  const leaves = new Set();
  livePw.forEach((p) => { if (p.authority === 'full') leaves.add(p.factorId); });
  livePk.forEach((k) => { if (k.authority === 'full') leaves.add(k.factorId); });
  if (!leaves.size) throw new Error('Keep at least one credential with full authority — otherwise you could never unlock your root.');
  return canonicalExpression(orOfLeaves([...leaves]));
}

// Passkey factors whose STAGED ending state makes them policy members while
// they hold no key material yet: the commit ceremony must acquire a device
// slot for each (one get()+PRF tap) before the operations can build.
export function requiredSlotEnrollments(m) {
  let leaves;
  try { leaves = policyFactorIds(desiredPolicy(m)); } catch (e) { return []; }
  const need = [];
  [...new Set(m.passkeys.filter((k) => k.pending !== 'removed').map((k) => k.factorId))]
    .forEach((fid) => {
      if (!leaves.includes(fid)) return;
      const rows = m.passkeys.filter((k) => k.factorId === fid && k.pending !== 'removed');
      const hasMaterial = rows.some((r) => r.recipientPub
        || (r._enroll && r._enroll.recipient) || r._addRecipient);
      if (!hasMaterial) need.push(rows[0]);
    });
  return need;
}

// ── the root ceremony's discovery: what opens the armor, from the armor ────
// The policy tree is deterministic and discoverable: expand it into its
// MINIMAL satisfying factor-sets. Every prompt the authorize screen shows is
// derived from these sets and from what has been collected so far — never
// from an assumed shape. Handles every legal tree: one password, any-one of
// N, either-of-mixed, one-of-each (MFA any), specific-of-each, and nested
// combinations.
export function satisfyingSets(policy) {
  const canonical = canonicalExpression(policy);
  function minimize(sets) {
    const uniq = [];
    const seen = new Set();
    for (const raw of sets) {
      const s = [...new Set(raw)].sort();
      const k = s.join(',');
      if (!seen.has(k)) { seen.add(k); uniq.push(s); }
    }
    return uniq.filter((s) => !uniq.some(
      (t) => t !== s && t.length < s.length && t.every((id) => s.includes(id)),
    ));
  }
  function walk(n) {
    if (n.op === 'factor') return [[n.factor_id]];
    if (n.op === 'or') return minimize(n.children.flatMap(walk));
    let acc = [[]];
    for (const child of n.children) {
      const cs = walk(child);
      const next = [];
      for (const a of acc) for (const c of cs) next.push([...a, ...c]);
      acc = next;
    }
    return minimize(acc);
  }
  return walk(canonical);
}

// ── what exactly is being authorized, in words ─────────────────────────────
// The same baseline diff stagedOperations commits, rendered as a bulleted
// human description for the ceremony screen: the operator sees precisely what
// their root authority is about to sign.
const AUTH_WORD = { full: 'Full authority', unlock: 'Unlock only', none: 'No authority' };
export function describeStagedChanges(m) {
  const lines = [];
  const b = m._baseline;
  if (!b) return lines;
  const live = (rows) => rows.filter((r) => r.pending !== 'removed');
  const authLine = (label, from, to) => '“' + label + '”: ' + AUTH_WORD[from] + ' → ' + AUTH_WORD[to];
  const signinLine = (label, off) => (off ? 'Turn sign-in off for “' : 'Turn sign-in back on for “') + label + '”';

  b.p.forEach((bp) => {
    const cur = m.passwords.find((x) => x.id === bp.id);
    if (!cur || cur.pending === 'removed') lines.push('Remove password “' + (bp.label || 'Password') + '”');
  });
  live(m.passwords).forEach((p) => {
    const base = b.p.find((x) => x.id === p.id);
    if (!base) { lines.push('Add password “' + (p.label || 'Password') + '”'); return; }
    if (p.pwChanged) lines.push('Change password “' + (p.label || 'Password') + '”');
    if (base.authority !== p.authority) lines.push(authLine(p.label || 'Password', base.authority, p.authority));
    if ((base.signin === false) !== (p.signin === false)) lines.push(signinLine(p.label || 'Password', p.signin === false));
  });

  const fids = [...new Set([...b.k.map((r) => r.factorId), ...m.passkeys.map((r) => r.factorId)])];
  fids.forEach((fid) => {
    const had = b.k.filter((r) => r.factorId === fid);
    const have = live(m.passkeys.filter((r) => r.factorId === fid));
    const first = (have[0] || had[0]) || {};
    const label = first.factorLabel || first.label || 'Passkey';
    if (had.length && !have.length) { lines.push('Remove passkey “' + label + '”'); return; }
    if (!had.length && have.length) {
      lines.push('Enroll passkey “' + label + '” on this device');
    } else {
      had.forEach((br) => {
        if (!have.find((r) => r.id === br.id)) {
          lines.push('Remove device “' + (br.device || br.label || 'device') + '” from “' + label + '”');
        }
      });
      have.forEach((r) => {
        if (r._addRecipient) lines.push('Enroll this device (“' + r._addRecipient.recipient.label + '”) for “' + label + '”');
      });
    }
    const b0 = had[0]; const c0 = have[0];
    if (b0 && c0) {
      if (b0.authority !== c0.authority) lines.push(authLine(label, b0.authority, c0.authority));
      if ((b0.signin === false) !== (c0.signin === false)) lines.push(signinLine(label, c0.signin === false));
    }
  });

  if (m.mfaChanged) {
    if (m.mfaOn && !b.on) {
      lines.push(m.mfaMode === 'any'
        ? 'Turn on multi-factor — any one password and any one passkey, together'
        : 'Turn on multi-factor — the selected password and passkey, together');
    } else if (!m.mfaOn && b.on) lines.push('Turn off multi-factor');
    else lines.push('Change the multi-factor selection');
  }
  return lines;
}

// ── the Alpine component: the design's script, verbatim except the hooks ────
let hostCallbacks = { onBack: null, onClose: null };

export function credentialsPanel() {
  return {
    identity: { name: '', initial: '' },
    stack: [{ s: 'credentials' }],
    passwords: [], passkeys: [],
    mfaOn: false, mfaMode: 'any', mfaPws: [], mfaPks: [],
    currentRpId: '', minIterations: 600000,

    // transient (design verbatim)
    password: '', newPw: '', verifying: false, toast: '', _baseline: null, _newId: 0,
    warnAt: null, _wt: null,
    inlineFor: null, inlineMode: null, inlineVal: '', inlineVal2: '', inlineResult: null,
    inlineAutofilled: false, _revT: null,
    renameFor: null, renameVal: '',
    pickMode: 'any', pickPws: [], pickPks: [],

    // production state (hooks only)
    loading: true, loadError: null, migrationPending: false, committing: false,
    generation: 0, rootPub: null, armorText: null, envelope: null,
    _committedPolicy: null, _authSeeds: {}, _viewFactors: [],
    deviceName: '', newDevErr: null, authAutofilled: false,
    _recovery: null, _recoveryCode: null, _recoveryPrintable: null, _recoveryQr: null, recoveryScanning: false, recoveryVerifyResult: null, recoveryInput: '',
    authShowMissing: false, authDeadEnd: false,

    init() { this.load(); },

    // ── HOOK: load — the real preset() ────────────────────────────────────
    async load() {
      this.loading = true; this.loadError = null; this.migrationPending = false;
      try {
        const [st, pj, fp] = await Promise.all([
          fetchJson('/api/identity/status'),
          fetchJson('/api/identity/personal'),
          fetchJson('/api/identity/factor-policy'),
        ]);
        if (pj && pj.error) throw new Error(pj.error);
        if (fp && fp.error) throw new Error(fp.error);
        this.identity = {
          name: (st.personal_identity && st.personal_identity.display_name) || '',
          initial: (((st.personal_identity && st.personal_identity.display_name) || '?').trim().charAt(0) || '?').toUpperCase(),
        };
        if (fp.migration_required) { this.migrationPending = true; this.loading = false; return; }
        const m = buildModelV3(fp, st);
        this.passwords = m.passwords; this.passkeys = m.passkeys;
        this.mfaOn = m.mfaOn; this.mfaMode = m.mfaMode;
        this.mfaPws = m.mfaPws; this.mfaPks = m.mfaPks;
        this.generation = m.generation; this.rootPub = m.rootPub;
        this.currentRpId = m.currentRpId; this.minIterations = m.minIterations;
        this.armorText = pj.armored_private_key;
        this.envelope = await parseFactorPolicyArmor(this.armorText);
        this._committedPolicy = fp.root_policy;
        this._viewFactors = fp.factors || [];
        this._recovery = fp.recovery || null;
        this.initBaseline();
        this.loading = false;
        this._greetNewDevice();
      } catch (e) {
        this.loadError = (e && e.message) || String(e);
        this.loading = false;
      }
    },
    // A passkey login recognized this device as a new PRF slot for a synced
    // credential (stashed by unlock.js — public data only). Greet it with the
    // name-this-device dialog: if a root-opening login already wrote the slot,
    // the dialog only names it; otherwise one password confirmation in the
    // dialog completes the enrollment.
    _readStore(key) {
      try {
        const raw = (typeof sessionStorage !== 'undefined') && sessionStorage.getItem(key);
        return raw ? JSON.parse(raw) : null;
      } catch (e) { return null; }
    },
    _dropStore(key) {
      try { if (typeof sessionStorage !== 'undefined') sessionStorage.removeItem(key); } catch (e) { /* gone is gone */ }
    },
    _greetNewDevice() {
      if (this.cur === 'newdevice') return;
      const enrolled = this._readStore('autonomy.factor.slot-enrolled');
      if (enrolled) {
        this.deviceName = enrolled.label || 'New device'; this.newDevErr = null;
        this.push({ s: 'newdevice', enrolled: true, slot: enrolled });
        return;
      }
      const pending = this._readStore('autonomy.factor.pending-slot');
      if (!pending) return;
      const factor = this.envelope.factors.find((f) => f.type === 'passkey'
        && f.credential_id === pending.credential_id);
      if (!factor) { this._dropStore('autonomy.factor.pending-slot'); return; }
      if (factor.recipients.some((s) => s.recipient_public_key === pending.recipient_public_key)) {
        // another root ceremony already wrote the slot — just name it
        this._dropStore('autonomy.factor.pending-slot');
        this.deviceName = pending.label || 'New device'; this.newDevErr = null;
        this.push({
          s: 'newdevice',
          enrolled: true,
          slot: { factor_id: factor.factor_id, recipient_public_key: pending.recipient_public_key, label: pending.label },
        });
        return;
      }
      this.deviceName = pending.label || 'New device'; this.newDevErr = null;
      this.push({ s: 'newdevice', enrolled: false, slot: { ...pending, factor_id: factor.factor_id } });
    },
    async newDeviceOk() {
      const t = this.top; if (t.s !== 'newdevice' || this.committing) return;
      const name = (this.deviceName || '').trim() || 'New device';
      if (t.enrolled) {
        this._patchLabel({ factorId: t.slot.factor_id, recipientPub: t.slot.recipient_public_key }, name);
        const row = this.passkeys.find((k) => k.recipientPub === t.slot.recipient_public_key);
        if (row) { row.label = name; row.device = name; }
        this._dropStore('autonomy.factor.slot-enrolled');
        this.pop();
        this.flash('This device now has full secure access');
        return;
      }
      // the one root proof the armor demands, through the STANDARD root
      // ceremony control (the same authorize screen commit uses — reusable,
      // policy-aware, passkey or password per the current policy)
      this.newDevErr = null;
      const opened = await this.requireRoot('Enroll “' + name + '”', '',
        ['Enroll this device (“' + name + '”) for your passkey']);
      if (!opened) return;
      this.committing = true;
      try {
        try {
          const ops = [{
            op: 'add_passkey_recipient',
            factor_id: t.slot.factor_id,
            recipient: {
              recipient_public_key: t.slot.recipient_public_key,
              label: name,
              created_at: nowIso(),
            },
          }];
          // re-enrollment restores the factor's AUTHORITY, not just its slot
          const typeOf = (fid) => {
            const f = this.envelope.factors.find((x) => x.factor_id === fid);
            return f ? f.type : null;
          };
          const granted = policyWithFactorGranted(this.envelope.policy, t.slot.factor_id, typeOf);
          if (stableJson(granted) !== stableJson(canonicalExpression(this.envelope.policy))) {
            ops.push({ op: 'set_root_policy', policy: granted });
          }
          await this._commitOps(ops, opened);
        } finally { opened.seed.fill(0); }
        this._dropStore('autonomy.factor.pending-slot');
        await this.load();
        if (this.cur === 'newdevice') this.pop();
        this.flash('Your passkey is enrolled — this device now has full secure access');
      } catch (e) {
        this.newDevErr = (e && e.message) || String(e);
      } finally { this.committing = false; }
    },
    get ready() { return !this.loading && !this.loadError && !this.migrationPending; },

    // ── derived (design verbatim) ─────────────────────────────────────────
    get top() { return this.stack[this.stack.length - 1]; },
    get cur() { return this.top.s; },
    get factorCount() { return this.passwords.length + this.passkeys.length; },
    get hasFullPasskey() { return this.passkeys.some((k) => k.authority === 'full'); },
    get hasFullPassword() { return this.passwords.some((p) => p.authority === 'full'); },
    createdLocal(iso) { return iso ? new Date(iso).toLocaleString().replace(', ', ' ') : ''; },
    itersLabel(n) { return (Number(n) || 0).toLocaleString() + ' iterations'; },
    transportHuman(k) {
      const t = (k && k.transports) || []; const has = (x) => t.includes(x);
      const parts = [];
      if (has('internal')) parts.push('Built-in');
      if (has('usb') || has('nfc') || has('ble')) parts.push('Security key');
      if ((k && k.synced) || has('hybrid')) parts.push('Cloud-Sync');
      if (k && k.device === this.thisDevice()) parts.push('This device');
      return parts.join(' · ') || 'Passkey';
    },

    // ── inline tap-to-rename: local metadata PATCH, instant, no root, no commit ──
    startRename(f) { this.renameFor = f.id; this.renameVal = f.label; },
    saveRename(f) {
      if (this.renameFor !== f.id) return;
      const v = this.renameVal.trim(); if (v) { f.label = v; this._patchLabel(f, v); }
      this.renameFor = null;
    },
    cancelRename() { this.renameFor = null; },
    // HOOK: the real metadata write behind the design's instant rename.
    _patchLabel(f, label) {
      const path = f.recipientPub
        ? '/api/identity/factors/' + encodeURIComponent(f.factorId) + '/recipients/'
          + encodeURIComponent(f.recipientPub) + '/metadata'
        : '/api/identity/factors/' + encodeURIComponent(f.factorId) + '/metadata';
      fetch(path, {
        method: 'PATCH', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ label }),
      }).catch(() => { /* best-effort; server truth reappears on next load */ });
      if (f.recipientPub && f.device) f.device = label;
    },
    factorById(id) { return this.passwords.find((p) => p.id === id) || this.passkeys.find((k) => k.id === id) || null; },
    labelOf(id) { const f = this.factorById(id); return f ? (f.label || 'Password') : ''; },

    // is this factor part of the multi-factor ROOT? (any mode = every factor)
    // A factor without a local PRF slot is STILL a member: its slot is
    // acquired by the commit ceremony, not modeled as a UI state.
    inMfaRoot(f) {
      if (!this.mfaOn) return false;
      return this.mfaMode === 'any' ? true : (this.mfaPws.includes(f.id) || this.mfaPks.includes(f.id));
    },
    _isPw(f) { return this.passwords.includes(f); },
    rootMembers(isPw) {
      const arr = isPw ? this.passwords : this.passkeys;
      return arr.filter((f) => f.pending !== 'removed'
        && (this.mfaMode === 'any' || (isPw ? this.mfaPws : this.mfaPks).includes(f.id)));
    },
    roleOf(f) {
      if (this.mfaOn) return f.authority === 'none' ? 'none' : 'unlock';
      return f.authority;
    },
    unlockOn(f) { return f.authority !== 'none'; },
    fullState(f) { if (this.inMfaRoot(f)) return 'multi'; if (f.authority === 'full') return 'single'; return 'none'; },
    toggleUnlock(f, ev) {
      if (this.statusOf(f) === 'removed') return;
      if (this.fullState(f) !== 'none') { this.warnHere(ev, 'A factor with full authority can always sign in.'); return; }
      f.authority = f.authority === 'none' ? 'unlock' : 'none';
    },
    // single-ladder authority word (sign-in + root fold into one): the readable badge
    authWord(f) {
      if (this.inMfaRoot(f)) return this.signinOff(f) ? 'Multi-factor only' : 'Multi-factor w/ unlock';
      return { full: 'Full authority', unlock: 'Unlock only', none: 'No authority' }[f.authority];
    },
    authCellClick(f, ev) {
      if (this.statusOf(f) === 'removed') return;
      if (this.inMfaRoot(f)) { f.signin = (f.signin === false); return; }
      this.toggleAuthority(f, ev);
    },
    authCls(f) {
      if (this.inMfaRoot(f)) return 'au-multi';
      return { full: 'au-full', unlock: 'au-unlock', none: 'au-none' }[f.authority];
    },
    signinOff(f) { return f.signin === false; },

    // ── navigation stack ──────────────────────────────────────────────────
    push(e) { this.stack.push(e); },
    pop() { if (this.stack.length > 1) this.stack.pop(); },
    closeAll() { if (hostCallbacks.onClose) hostCallbacks.onClose(); },
    back() {
      const t = this.top;
      if (t.s === 'authorize') {
        if (t.resolve) t.resolve(false);
        this.password = ''; this.verifying = false; this._clearAuthSeeds(); this.pop();
      } else if (t.s === 'newpw') {
        if (t.resolve) t.resolve(null); this.newPw = ''; this.pop();
      } else if (this.stack.length > 1) {
        this.pop();
      } else if (hostCallbacks.onBack) hostCallbacks.onBack();
      else this.closeAll();
    },

    // ── inline verify / change: the card itself becomes the input ─────────
    verifyPassword(p) { this._inlineOpen(p.id, 'verify'); },
    changePassword(p) { this._inlineOpen(p.id, 'change'); },
    _inlineOpen(id, mode) {
      clearTimeout(this._revT); this.inlineFor = id; this.inlineMode = mode;
      this.inlineVal = ''; this.inlineVal2 = ''; this.inlineResult = null; this.inlineAutofilled = false;
    },
    cancelInline() {
      clearTimeout(this._revT); this.inlineFor = null; this.inlineMode = null;
      this.inlineVal = ''; this.inlineVal2 = ''; this.inlineResult = null; this.inlineAutofilled = false;
    },
    inlineTitle() {
      return this.inlineMode === 'verify' ? 'Verify your password'
        : this.inlineMode === 'confirm' ? 'Confirm your new password' : 'Change your password';
    },
    inlineBtnText() {
      return this.inlineMode === 'verify' ? 'Verify'
        : this.inlineMode === 'confirm' ? 'Confirm' : (this.inlineAutofilled ? 'Change' : 'Continue');
    },
    async submitInline(p) {
      if (this.inlineMode === 'verify') {
        if (!this.inlineVal) return;
        // HOOK: the real check — the typed password must open its own factor.
        try {
          const factor = this.envelope.factors.find((f) => f.factor_id === p.factorId);
          const seed = await openPasswordFactor(this.rootPub, factor, this.inlineVal);
          seed.fill(0);
          this.inlineResult = 'ok';
          this._revT = setTimeout(() => this.cancelInline(), 1600);
        } catch (e) { this.inlineResult = 'fail'; }
        return;
      }
      if (this.inlineMode === 'change') {
        if (!this.inlineVal) return;
        if (this.inlineAutofilled) { await this._applyChange(p); return; }
        this.inlineMode = 'confirm'; this.inlineResult = null; return;
      }
      if (this.inlineMode === 'confirm') {
        if (!this.inlineVal2) return;
        if (this.inlineVal2 !== this.inlineVal) { this.inlineResult = 'mismatch'; return; }
        await this._applyChange(p);
      }
    },
    async _applyChange(p) {
      // HOOK: derive the replacement factor NOW (fresh salt), stage it; the
      // plaintext never leaves this handler. STAGE; commit later with root.
      const made = await createPasswordFactor(this.rootPub, p.factorId, this.inlineVal, this.minIterations);
      made.seed.fill(0);
      p._factor = made.factor;
      p.pwChanged = true; p.created = nowIso();
      this.inlineResult = 'ok';
      this._revT = setTimeout(() => this.cancelInline(), 1300);
    },

    // ── change tracking: NET diff vs the committed baseline ───────────────
    initBaseline() {
      this._baseline = JSON.parse(JSON.stringify({
        p: this.passwords, k: this.passkeys, on: this.mfaOn, mode: this.mfaMode,
        pws: this.mfaPws, pks: this.mfaPks,
      }));
    },
    _baseFactor(id) {
      if (!this._baseline) return null;
      return this._baseline.p.find((x) => x.id === id) || this._baseline.k.find((x) => x.id === id) || null;
    },
    statusOf(f) {
      if (f.pending === 'removed') return 'removed';
      const b = this._baseFactor(f.id); if (!b) return 'new';
      if (f.pwChanged) return 'changed';
      if (b.authority !== f.authority) return 'changed';
      if ((b.signin === false) !== (f.signin === false)) return 'changed';
      return null;
    },
    get mfaChanged() {
      return !this._baseline || this._baseline.on !== this.mfaOn || this._baseline.mode !== this.mfaMode
        || JSON.stringify(this._baseline.pws) !== JSON.stringify(this.mfaPws)
        || JSON.stringify(this._baseline.pks) !== JSON.stringify(this.mfaPks);
    },
    get changeCount() {
      let n = 0;
      this.passwords.forEach((p) => { if (this.statusOf(p)) n += 1; });
      this.passkeys.forEach((k) => { if (this.statusOf(k)) n += 1; });
      if (this.mfaChanged) n += 1;
      return n;
    },
    cancelChanges() {
      if (this._baseline) {
        const b = JSON.parse(JSON.stringify(this._baseline));
        this.passwords = b.p; this.passkeys = b.k; this.mfaOn = b.on; this.mfaMode = b.mode;
        this.mfaPws = b.pws; this.mfaPks = b.pks;
      }
    },

    // ── HOOK: commit — the ONE root-authorized write ──────────────────────
    async commit() {
      const n = this.changeCount; if (!n || this.committing) return;
      // The staged ending state may grant authority to a passkey with no key
      // material on record: acquire each missing device slot NOW (one tap per
      // factor), so the ending state is reached in this one commit.
      for (const row of requiredSlotEnrollments(this)) {
        let minted;
        try { minted = await this._mintSlotFor(row); }
        catch (e) { this.flash((e && e.message) || String(e)); return; }
        if (!minted) {
          this.flash('Enrolling “' + (row.factorLabel || row.label) + '” was cancelled — your edits are still staged');
          return;
        }
      }
      let ops;
      try { ops = stagedOperations(this); } catch (e) { this.flash((e && e.message) || String(e)); return; }
      let lines = [];
      try { lines = describeStagedChanges(this); } catch (e) { lines = []; }
      const opened = await this.requireRoot('Commit ' + n + (n === 1 ? ' change' : ' changes'), '', lines);
      if (!opened) return;
      this.committing = true;
      try {
        await this._commitOps(ops, opened);
        await this.load();
        this.flash('Committed ' + n + (n === 1 ? ' change' : ' changes'));
      } catch (e) {
        reportCeremonyError('factor-commit', 'commit', e);
        this.flash((e && e.message) || String(e));
      } finally {
        opened.seed.fill(0);
        this.committing = false;
      }
    },
    // the one write path: register staged credentials, preview the batch,
    // build + sign the candidate armor, commit the generation
    async _commitOps(ops, opened) {
      for (const r of this.passkeys) {
        if (r._enroll && !r._enroll.registered) await this._registerStaged(r, opened);
      }
      const pv = await postJson('/api/identity/factor-policy/preview', {
        base_generation: this.generation, operations: ops,
      });
      if (!pv.ok) throw new Error(pv.error || 'the staged changes were refused');
      const armor = await buildFactorPolicyArmor({
        rootSeed: opened.seed, rootPub: this.rootPub, generation: pv.generation,
        factors: pv.factors, access: pv.access, policy: pv.root_policy,
        recovery: pv.recovery || undefined,
      });
      const signature = await signFactorPolicyTransition({
        signingKey: opened.signingKey, baseGeneration: this.generation,
        operations: ops, candidateArmor: armor,
      });
      const res = await postJson('/api/identity/factor-policy/commit', {
        base_generation: this.generation, operations: ops,
        candidate_armor: armor, root_signature: signature,
      });
      if (!res.ok) throw new Error(res.error || 'the authorization was refused');
    },
    // ── recovery code (design graph://fd418706-97e) ──────────────────────
    get hasRecovery() { return !!this._recovery; },
    get recoveryCreated() { return (this._recovery && this._recovery.created_at) || ''; },
    // Generate a fresh code, show it once, then enrol it through the SAME
    // root ceremony + commit path as any factor change: a set_recovery op
    // carrying the slot, and the same slot embedded in the candidate armor.
    async startRecovery() {
      const code = generateRecoveryCode();
      this._recoveryCode = code;
      this._recoveryPrintable = await encodeRecoveryCode(code);
      this.push({ s: 'recovery-explain' });
    },
    async generateRecoveryCode() {
      // move from the explanation to the one-time presentation
      if (!this._recoveryCode) {
        this._recoveryCode = generateRecoveryCode();
        this._recoveryPrintable = await encodeRecoveryCode(this._recoveryCode);
      }
      this._recoveryQr = '';
      this._qrSvg(this._recoveryPrintable).then((svg) => { this._recoveryQr = svg; });
      this.push({ s: 'recovery-present' });
    },
    get recoveryPrintable() { return this._recoveryPrintable || ''; },
    get recoveryQr() { return this._recoveryQr || ''; },
    // lazy-load a vendored UMD script once (qrcode-generator / jsQR)
    _loadScript(src) {
      return new Promise((resolve, reject) => {
        if (typeof document === 'undefined') { reject(new Error('no document')); return; }
        const existing = document.querySelector('script[data-fui="' + src + '"]');
        if (existing) { resolve(); return; }
        const el = document.createElement('script');
        el.src = src; el.dataset.fui = src;
        el.onload = () => resolve(); el.onerror = () => reject(new Error('failed to load ' + src));
        setTimeout(() => reject(new Error('timed out loading ' + src)), 4000);
        document.head.appendChild(el);
      });
    },
    async _qrSvg(text) {
      try {
        if (typeof window === 'undefined' || !window.qrcode) {
          await this._loadScript('/static/vendor/qrcode-generator-1.4.4.js');
        }
        const qr = window.qrcode(0, 'M');
        qr.addData(text); qr.make();
        return qr.createSvgTag({ cellSize: 4, margin: 0, scalable: true });
      } catch (e) { return ''; }   // display-only; logic never depends on it
    },
    // Print an 8.5x11 recovery sheet: the QR + code + how-to-use, the generated
    // date, NO account name, NO print timestamp. Print CSS hides everything else.
    printRecovery() {
      if (typeof document === 'undefined') return;
      const gen = this.createdLocal(nowIso());
      const sheet = document.createElement('div');
      sheet.id = 'fui-print-sheet';
      sheet.innerHTML =
        '<div class="fui-print-inner">'
        + '<h1>Autonomy Network — Recovery Code</h1>'
        + '<div class="fui-print-qr">' + (this._recoveryQr || '') + '</div>'
        + '<div class="fui-print-code">' + this._formatCodeBlocks(this._recoveryPrintable) + '</div>'
        + '<p class="fui-print-gen">Generated ' + gen + '</p>'
        + '<div class="fui-print-how"><h2>What this is</h2>'
        + '<p>This is the recovery code for an Autonomy Network identity. It is the '
        + 'last way to get back in if every password and passkey is lost, and it can '
        + 're-secure the account if one is stolen.</p>'
        + '<h2>How to use it</h2>'
        + '<p>On the sign-in screen, choose “Use recovery code”, then scan this QR '
        + 'code or type the words below. Keep this sheet somewhere safe and offline. '
        + 'Anyone who has it can recover the identity, and it cannot be re-created if '
        + 'it is lost.</p></div></div>';
      const style = document.createElement('style');
      style.id = 'fui-print-style';
      style.textContent =
        '@media print { body > *:not(#fui-print-sheet) { display:none !important; }'
        + ' #fui-print-sheet { display:block !important; } }'
        + ' #fui-print-sheet { display:none; position:fixed; inset:0; background:#fff; color:#000; z-index:99999; }'
        + ' @page { size:letter; margin:0.75in; }'
        + ' .fui-print-inner { font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; max-width:6.5in; margin:0 auto; padding:0.5in 0; }'
        + ' .fui-print-inner h1 { font-size:20px; margin:0 0 18px; }'
        + ' .fui-print-qr svg { width:2.2in; height:2.2in; }'
        + ' .fui-print-code { font-family:ui-monospace,Menlo,monospace; font-size:20px; letter-spacing:2px; margin:16px 0; }'
        + ' .fui-print-gen { color:#555; font-size:12px; margin:0 0 20px; }'
        + ' .fui-print-how h2 { font-size:14px; margin:16px 0 4px; } .fui-print-how p { margin:0 0 10px; }';
      document.body.appendChild(style); document.body.appendChild(sheet);
      const cleanup = () => { sheet.remove(); style.remove(); window.removeEventListener('afterprint', cleanup); };
      window.addEventListener('afterprint', cleanup);
      window.print();
    },
    _formatCodeBlocks(printable) {
      return String(printable || '').split(/[\s-]+/).filter(Boolean).join(' &nbsp; ');
    },
    // "I've saved it" → the confirm step, still BEFORE the point of no return:
    // nothing is committed until they verify (or skip), so a mismatch can go
    // back and see the code again.
    confirmRecoverySaved() {
      this.recoveryInput = ''; this.recoveryVerifyResult = null;
      this.push({ s: 'recovery-verify', wizard: true });
    },
    seeCodeAgain() {
      // from the wizard verify step back to the presentation (allowed until
      // the enrolment completes)
      if (this.top.s === 'recovery-verify') this.pop();
    },
    // Wizard verify: the typed/scanned copy must MATCH the code we still hold,
    // then commit. Standalone verify (enrolled row): the code must OPEN the
    // armor, read-only.
    async submitVerifyRecovery(printable) {
      this.recoveryVerifyResult = null;
      let code;
      try { code = await decodeRecoveryCode(String(printable || '').trim()); }
      catch (e) { this.recoveryVerifyResult = 'malformed'; return; }
      if (this.top.wizard) {
        const want = this._recoveryCode;
        const matches = want && code.length === want.length
          && code.every((b, i) => b === want[i]);
        if (!matches) { this.recoveryVerifyResult = 'fail'; return; }
        this.recoveryVerifyResult = 'ok';
        await this._enrolRecovery();
        return;
      }
      try {
        const opened = await openRootWithRecovery(this.armorText, code);
        opened.seed.fill(0);
        this.recoveryVerifyResult = 'ok';
      } catch (e) { this.recoveryVerifyResult = 'fail'; }
    },
    // Commit the code: open the CURRENT root authority, build the slot with the
    // opened seed, commit one set_recovery op (also embedded in the candidate).
    async _enrolRecovery() {
      const opened = await this.requireRoot('Add a recovery code', '', ['Add a recovery code']);
      if (!opened) return;
      this.committing = true;
      try {
        const recipient = await recoveryRecipientPublicKey(this._recoveryCode);
        const { recoveryPub } = await deriveRecoveryFactors(this._recoveryCode);
        const slot = await recoverySlot({
          rootSeed: opened.seed, recoveryRecipientPub: recipient, recoveryPub, createdAt: nowIso(),
        });
        await this._commitOps([{ op: 'set_recovery', recovery: slot }], opened);
        this._recoveryCode = null; this._recoveryPrintable = null;
        await this.load();
        this.stack = [{ s: 'credentials' }];
        this.flash('Recovery code saved');
      } catch (e) {
        this.flash((e && e.message) || String(e));
      } finally { opened.seed.fill(0); this.committing = false; }
    },
    // Verify an already-enrolled code (from the enrolled row): read-only.
    startVerifyRecovery() {
      this.recoveryInput = ''; this.recoveryVerifyResult = null;
      this.push({ s: 'recovery-verify', wizard: false });
    },
    // Camera QR scan (best-effort): the type field is always available as the
    // fallback, so a browser without a camera loses nothing.
    async startRecoveryScan() {
      if (typeof navigator === 'undefined' || !navigator.mediaDevices) { this.flash('No camera on this device — type the code instead'); return; }
      try {
        if (!window.jsQR) await this._loadScript('/static/vendor/jsQR-1.4.0.js');
        this._recoveryStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } });
        this.recoveryScanning = true;
        const video = document.createElement('video');
        video.setAttribute('playsinline', ''); video.srcObject = this._recoveryStream;
        await video.play();
        const canvas = document.createElement('canvas');
        const tick = async () => {
          if (!this.recoveryScanning) return;
          if (video.readyState === video.HAVE_ENOUGH_DATA) {
            canvas.width = video.videoWidth; canvas.height = video.videoHeight;
            const ctx = canvas.getContext('2d');
            ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
            const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
            const found = window.jsQR(img.data, img.width, img.height);
            if (found && found.data) {
              this.recoveryInput = found.data; this.stopRecoveryScan();
              await this.submitVerifyRecovery(found.data); return;
            }
          }
          requestAnimationFrame(tick);
        };
        requestAnimationFrame(tick);
      } catch (e) { this.recoveryScanning = false; this.flash('Could not open the camera — type the code instead'); }
    },
    stopRecoveryScan() {
      this.recoveryScanning = false;
      if (this._recoveryStream) { this._recoveryStream.getTracks().forEach((t) => t.stop()); this._recoveryStream = null; }
    },
    async _registerStaged(row, opened) {
      const en = row._enroll;
      const statement = await mintEnrollmentStatement({
        credentialId: en.credId,
        credentialPublicKey: en.credentialPublicKeyHex,
        rpId: en.minted.rp_id,
        origin: en.minted.origin,
        nonce: en.minted.nonce,
        createdHlc: [Date.now(), 0],
        signer: opened.rootPub,
        initialSignCount: en.signCount,
        provisioningPublicKey: en.provisioningPublicKey,
        label: row.label,
        transports: en.transports,
      }, opened.signingKey);
      const result = await postJson('/api/identity/passkey/register', {
        label: row.label,
        credential: {
          id: en.credId,
          rawId: en.credId,
          type: 'public-key',
          authenticatorAttachment: en.attachment || undefined,
          // PRF support only — never the PRF OUTPUT, which derives key material.
          clientExtensionResults: en.prfSupported ? { prf: { enabled: true } } : {},
          response: {
            clientDataJSON: en.clientDataJSON,
            attestationObject: en.attestationObject,
            transports: en.transports,
          },
        },
        statement,
      });
      if (!result.ok) throw new Error(result.error || 'passkey registration was refused');
      en.registered = true;
    },

    // ── the authorize screen: the design's requireRoot, real crypto ───────
    requireRoot(detail, step, lines) {
      return new Promise((r) => {
        this.password = ''; this._authSeeds = {}; this.authShowMissing = false; this.authDeadEnd = false;
        this.push({ s: 'authorize', detail, step: step || '', lines: lines || [], resolve: r });
        // no WebAuthn at all + every route needs a passkey: dead end, known now
        const hasWebAuthn = typeof window !== 'undefined' && !!window.PublicKeyCredential
          && typeof navigator !== 'undefined' && !!navigator.credentials;
        if (!hasWebAuthn && this.authNeedsPasskeyOnly) {
          this.authDeadEnd = true; this.authShowMissing = true;
        }
      });
    },
    // What the CURRENT (committed) policy accepts — discovered from the armor,
    // never assumed: the minimal satisfying factor-sets, filtered by what has
    // been collected so far. The screen prompts for exactly what is still
    // NEEDED, in any order, and each supplied factor validates independently
    // (password: its protector's authenticated decryption; passkey: enrolled
    // recipient membership) before the joint open.
    get authLeaves() { return this.envelope ? policyFactorIds(this.envelope.policy) : []; },
    get authSets() { return this.envelope ? satisfyingSets(this.envelope.policy) : []; },
    _authType(id) {
      const f = this.envelope && this.envelope.factors.find((x) => x.factor_id === id);
      return f ? f.type : null;
    },
    get authCollected() { return Object.keys(this._authSeeds); },
    get authAchievable() {
      const col = this.authCollected;
      return this.authSets.filter((s) => col.every((id) => s.includes(id)));
    },
    get authNeeded() {
      const col = new Set(this.authCollected);
      const out = new Set();
      this.authAchievable.forEach((s) => s.forEach((id) => { if (!col.has(id)) out.add(id); }));
      return [...out];
    },
    get authPw() { return this.authNeeded.some((id) => this._authType(id) === 'password'); },
    get authPk() { return this.authNeeded.some((id) => this._authType(id) === 'passkey'); },
    get authBoth() {
      const sets = this.authAchievable;
      return sets.length > 0 && sets.every((s) => s.some((id) => this._authType(id) === 'password')
        && s.some((id) => this._authType(id) === 'passkey'));
    },
    get authPwDone() { return this.authCollected.some((id) => this._authType(id) === 'password'); },
    get authPkDone() { return this.authCollected.some((id) => this._authType(id) === 'passkey'); },
    // Dead-end detection: this device cannot complete ANY achievable set —
    // every route still open requires a passkey it cannot produce. Definitive
    // when the browser has no WebAuthn at all; inferred after a tap proves the
    // available credential is not enrolled here. The explanation lists the
    // required factors and the enrolled devices that WOULD get you in.
    get authNeedsPasskeyOnly() {
      const sets = this.authAchievable;
      return sets.length > 0 && sets.every((s) => s.some((id) => this._authType(id) === 'passkey'
        && !this._authSeeds[id]));
    },
    get authMissingPasskeys() {
      const needed = new Set(this.authNeeded);
      const rows = (this._viewFactors || []).filter((f) => f.type === 'passkey' && needed.has(f.factor_id));
      return rows.map((f) => ({
        label: f.label || 'Passkey',
        devices: (f.recipients || []).map((r) => r.label).join(', ') || 'no device enrolled',
      }));
    },
    get authMissingLead() {
      // one required factor everywhere vs. alternatives
      const single = this.authMissingPasskeys.length === 1;
      return single
        ? 'The following factor is required but missing on this device:'
        : 'At least one of the following factors must be provided:';
    },
    _clearAuthSeeds() {
      Object.values(this._authSeeds).forEach((s) => { if (s && s.fill) s.fill(0); });
      this._authSeeds = {};
    },
    authWithPasskey() { this._authPasskey(); },
    async _authPasskey() {
      if (this.top.s !== 'authorize' || this.verifying) return;
      try {
        const needed = new Set(this.authNeeded);
        const pkFactors = this.envelope.factors.filter((f) => f.type === 'passkey'
          && needed.has(f.factor_id));
        const allow = pkFactors.map((f) => ({ type: 'public-key', id: b64uToBytes(f.credential_id) }));
        const asrt = await navigator.credentials.get({ publicKey: {
          challenge: crypto.getRandomValues(new Uint8Array(32)),
          rpId: this.currentRpId || undefined,
          allowCredentials: allow,
          userVerification: 'required',
          extensions: prfEvalExtension(),
        } });
        const prf = prfOutputFromResults(asrt.getClientExtensionResults());
        if (!prf) throw new Error('this passkey has no PRF and cannot authorize your root');
        const rec = await primitives.deriveEncapsulationKeypair(prf, FACTOR_RECIPIENT_PURPOSE);
        const credId = bytesToB64u(asrt.rawId);
        const match = pkFactors.find((f) => f.credential_id === credId
          && f.recipients.some((s) => s.recipient_public_key === rec.publicKeyHex));
        if (!match) {
          prf.fill(0);
          // the tap proved this device holds no enrolled slot: surface the
          // requirement list; a hard dead end when no other route remains
          this.authShowMissing = true;
          if (this.authNeedsPasskeyOnly) this.authDeadEnd = true;
          throw new Error('This passkey works for sign-in, but this device is not enrolled to authorize your root.');
        }
        this._authSeeds = { ...this._authSeeds, [match.factor_id]: prf };
        await this._settle();
      } catch (e) {
        if (e && e.name === 'NotAllowedError') {
          // cancelled or no usable credential here — show what WOULD work
          this.authShowMissing = true;
          return;
        }
        this.flash((e && e.message) || String(e));
      }
    },
    authWithPassword() { if (this.password) this._authPassword(); },
    async _authPassword() {
      if (this.top.s !== 'authorize' || this.verifying) return;
      const needed = new Set(this.authNeeded);
      const pwFactors = this.envelope.factors.filter((f) => f.type === 'password'
        && needed.has(f.factor_id));
      let opened = null;
      for (const f of pwFactors) {
        try {
          opened = { fid: f.factor_id, seed: await openPasswordFactor(this.rootPub, f, this.password) };
          break;
        } catch (e) { /* this password may name another enrolled factor */ }
      }
      if (!opened) { this.flash('Incorrect — try again'); return; }
      this.password = '';
      this._authSeeds = { ...this._authSeeds, [opened.fid]: opened.seed };
      await this._settle();
    },
    async _settle() {
      if (!policySatisfied(this.envelope.policy, Object.keys(this._authSeeds))) return;   // AND: await the other factor
      this.verifying = true;
      try {
        const opened = await openFactorPolicyArmor(this.armorText, this._authSeeds);
        const t = this.top; this.verifying = false;
        if (t.s === 'authorize') {
          const res = t.resolve; this.pop(); this.password = ''; this._clearAuthSeeds();
          if (res) res(opened);
        } else { opened.seed.fill(0); }
      } catch (e) {
        this.verifying = false; this._clearAuthSeeds();
        this.flash((e && e.message) || 'Those factors did not open your root — try again');
      }
    },
    collectNewPassword(title) {
      return new Promise((r) => { this.newPw = ''; this.push({ s: 'newpw', title, resolve: r }); });
    },
    newpwContinue() {
      const t = this.top;
      if (t.s === 'newpw' && this.newPw) {
        const res = t.resolve; const v = this.newPw; this.pop(); this.newPw = '';
        if (res) res(v);
      }
    },

    // ── actions (all STAGE) ───────────────────────────────────────────────
    // ── per-device slots for one credential ───────────────────────────────
    thisDevice() { return deviceLabel(); },
    enrolledHere(k) { return this.passkeys.some((x) => x.credId === k.credId && x.device === this.thisDevice()); },
    // Offer enrollment on a SYNCED credential with no slot on this device. We
    // can't know it's usable here without a ceremony — the offer is
    // honest-optimistic, and tapping it runs the ceremony that resolves it.
    offerEnroll(k) { return (k.synced || k.unpaired) && k.device !== this.thisDevice() && !this.enrolledHere(k); },
    sib(k) { return this.passkeys.some((x) => x.id !== k.id && x.credId === k.credId); },
    // The physical half of a slot enrollment: one get()+PRF on the factor's
    // credential, staging its _addRecipient row. Returns the row, 'already',
    // or null (cancelled); throws on real errors. Used by the row action, the
    // +Add pivot, AND the commit ceremony when the staged ending state needs
    // a slot this factor doesn't have yet.
    async _mintSlotFor(k) {
      const asrt = await navigator.credentials.get({ publicKey: {
        challenge: crypto.getRandomValues(new Uint8Array(32)),
        rpId: this.currentRpId || undefined,
        allowCredentials: [{ type: 'public-key', id: b64uToBytes(k.credId) }],
        userVerification: 'required',
        extensions: prfEvalExtension(),
      } }).catch((e) => { if (e && e.name === 'NotAllowedError') return null; throw e; });
      if (!asrt) return null;
      const prf = prfOutputFromResults(asrt.getClientExtensionResults());
      if (!prf) throw new Error('this passkey has no PRF and cannot hold a device slot');
      const rec = await primitives.deriveEncapsulationKeypair(prf, FACTOR_RECIPIENT_PURPOSE);
      prf.fill(0);
      const already = this.passkeys.some((x) => x.factorId === k.factorId && x.recipientPub === rec.publicKeyHex);
      if (already) return 'already';
      return this._stageSlotRow(k, rec.publicKeyHex);
    },
    // The staging half, ceremony-free: what the freshly derived recipient key
    // becomes in the model. Split out so the transition matrix can drive the
    // REAL staging with synthetic material the way commit drives it with
    // ceremony material.
    _stageSlotRow(k, recipientPubHex) {
      const row = {
        id: 'pk-new-' + (this._newId++),
        factorId: k.factorId,
        factorLabel: k.factorLabel || k.label,
        credId: k.credId,
        device: this.thisDevice(),
        synced: k.synced,
        label: this.thisDevice(),
        transports: (k.transports || []).slice(),
        created: nowIso(),
        authority: k.authority,
        recipientPub: recipientPubHex,
        _addRecipient: {
          factorId: k.factorId,
          recipient: { recipient_public_key: recipientPubHex, label: this.thisDevice(), created_at: nowIso() },
        },
      };
      this.passkeys.push(row);
      // an unpaired placeholder row is replaced by its first real slot
      if (k.unpaired) { const i = this.passkeys.indexOf(k); if (i > -1) this.passkeys.splice(i, 1); }
      return row;
    },
    async enrollThisDevice(k) {
      // HOOK: the real get()+PRF here → a new device slot for the credential.
      try {
        const r = await this._mintSlotFor(k);
        if (r === 'already') { this.flash('Already enrolled on this device'); return; }
        if (r) this.flash(this.thisDevice() + ' enrolled');
      } catch (e) {
        this.flash((e && e.message) || String(e));
      }
    },

    startAddPasskey() { this.push({ s: 'addpk' }); },
    // The recovery the whole thread is about: if a synced credential exists but
    // has no slot on this device, create() throws InvalidStateError — we catch
    // that and pivot to get()+PRF, adding THIS device's slot. The user only
    // ever sees success + a new row.
    get pendingEnroll() { return this.passkeys.find((k) => this.offerEnroll(k)) || null; },
    async usePasskey() {
      const p = this.pendingEnroll;
      if (p) { await this.enrollThisDevice(p); this.pop(); return; }
      try {
        await this._createPasskey();
        this.pop(); this.flash('Passkey enrolled');
      } catch (e) {
        if (e && e.name === 'NotAllowedError') return;
        if (e && e.name === 'InvalidStateError') {
          // an existing credential answered — enroll this device's slot instead
          const any = this.passkeys[0];
          if (any) { await this.enrollThisDevice(any); this.pop(); return; }
        }
        this.flash((e && e.message) || String(e));
      }
    },
    // HOOK: phase A of enrollment — the physical ceremony, staged locally.
    // Phase B (root-signed statement + server registration) runs inside commit.
    async _createPasskey() {
      const minted = await postJson('/api/identity/passkey/register-options', {});
      if (!minted.ok) throw new Error(minted.error || 'could not start enrollment');
      const pk = minted.options;
      pk.challenge = b64uToBytes(pk.challenge);
      pk.user.id = b64uToBytes(pk.user.id);
      (pk.excludeCredentials || []).forEach((c) => { c.id = b64uToBytes(c.id); });
      pk.extensions = prfEvalExtension();
      const cred = await navigator.credentials.create({ publicKey: pk });
      if (!cred) throw new Error('enrollment was cancelled');
      const createResults = (cred.getClientExtensionResults && cred.getClientExtensionResults()) || {};
      const prf = await evaluatePrf(createResults, async () => {
        const asrt = await navigator.credentials.get({ publicKey: {
          challenge: crypto.getRandomValues(new Uint8Array(32)),
          rpId: minted.rp_id,
          allowCredentials: [{ type: 'public-key', id: cred.rawId }],
          userVerification: 'required',
          extensions: prfEvalExtension(),
        } });
        return asrt.getClientExtensionResults();
      });
      const authData = new Uint8Array(cred.response.getAuthenticatorData());
      const { credentialPublicKeyHex, signCount } = attestedCredential(authData);
      const flags = authData[32];
      const backedUp = !!(flags & 0x10);
      let recipient = null; let provisioningPublicKey = null;
      if (prf) {
        const rec = await primitives.deriveEncapsulationKeypair(new Uint8Array(prf), FACTOR_RECIPIENT_PURPOSE);
        recipient = { recipient_public_key: rec.publicKeyHex, label: this.thisDevice(), created_at: nowIso() };
        provisioningPublicKey = (await deriveProvisioningKey(prf)).publicKeyHex;
        new Uint8Array(prf).fill(0);
      }
      const transports = (cred.response.getTransports && cred.response.getTransports()) || [];
      const credId = bytesToB64u(cred.rawId);
      const factorId = 'pk.' + randHex(6);
      this.passkeys.push({
        id: 'pk-new-' + (this._newId++),
        factorId,
        credId,
        device: this.thisDevice(),
        synced: backedUp,
        label: this.thisDevice(),
        transports,
        created: nowIso(),
        authority: recipient ? 'full' : 'unlock',
        unpaired: !recipient,
        recipientPub: recipient ? recipient.recipient_public_key : null,
        _enroll: {
          minted: { rp_id: minted.rp_id, origin: minted.origin, nonce: minted.nonce },
          credId,
          clientDataJSON: bytesToB64u(cred.response.clientDataJSON),
          attestationObject: bytesToB64u(cred.response.attestationObject),
          credentialPublicKeyHex,
          signCount,
          transports,
          attachment: cred.authenticatorAttachment || null,
          prfSupported: !!prf || !!(createResults.prf && createResults.prf.enabled),
          recipient,
          provisioningPublicKey,
          registered: false,
        },
      });
    },
    async addPassword() {
      const v = await this.collectNewPassword('Add a password'); if (v === null) return;
      // HOOK: PBKDF2(v, fresh salt) HERE; stage the derived factor.
      const factorId = 'pw.' + randHex(6);
      const made = await createPasswordFactor(this.rootPub, factorId, v, this.minIterations);
      made.seed.fill(0);
      this.passwords.push({
        id: factorId, factorId, label: 'Password', kdf: 'PBKDF2',
        iterations: this.minIterations, created: nowIso(), authority: 'full',
        _factor: made.factor,
      });
    },

    // ── solver: never allow an invalid state ──────────────────────────────
    _live() { return [...this.passwords, ...this.passkeys].filter((f) => f.pending !== 'removed'); },
    _othersFull(f) { return this._live().some((x) => x !== f && x.authority === 'full'); },
    canToggle(f) {
      if (this.mfaOn) return true;
      return f.authority === 'unlock' || this._othersFull(f);
    },
    toggleAuthority(f, ev) {
      if (this.mfaOn) { f.authority = f.authority === 'none' ? 'unlock' : 'none'; return; }
      if (!this.canToggle(f)) {
        this.warnHere(ev, 'Keep at least one credential with full authority — otherwise you could never unlock your root.');
        return;
      }
      f.authority = f.authority === 'full' ? 'unlock' : 'full';
    },
    // a warning bubble anchored at the tap point (not a bottom-centered toast)
    warnHere(ev, text) {
      this.warnAt = { text, x: ev ? ev.clientX : 0, y: ev ? ev.clientY : 0 };
      clearTimeout(this._wt); this._wt = setTimeout(() => { this.warnAt = null; }, 3200);
    },
    canRemove(f) {
      if (this.mfaOn) {
        if (!this.inMfaRoot(f)) return this._live().length > 1;
        const remaining = this.rootMembers(this._isPw(f)).filter((x) => x !== f).length;
        return remaining >= 1;
      }
      const s = this._live().filter((x) => x !== f); if (!s.length) return false;
      return f.authority !== 'full' || s.some((x) => x.authority === 'full');
    },
    remove(list, f, ev) {
      if (!this.canRemove(f)) {
        this.warnHere(ev, this.mfaOn
          ? 'Multi-factor needs at least one enrolled password and one passkey — this is the last of its kind.'
          : (this._live().length <= 1 ? 'You can’t remove your only credential.' : 'Keep at least one credential with full authority — removing this would lock you out of your root.'));
        return;
      }
      const b = this._baseFactor(f.id);
      if (!b) { const i = list.indexOf(f); if (i > -1) list.splice(i, 1); return; }
      f.pending = 'removed';
    },
    undoRemove(f) { delete f.pending; },

    // ── multi-factor setup (two modes) ────────────────────────────────────
    startMfaSetup() {
      this.pickMode = this.mfaOn ? this.mfaMode : 'any';
      this.pickPws = (this.mfaOn && this.mfaMode === 'specific') ? [...this.mfaPws] : this.passwords.map((p) => p.id);
      this.pickPks = (this.mfaOn && this.mfaMode === 'specific') ? [...this.mfaPks] : this.passkeys.map((k) => k.id);
      this.push({ s: 'mfa-setup' });
    },
    togglePick(which, id) { const a = this[which]; const i = a.indexOf(id); if (i >= 0) a.splice(i, 1); else a.push(id); },
    pickedPw(p) { return this.pickMode === 'any' || this.pickPws.includes(p.id); },
    pickedPk(k) { return this.pickMode === 'any' || this.pickPks.includes(k.id); },
    _join(a) {
      if (a.length <= 1) return a[0] || '';
      if (a.length === 2) return a[0] + ' and ' + a[1];
      return a.slice(0, -1).join(', ') + ', and ' + a[a.length - 1];
    },
    mfaSetupText() {
      const pwM = this.passwords.filter((p) => this.pickedPw(p));
      const pkM = this.passkeys.filter((k) => this.pickedPk(k));
      const req = this.pickMode === 'any'
        ? 'Full authority will require any one of your passwords and any one of your passkeys, together.'
        : 'Full authority will require one of the checked passwords and one of the checked passkeys, together.';
      const unlockers = [...pwM, ...pkM].filter((f) => f.signin !== false).map((f) => '“' + f.label + '”');
      const total = pwM.length + pkM.length;
      let unl;
      if (unlockers.length === 0) unl = (total === 2 ? 'Neither' : 'None of them') + ' will unlock the dashboard on ' + (total === 2 ? 'its' : 'their') + ' own.';
      else unl = this._join(unlockers) + (unlockers.length === 1 ? ' will unlock the dashboard on its own.' : ' will unlock the dashboard on their own.');
      return req + ' ' + unl;
    },
    get canEnableMfa() {
      // Live rows only (a staged-removed factor cannot anchor MFA). A factor
      // without a local PRF slot still counts: the commit ceremony acquires
      // its slot on the way to the ending state.
      const livePw = this.passwords.filter((p) => p.pending !== 'removed');
      const livePk = this.passkeys.filter((k) => k.pending !== 'removed');
      return this.pickMode === 'any'
        ? (livePw.length >= 1 && livePk.length >= 1)
        : (livePw.some((p) => this.pickPws.includes(p.id))
          && livePk.some((k) => this.pickPks.includes(k.id)));
    },
    enableMfa() {
      if (!this.canEnableMfa) return;
      this.mfaOn = true; this.mfaMode = this.pickMode;
      if (this.pickMode === 'specific') { this.mfaPws = [...this.pickPws]; this.mfaPks = [...this.pickPks]; } else { this.mfaPws = []; this.mfaPks = []; }
      this._live().forEach((f) => { if (f.authority === 'full' && !this.inMfaRoot(f)) f.authority = 'unlock'; });
      this.pop(); this.flash('Multi-factor staged');
    },
    disableMfa() {
      const keepFull = this._live().find((f) => this.passwords.includes(f)) || this._live()[0];
      this.mfaOn = false; this.mfaMode = 'any'; this.mfaPws = []; this.mfaPks = [];
      if (keepFull) keepFull.authority = 'full';
      // 'No authority' is not a valid state outside MFA: rows whose sign-in
      // was off under MFA land on Unlock only, deterministically
      this._live().forEach((f) => {
        if (f.authority === 'none') f.authority = 'unlock';
        delete f.signin;
      });
      this.flash('Multi-factor turned off — this credential now holds full authority');
    },

    flash(m) { this.toast = m; setTimeout(() => { if (this.toast === m) this.toast = ''; }, 2600); },
  };
}

// ── styles: the design's stylesheet, scoped under .fui-cred ────────────────
const STYLE = `
.fui-cred{ --bg:#0b0f17; --panel:#111827; --panel2:#0d1420; --line:#1f2937; --line2:#374151;
  --ink:#e5e7eb; --dim:#9ca3af; --faint:#6b7280; --accent:#6366f1; --accent2:#818cf8;
  --good:#34d399; --goodbg:#064e3b; --warn:#fbbf24; --warnbg:#4d3908; --danger:#f87171; --mfa:#38bdf8; --mfabg:#0c2c40;
  position:relative; display:flex; flex-direction:column; min-height:0; flex:1; background:var(--panel); color:var(--ink);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; -webkit-font-smoothing:antialiased; }
.fui-cred *{ box-sizing:border-box; } .fui-cred [x-cloak]{ display:none!important; }
.fui-cred .closex{ margin-left:auto; background:none; border:none; color:var(--faint); font-size:22px; line-height:1; cursor:pointer; padding:2px 8px; border-radius:8px; } .fui-cred .closex:hover{ background:#0d1420; color:var(--ink); }
.fui-cred .lnk.lchange{ color:var(--accent2); } .fui-cred .lnk.lverify{ color:#34d399; } .fui-cred .lnk.ldisable{ color:var(--danger); }
.fui-cred .lnk.lchange:hover, .fui-cred .lnk.lverify:hover, .fui-cred .lnk.ldisable:hover{ text-decoration:underline; background:none; }
.fui-cred .mfaactions{ display:flex; flex-direction:column; align-items:flex-end; margin-left:auto; flex:none; }
.fui-cred .fmeta>div{ line-height:1.5; }
.fui-cred .credframe{ display:flex; flex-direction:column; flex:1; min-height:0; }
.fui-cred .credcontent{ flex:1; min-height:0; display:flex; flex-direction:column; }
.fui-cred .credcontent > template + div, .fui-cred .credcontent > div{ flex:1; min-height:0; display:flex; flex-direction:column; }
.fui-cred .scrhead{ display:flex; align-items:center; gap:10px; padding:13px 14px 12px; border-bottom:1px solid var(--line); }
.fui-cred .scrhead .back{ background:none; border:none; color:var(--accent2); cursor:pointer; display:flex; align-items:center; padding:2px 4px; }
.fui-cred .scrhead .ttl{ min-width:0; } .fui-cred .scrhead h1{ margin:0; font-size:15.5px; font-weight:650; } .fui-cred .scrhead .sub{ font-size:12px; color:var(--dim); }
.fui-cred .scrhead .step{ margin-left:auto; font-size:10.5px; font-weight:700; color:var(--accent2); background:#171e33; border:1px solid #33406b; border-radius:999px; padding:2px 8px; }
.fui-cred .body{ overflow-y:auto; padding:6px 12px 16px; flex:1; }
.fui-cred{ --rec:#f0b429; --recbg:#2a1f05; --recborder:#5a4410; }
.fui-cred .mfacard{ margin:10px 6px 4px; border:1px solid var(--line2); border-radius:12px; padding:12px; display:flex; align-items:center; gap:11px; background:linear-gradient(180deg,#0c1626,#0b1220); }
.fui-cred .reccard{ border-color:var(--recborder); background:linear-gradient(180deg,#251c07,#160f02); }
.fui-cred .reccard .ficon{ color:var(--rec); border-color:var(--recborder); background:rgba(240,180,41,.08); }
.fui-cred .reccard .mt{ color:#f7cf6b; }
.fui-cred .addbtn.rec{ color:#f7cf6b; border-color:var(--recborder); }
.fui-cred .authcell.au-rec .ac-ico{ color:var(--rec); border-color:var(--recborder); background:rgba(240,180,41,.08); } .fui-cred .authcell.au-rec .ac-lbl{ color:#f7cf6b; }
.fui-cred .btn-rec{ background:var(--rec); color:#231a02; border-color:var(--rec); }
.fui-cred .recwarn{ border:1px solid var(--recborder); background:var(--recbg); border-radius:12px; padding:12px 14px; font-size:12.5px; line-height:1.5; color:#e9d8a6; margin:14px 0 0; }
.fui-cred .reccode{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:17px; letter-spacing:1px; text-align:center; background:#0b111b; border:1px solid var(--recborder); border-radius:12px; padding:14px 10px; color:#f7cf6b; line-height:1.9; word-spacing:6px; flex:1; }
.fui-cred .recqr{ width:150px; height:150px; flex:none; background:#fff; padding:9px; border-radius:10px; }
.fui-cred .recqr svg{ width:100%; height:100%; display:block; }
.fui-cred .recrow{ display:flex; gap:14px; align-items:center; margin:6px 0 4px; }
.fui-cred .exp p{ font-size:13.5px; color:#c5cbd6; margin:0 0 12px; } .fui-cred .exp p b{ color:var(--ink); }
.fui-cred .choice{ display:flex; gap:10px; margin-top:6px; } .fui-cred .choice .btn{ flex:1; flex-direction:column; padding:18px 10px; gap:6px; }
.fui-cred .mfacard.pending{ border-color:#5865e6; box-shadow:inset 0 0 0 1px rgba(88,101,230,.35); }
.fui-cred .mfacard .mi{ min-width:0; flex:1; } .fui-cred .mfacard .mt{ font-weight:650; font-size:14px; white-space:nowrap; } .fui-cred .mfacard .md{ font-size:12px; color:var(--dim); margin-top:3px; }
.fui-cred .mfaenabled{ font-size:10.5px; font-weight:700; color:var(--mfa); background:var(--mfabg); border:1px solid #1e5b7e; border-radius:999px; padding:1px 9px; margin-bottom:5px; }
.fui-cred .grouphead{ display:flex; align-items:center; padding:14px 8px 6px; } .fui-cred .grouphead .grouplbl{ padding:0; }
.fui-cred .grouplbl{ font-size:11px; letter-spacing:.12em; text-transform:uppercase; color:var(--faint); }
.fui-cred .addbtn{ margin-left:auto; background:none; border:1px solid var(--line2); color:var(--accent2); border-radius:8px; padding:4px 11px; font:inherit; font-size:12px; font-weight:650; cursor:pointer; display:inline-flex; align-items:center; gap:5px; } .fui-cred .addbtn:hover{ background:#12203b; } .fui-cred .addbtn svg{ width:12px; height:12px; }
.fui-cred .row{ display:flex; align-items:center; gap:12px; padding:12px; border:1px solid var(--line); background:var(--panel2); border-radius:12px; margin:6px 0; }
.fui-cred .ficon{ position:relative; width:34px; height:34px; border-radius:9px; flex:none; display:grid; place-items:center; background:#0b1220; border:1px solid var(--line2); color:var(--accent2); } .fui-cred .ficon svg{ width:18px; height:18px; }
.fui-cred .authcell{ width:80px; flex:none; display:flex; flex-direction:column; align-items:center; gap:6px; cursor:pointer; text-align:center; }
.fui-cred .authcell .ac-ico{ position:relative; width:34px; height:34px; border-radius:9px; display:grid; place-items:center; background:#0b1220; border:1px solid var(--line2); color:var(--accent2); overflow:visible; }
.fui-cred .authcell .ac-ico svg{ width:18px; height:18px; }
.fui-cred .authcell .ac-lbl{ font-size:10.5px; line-height:1.15; font-weight:600; color:var(--dim); }
.fui-cred .mfab{ position:absolute; top:-6px; right:-6px; width:16px; height:16px; line-height:0; }
.fui-cred .mfab .armor{ width:16px; height:16px; overflow:visible; }
.fui-cred .mfab .armor path{ fill:url(#armorGrad); stroke:#cdd3ff; stroke-width:1.3; stroke-linejoin:round; filter:drop-shadow(0 0 3px rgba(124,134,255,.9)); }
.fui-cred .mfab.dim .armor path{ fill:#39415a; stroke:#727c96; filter:none; }
.fui-cred .authcell.au-full .ac-ico{ color:#34d399; border-color:rgba(52,211,153,.45); background:rgba(52,211,153,.08); } .fui-cred .authcell.au-full .ac-lbl{ color:#34d399; }
.fui-cred .authcell.au-multi .ac-ico{ color:var(--mfa); border-color:rgba(120,130,240,.5); background:rgba(120,130,240,.08); } .fui-cred .authcell.au-multi .ac-lbl{ color:var(--mfa); }
.fui-cred .authcell.au-unlock .ac-ico{ color:var(--accent2); } .fui-cred .authcell.au-unlock .ac-lbl{ color:#93a3bd; }
.fui-cred .authcell.au-none .ac-ico{ color:var(--line2); background:transparent; } .fui-cred .authcell.au-none .ac-lbl{ color:#5b6578; }
.fui-cred .fmid{ min-width:0; flex:1; } .fui-cred .fname{ font-weight:600; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.fui-cred .fmeta{ font-size:12px; color:var(--dim); margin-top:2px; overflow-wrap:break-word; }
.fui-cred .row.removing{ opacity:.6; border:1px dashed var(--danger); } .fui-cred .row.removing .fname{ text-decoration:line-through; }
.fui-cred .row.pending{ border-color:#5865e6; box-shadow:inset 0 0 0 1px rgba(88,101,230,.35); }
.fui-cred .factcol{ display:flex; flex-direction:column; align-items:flex-end; gap:1px; margin-left:auto; }
.fui-cred .lnk.ldelete{ color:var(--danger); } .fui-cred .lnk.ldelete:hover{ text-decoration:underline; background:none; }
@keyframes fui-afstart{ from{opacity:1;} to{opacity:1;} }
.fui-cred input:-webkit-autofill{ animation-name:fui-afstart; animation-duration:.01s; }
.fui-cred input:autofill{ animation-name:fui-afstart; animation-duration:.01s; }
.fui-cred .inlinepw{ flex:1; min-width:0; display:flex; flex-direction:column; gap:8px; }
.fui-cred .inlinepw .fname{ font-weight:600; }
.fui-cred .inlinerow{ display:flex; gap:8px; align-items:center; }
.fui-cred .inlineinput{ flex:1; min-width:0; background:#0b111b; border:1px solid var(--line2); border-radius:8px; color:var(--ink); padding:8px 10px; font-size:14px; }
.fui-cred .inlineinput:focus{ outline:none; border-color:var(--accent); }
.fui-cred .inlinego{ flex:none; padding:8px 16px; }
.fui-cred .inlinefoot{ display:flex; align-items:center; gap:12px; min-height:16px; }
.fui-cred .inlinemsg{ display:inline-flex; align-items:center; gap:5px; font-size:12px; }
.fui-cred .inlinemsg svg{ width:14px; height:14px; }
.fui-cred .inlinemsg.fail{ color:var(--danger); }
.fui-cred .inlineok{ display:inline-flex; align-items:center; gap:8px; color:#34d399; font-weight:600; font-size:14px; padding:4px 0; }
.fui-cred .inlineok svg{ width:20px; height:20px; }
.fui-cred .lnk.lcancel{ color:var(--dim); margin-left:auto; } .fui-cred .lnk.lcancel:hover{ text-decoration:underline; background:none; }
.fui-cred .namewrap{ display:inline-flex; align-items:center; gap:5px; }
.fui-cred .editbtn{ display:inline-flex; align-items:center; justify-content:center; width:16px; height:16px; padding:0; background:none; border:none; color:var(--dim); cursor:pointer; }
.fui-cred .editbtn svg{ width:16px; height:16px; }
.fui-cred .editbtn:hover{ color:#fff; }
.fui-cred .renameinput{ background:#0b111b; border:1px solid var(--accent); border-radius:6px; color:var(--ink); font-size:inherit; font-weight:inherit; font-family:inherit; padding:2px 7px; max-width:170px; }
.fui-cred .renameinput:focus{ outline:none; }
.fui-cred .renamewrap{ display:inline-flex; align-items:center; gap:2px; }
.fui-cred .renameok, .fui-cred .renamecancel{ display:inline-flex; align-items:center; justify-content:center; width:20px; height:20px; padding:0; background:none; border:none; cursor:pointer; }
.fui-cred .renameok svg, .fui-cred .renamecancel svg{ width:16px; height:16px; }
.fui-cred .renameok{ color:#34d399; } .fui-cred .renameok:hover{ color:#4ade80; }
.fui-cred .renamecancel{ color:var(--danger); }
.fui-cred .sibtie{ display:inline-flex; align-items:center; gap:5px; margin-top:3px; color:#8b93a7; font-size:11px; }
.fui-cred .sibtie svg{ width:12px; height:12px; opacity:.8; }
.fui-cred .enrollbtn{ display:inline-flex; align-items:center; gap:5px; white-space:nowrap; background:rgba(52,211,153,.12); color:#34d399; border:1px solid rgba(52,211,153,.4); border-radius:7px; padding:5px 9px; font-size:12px; font-weight:600; cursor:pointer; }
.fui-cred .enrollbtn:hover{ background:rgba(52,211,153,.2); }
.fui-cred .enrollbtn svg{ width:13px; height:13px; }
.fui-cred .trashbtn{ width:30px; height:30px; border-radius:8px; border:1px solid var(--line2); background:#0e1420; color:var(--faint); display:grid; place-items:center; cursor:pointer; } .fui-cred .trashbtn svg{ width:15px; height:15px; } .fui-cred .trashbtn.locked{ opacity:.38; cursor:not-allowed; }
@media (hover:hover){ .fui-cred .trashbtn:hover{ color:var(--danger); border-color:var(--danger); background:#3b1414; } }
.fui-cred .facts{ display:flex; align-items:center; gap:2px; margin-left:auto; }
.fui-cred .lnk{ background:none; border:none; color:var(--dim); font-size:12px; cursor:pointer; padding:6px 8px; border-radius:8px; } .fui-cred .lnk:hover{ color:var(--ink); background:#0d1420; }
.fui-cred .btn{ font:inherit; font-weight:600; cursor:pointer; border-radius:10px; padding:9px 13px; border:1px solid var(--line2); display:inline-flex; align-items:center; gap:7px; justify-content:center; } .fui-cred .btn svg{ width:15px; height:15px; }
.fui-cred .btn-ghost{ background:#0d1420; color:var(--ink); } .fui-cred .btn-ghost:hover{ background:#131c2c; }
.fui-cred .btn-primary{ background:var(--accent); color:#fff; border-color:var(--accent); } .fui-cred .btn-primary:hover{ background:#5457e6; } .fui-cred .btn:disabled{ opacity:.45; cursor:default; }
.fui-cred .locked{ opacity:.38; cursor:not-allowed; }
.fui-cred .commitbar{ display:flex; gap:10px; padding:12px 14px; border-top:1px solid var(--line2); background:#0d1420; flex:none; } .fui-cred .commitbar .btn-primary{ flex:1; }
.fui-cred .deep{ padding:16px 16px 18px; overflow-y:auto; } .fui-cred .deep .lead{ font-size:13px; color:var(--dim); margin:0 0 14px; }
.fui-cred .modeseg{ display:flex; gap:8px; margin:2px 0 14px; }
.fui-cred .modeopt{ flex:1; border:1px solid var(--line2); border-radius:12px; padding:12px; cursor:pointer; }
.fui-cred .modeopt.sel{ border-color:var(--mfa); background:#0b1a28; }
.fui-cred .modeopt .mo-t{ font-weight:650; font-size:13px; } .fui-cred .modeopt .mo-d{ font-size:11.5px; color:var(--dim); margin-top:3px; }
.fui-cred .pickgroup{ margin:4px 0 10px; } .fui-cred .pickgroup .lbl{ font-size:11px; letter-spacing:.1em; text-transform:uppercase; color:var(--faint); margin:10px 2px 6px; }
.fui-cred .pickrow{ display:flex; align-items:center; gap:10px; padding:9px 10px; border:1px solid var(--line2); border-radius:11px; margin:6px 0; }
.fui-cred .pkchk{ width:18px; height:18px; flex:none; cursor:pointer; }
.fui-cred .pkchk.locked{ cursor:default; opacity:.9; }
.fui-cred .pkchk .check{ width:18px; height:18px; border-radius:5px; border:2px solid var(--line2); display:grid; place-items:center; }
.fui-cred .pkchk.sel .check{ border-color:var(--mfa); background:var(--mfa); }
.fui-cred .pkchk.sel .check::after{ content:""; width:5px; height:9px; border:2px solid #04121c; border-top:0; border-left:0; transform:rotate(45deg) translate(-1px,-1px); }
.fui-cred .pkicon{ width:30px; height:30px; border-radius:8px; flex:none; display:grid; place-items:center; background:#0b1220; border:1px solid var(--line2); color:var(--accent2); } .fui-cred .pkicon svg{ width:16px; height:16px; }
.fui-cred .pkname{ font-weight:600; font-size:14px; flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.fui-cred .unlockbadge{ flex:none; white-space:nowrap; font-size:12px; font-weight:600; border-radius:999px; padding:6px 12px; cursor:pointer; background:rgba(52,211,153,.14); color:#34d399; border:1px solid rgba(52,211,153,.45); }
.fui-cred .unlockbadge:hover{ background:rgba(52,211,153,.22); }
.fui-cred .unlockbadge.off{ background:#161c2a; color:#8b93a7; border-color:var(--line2); }
.fui-cred .mfadyn{ border:1px solid #1e5b7e; background:#0b1a28; border-radius:12px; padding:13px; font-size:12.5px; line-height:1.5; color:#bfe3f5; margin:12px 0 4px; }
.fui-cred .authchanges{ border:1px solid #33406b; background:#0f1a2e; border-radius:12px; padding:13px; font-size:12.5px; line-height:1.5; color:#dbe4ff; margin:2px 0 14px; }
.fui-cred .pkbtn{ width:100%; display:flex; align-items:center; justify-content:center; gap:10px; background:#0f1a2e; border:1px solid #33406b; color:#dbe4ff; font-weight:650; border-radius:12px; padding:14px; cursor:pointer; } .fui-cred .pkbtn:hover{ background:#14213a; } .fui-cred .pkbtn svg{ width:17px; height:17px; }
.fui-cred .or{ display:flex; align-items:center; gap:10px; color:var(--faint); font-size:11px; text-transform:uppercase; letter-spacing:.1em; margin:12px 2px; } .fui-cred .or::before,.fui-cred .or::after{ content:""; height:1px; background:var(--line); flex:1; }
.fui-cred .field label{ font-size:12px; color:var(--dim); } .fui-cred .field input{ width:100%; margin-top:5px; background:var(--panel2); border:1px solid var(--line2); color:var(--ink); border-radius:10px; padding:11px 12px; font:inherit; } .fui-cred .field input:focus{ outline:none; border-color:var(--accent2); }
.fui-cred .authrow{ display:flex; gap:8px; margin-top:16px; } .fui-cred .authrow .btn{ flex:1; }
.fui-cred .spin{ width:15px; height:15px; border-radius:50%; border:2px solid rgba(255,255,255,.35); border-top-color:#fff; display:inline-block; animation:fui-sp .7s linear infinite; } @keyframes fui-sp{ to{ transform:rotate(360deg); } }
.fui-cred .verifying{ display:flex; align-items:center; justify-content:center; gap:9px; padding:30px 0; color:var(--dim); }
.fui-cred .toast{ position:absolute; left:50%; bottom:16px; transform:translateX(-50%); z-index:40; max-width:88%; text-align:center; background:#0f1a2e; border:1px solid #33406b; color:#dbe4ff; font-size:12.5px; padding:8px 14px; border-radius:14px; }
.fui-cred .warnbubble{ position:fixed; z-index:60; transform:translate(-50%,calc(-100% - 12px)); background:#2b1616; border:1px solid #5b2626; color:#fca5a5; font-size:11.5px; font-weight:600; line-height:1.35; padding:7px 11px; border-radius:10px; max-width:230px; box-shadow:0 8px 24px rgba(0,0,0,.55); cursor:pointer; }
.fui-cred .warnbubble::after{ content:""; position:absolute; left:50%; bottom:-5px; transform:translateX(-50%) rotate(45deg); width:8px; height:8px; background:#2b1616; border-right:1px solid #5b2626; border-bottom:1px solid #5b2626; }
`;

const SYMBOLS = `<svg width="0" height="0" style="position:absolute"><defs>
  <symbol id="i-key" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="7.5" cy="15.5" r="4.5"/><path d="M10.7 12.3 21 2m-4 0 3 3m-6 0 3 3"/></symbol>
  <symbol id="i-passkey" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="8" r="4"/><path d="M10.3 14C6 14 3 16.5 3 20m14-6v7m0-7 2.5 1.5M17 17l2.5-1.5M17 20l2.3 1.4"/></symbol>
  <symbol id="i-plus" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></symbol>
  <symbol id="i-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></symbol>
  <symbol id="i-shield" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z"/></symbol>
  <symbol id="i-x" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></symbol>
  <symbol id="i-trash" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2M6 7l1 13a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-13M10 11v6M14 11v6"/></symbol>
  <symbol id="i-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 6.5"/></symbol>
  <symbol id="i-edit" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></symbol>
  <symbol id="i-hash" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3 7 21M17 3l-2 18M4 8.5h16M3.5 15.5h16"/></symbol>
  <symbol id="i-print" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9V2h12v7M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><path d="M6 14h12v8H6z"/></symbol>
  <symbol id="i-scan" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7V5a2 2 0 0 1 2-2h2M17 3h2a2 2 0 0 1 2 2v2M21 17v2a2 2 0 0 1-2 2h-2M7 21H5a2 2 0 0 1-2-2v-2"/><rect x="8" y="8" width="8" height="8" rx="1"/></symbol>
  <linearGradient id="armorGrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#b3bcff"/><stop offset="1" stop-color="#4d59d6"/></linearGradient>
</defs></svg>`;

// ── markup: the design's credentials frame, verbatim ───────────────────────
const MARKUP = `
<div class="credframe">
  <div class="scrhead">
    <div class="ttl"><h1>Manage credentials</h1><div class="sub"><span x-text="identity.name"></span> · <span x-text="factorCount"></span> factors</div></div>
    <button class="closex" @click="back()" aria-label="Close">&times;</button></div>
  <div class="credcontent">

  <template x-if="loading"><div class="body"><p class="deep lead">Loading…</p></div></template>
  <template x-if="loadError"><div class="body"><p class="deep lead" x-text="loadError"></p></div></template>
  <template x-if="migrationPending"><div class="body"><p class="deep lead">Your credentials need a one-time upgrade. Sign out and sign back in with your password to finish it, then come back here.</p></div></template>

  <template x-if="ready && cur==='credentials'"><div class="body">
    <div class="mfacard" :class="{pending:mfaChanged}">
      <div class="ficon"><svg><use xlink:href="#i-shield"/></svg></div>
      <div class="mi"><div class="mt">Multi-factor authentication</div>
        <div class="md" x-show="!mfaOn">Require multiple factors to allow full authority.</div>
        <div class="md" x-show="mfaOn && mfaMode==='any'">Any one of your passwords <b>and</b> any one of your passkeys together provide full authority.</div>
        <div class="md" x-show="mfaOn && mfaMode==='specific'">Any <b>selected</b> password <b>and</b> any <b>selected</b> passkey together provide full authority.</div></div>
      <button class="addbtn" x-show="!mfaOn" @click="startMfaSetup()">Set up</button>
      <div class="mfaactions" x-show="mfaOn"><span class="mfaenabled">Enabled</span><button class="lnk lchange" @click="startMfaSetup()">Configure</button><button class="lnk ldisable" @click="disableMfa()">Disable</button></div>
    </div>

    <template x-if="!hasRecovery"><div class="mfacard reccard">
      <div class="ficon"><svg><use xlink:href="#i-hash"/></svg></div>
      <div class="mi"><div class="mt">Recovery code</div>
        <div class="md">Recovery codes are an essential means to protecting your account. Generate one now.</div></div>
      <button class="addbtn rec" @click="startRecovery()">Set up</button>
    </div></template>

    <div class="grouphead"><span class="grouplbl">Passwords</span>
      <button class="addbtn" @click="addPassword()"><svg><use xlink:href="#i-plus"/></svg> Add</button></div>
    <template x-for="p in passwords" :key="p.id">
      <div class="row" :class="{removing:statusOf(p)==='removed', pending:statusOf(p)==='new'||statusOf(p)==='changed'}">
        <div class="authcell" :class="authCls(p)" @click="authCellClick(p,$event)"><span class="ac-ico"><svg><use xlink:href="#i-key"/></svg><template x-if="inMfaRoot(p)"><span class="mfab" :class="{dim:signinOff(p)}"><svg class="armor" viewBox="0 0 24 24"><path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z"/></svg></span></template></span><span class="ac-lbl" x-text="authWord(p)"></span></div>
        <div class="fmid" x-show="inlineFor!==p.id"><div class="fname">
          <span class="namewrap" x-show="renameFor!==p.id"><span x-text="p.label"></span><button class="editbtn" @click="startRename(p)" aria-label="Rename"><svg><use xlink:href="#i-edit"/></svg></button></span>
          <span class="renamewrap" x-show="renameFor===p.id">
            <input class="renameinput" x-model="renameVal" @keydown.enter="saveRename(p)" @keydown.escape="cancelRename()" x-effect="renameFor===p.id&&setTimeout(()=>$el.focus(),0)">
            <button class="renameok" @click="saveRename(p)" aria-label="Save"><svg><use xlink:href="#i-check"/></svg></button>
            <button class="renamecancel" @click="cancelRename()" aria-label="Cancel"><svg><use xlink:href="#i-x"/></svg></button></span></div>
          <div class="fmeta"><div x-text="p.kdf+' · '+itersLabel(p.iterations)"></div><div x-text="createdLocal(p.created)"></div></div></div>
        <div class="facts factcol" x-show="inlineFor!==p.id && renameFor!==p.id"><template x-if="statusOf(p)==='removed'"><button class="lnk lchange" @click="undoRemove(p)">Undo</button></template>
          <template x-if="statusOf(p)!=='removed'"><span style="display:flex;flex-direction:column;align-items:flex-end">
            <button class="lnk lchange" @click="changePassword(p)">Change</button>
            <button class="lnk lverify" @click="verifyPassword(p)">Verify</button>
            <button class="lnk ldelete" :class="{locked:!canRemove(p)}" @click="remove(passwords,p,$event)">Delete</button></span></template></div>
        <div class="inlinepw" x-show="inlineFor===p.id">
          <div class="fname" x-text="inlineTitle()"></div>
          <template x-if="inlineResult==='ok'">
            <div class="inlineok"><svg><use xlink:href="#i-check"/></svg> <span x-text="inlineMode==='verify'?'Verified':'Password changed'"></span></div></template>
          <template x-if="inlineResult!=='ok'"><div>
            <div class="inlinerow">
              <input x-show="inlineMode!=='confirm'" type="password" class="inlineinput" x-model="inlineVal"
                :autocomplete="inlineMode==='verify'?'current-password':'new-password'"
                :placeholder="inlineMode==='verify'?'Password':'New password'"
                @animationstart="if($event.animationName==='fui-afstart') inlineAutofilled=true"
                @keydown="if($event.key&&$event.key.length===1) inlineAutofilled=false"
                @keydown.enter="submitInline(p)"
                x-effect="inlineFor===p.id&&inlineMode!=='confirm'&&setTimeout(()=>$el.focus(),0)">
              <input x-show="inlineMode==='confirm'" type="password" class="inlineinput" x-model="inlineVal2"
                autocomplete="new-password" placeholder="Re-enter new password" @keydown.enter="submitInline(p)"
                x-effect="inlineFor===p.id&&inlineMode==='confirm'&&setTimeout(()=>$el.focus(),0)">
              <button class="btn btn-primary inlinego" :disabled="inlineMode==='confirm'?!inlineVal2:!inlineVal" @click="submitInline(p)" x-text="inlineBtnText()"></button></div>
            <div class="inlinefoot">
              <template x-if="inlineResult==='fail'"><span class="inlinemsg fail"><svg><use xlink:href="#i-x"/></svg> Incorrect — try again</span></template>
              <template x-if="inlineResult==='mismatch'"><span class="inlinemsg fail"><svg><use xlink:href="#i-x"/></svg> Those don't match — re-enter</span></template>
              <button class="lnk lcancel" @click="cancelInline()">Cancel</button></div></div></template></div></div>
    </template>

    <div class="grouphead"><span class="grouplbl">Passkeys</span>
      <button class="addbtn" @click="startAddPasskey()"><svg><use xlink:href="#i-plus"/></svg> Add</button></div>
    <template x-for="k in passkeys" :key="k.id">
      <div class="row" :class="{removing:statusOf(k)==='removed', pending:statusOf(k)==='new'||statusOf(k)==='changed'}">
        <div class="authcell" :class="authCls(k)" @click="authCellClick(k,$event)"><span class="ac-ico"><svg><use xlink:href="#i-passkey"/></svg><template x-if="inMfaRoot(k)"><span class="mfab" :class="{dim:signinOff(k)}"><svg class="armor" viewBox="0 0 24 24"><path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z"/></svg></span></template></span><span class="ac-lbl" x-text="authWord(k)"></span></div>
        <div class="fmid"><div class="fname">
          <span class="namewrap" x-show="renameFor!==k.id"><span x-text="k.label"></span><button class="editbtn" @click="startRename(k)" aria-label="Rename"><svg><use xlink:href="#i-edit"/></svg></button></span>
          <span class="renamewrap" x-show="renameFor===k.id">
            <input class="renameinput" x-model="renameVal" @keydown.enter="saveRename(k)" @keydown.escape="cancelRename()" x-effect="renameFor===k.id&&setTimeout(()=>$el.focus(),0)">
            <button class="renameok" @click="saveRename(k)" aria-label="Save"><svg><use xlink:href="#i-check"/></svg></button>
            <button class="renamecancel" @click="cancelRename()" aria-label="Cancel"><svg><use xlink:href="#i-x"/></svg></button></span></div>
          <div class="fmeta"><div x-text="transportHuman(k)"></div><div x-text="createdLocal(k.created)"></div></div></div>
        <div class="facts factcol" x-show="renameFor!==k.id"><template x-if="statusOf(k)==='removed'"><button class="lnk lchange" @click="undoRemove(k)">Undo</button></template>
          <template x-if="statusOf(k)!=='removed'"><button class="trashbtn" :class="{locked:!canRemove(k)}" @click="remove(passkeys,k,$event)" aria-label="Remove"><svg><use xlink:href="#i-trash"/></svg></button></template></div></div>
    </template>

    <template x-if="hasRecovery"><div>
      <div class="grouphead"><span class="grouplbl">Recovery code</span></div>
      <div class="row">
        <div class="authcell au-rec"><span class="ac-ico"><svg><use xlink:href="#i-hash"/></svg></span><span class="ac-lbl">Emergency</span></div>
        <div class="fmid"><div class="fname">Recovery code</div>
          <div class="fmeta"><span x-text="recoveryCreated ? ('Generated '+createdLocal(recoveryCreated)) : 'Enrolled'"></span></div></div>
        <div class="facts factcol"><button class="lnk lverify" @click="startVerifyRecovery()">Verify</button></div>
      </div>
    </div></template>
  </div></template>

  <!-- ADD PASSKEY -->
  <template x-if="ready && cur==='addpk'"><div style="display:flex;flex-direction:column;min-height:0">
    <div class="scrhead"><button class="back" @click="back()"><svg style="width:16px;height:16px"><use xlink:href="#i-chev"/></svg></button>
      <div class="ttl"><h1>Add a passkey</h1><div class="sub">On this device</div></div></div>
    <div class="deep">
      <p class="lead">Approve with this device’s screen lock — fingerprint, face, or PIN.</p>
      <button class="pkbtn" @click="usePasskey()"><svg><use xlink:href="#i-passkey"/></svg> Use your passkey</button>
      <div class="authrow"><button class="btn btn-ghost" @click="back()">Cancel</button></div>
    </div>
  </div></template>

  <!-- MFA SETUP (two modes) -->
  <template x-if="ready && cur==='mfa-setup'"><div style="display:flex;flex-direction:column;min-height:0">
    <div class="scrhead"><button class="back" @click="back()"><svg style="width:16px;height:16px"><use xlink:href="#i-chev"/></svg></button>
      <div class="ttl"><h1>Set up multi-factor</h1><div class="sub">Full authority will require a password and a passkey</div></div></div>
    <div class="deep">
      <div class="modeseg">
        <div class="modeopt" :class="{sel:pickMode==='any'}" @click="pickMode='any'"><div class="mo-t">Any one of each</div><div class="mo-d">Any password + any passkey</div></div>
        <div class="modeopt" :class="{sel:pickMode==='specific'}" @click="pickMode='specific'"><div class="mo-t">Specific one(s) of each</div><div class="mo-d">Only the ones you check</div></div>
      </div>

      <div class="pickgroup"><div class="lbl">Passwords</div>
        <template x-for="p in passwords" :key="p.id">
          <div class="pickrow">
            <div class="pkchk" :class="{sel:pickedPw(p), locked:pickMode==='any'}" @click="pickMode==='specific'&&togglePick('pickPws',p.id)"><div class="check"></div></div>
            <span class="pkicon"><svg><use xlink:href="#i-key"/></svg></span>
            <span class="pkname" x-text="p.label"></span>
            <button class="unlockbadge" :class="{off:p.signin===false}" @click.stop="p.signin=(p.signin===false)" x-text="p.signin===false?'No Dashboard Unlock':'Unlocks Dashboard'"></button>
          </div>
        </template>
      </div>
      <div class="pickgroup"><div class="lbl">Passkeys</div>
        <template x-for="k in passkeys" :key="k.id">
          <div class="pickrow">
            <div class="pkchk" :class="{sel:pickedPk(k), locked:pickMode==='any'}" @click="pickMode==='specific'&&togglePick('pickPks',k.id)"><div class="check"></div></div>
            <span class="pkicon"><svg><use xlink:href="#i-passkey"/></svg></span>
            <span class="pkname" x-text="k.label"></span>
            <button class="unlockbadge" :class="{off:k.signin===false}" @click.stop="k.signin=(k.signin===false)" x-text="k.signin===false?'No Dashboard Unlock':'Unlocks Dashboard'"></button>
          </div>
        </template>
      </div>

      <div class="mfadyn" x-text="mfaSetupText()"></div>

      <div class="authrow"><button class="btn btn-ghost" @click="back()">Cancel</button>
        <button class="btn btn-primary" :disabled="!canEnableMfa" @click="enableMfa()">Enable multi-factor</button></div>
    </div>
  </div></template>

  <!-- NEW PASSWORD -->
  <template x-if="ready && cur==='newpw'"><div style="display:flex;flex-direction:column;min-height:0">
    <div class="scrhead"><button class="back" @click="back()"><svg style="width:16px;height:16px"><use xlink:href="#i-chev"/></svg></button>
      <div class="ttl"><h1 x-text="top.title"></h1></div></div>
    <div class="deep">
      <p class="lead">Enter your new password. It’s staged with your other changes — you’ll commit and authorize them together.</p>
      <div class="field"><label>New password</label>
        <input type="password" x-model="newPw" autocomplete="new-password" placeholder="New password" @keydown.enter="newpwContinue()"></div>
      <div class="authrow"><button class="btn btn-ghost" @click="back()">Cancel</button>
        <button class="btn btn-primary" :disabled="!newPw" @click="newpwContinue()">Set password</button></div>
    </div>
  </div></template>

  <!-- NEW DEVICE (a synced passkey's first sign-in from this device) -->
  <template x-if="ready && cur==='newdevice'"><div style="display:flex;flex-direction:column;min-height:0">
    <div class="scrhead"><button class="back" @click="back()"><svg style="width:16px;height:16px"><use xlink:href="#i-chev"/></svg></button>
      <div class="ttl"><h1>First-time login on this device</h1><div class="sub">New device recognized</div></div></div>
    <div class="deep">
      <p class="lead" x-show="top.enrolled">Your factor has already been upgraded and is available for secure vault access.</p>
      <p class="lead" x-show="!top.enrolled">A new authorized device has been detected. You must approve full enrollment before this device can access your secure data.</p>
      <div class="field"><label>Device name</label>
        <input type="text" x-model="deviceName" @keydown.enter="newDeviceOk()"
          x-effect="cur==='newdevice'&&setTimeout(()=>$el.select(),0)"></div>
      <template x-if="newDevErr"><p class="lead" style="color:var(--danger);margin-top:12px" x-text="newDevErr"></p></template>
      <div class="authrow"><button class="btn btn-ghost" @click="back()">Not now</button>
        <button class="btn btn-primary" :disabled="committing || !deviceName" @click="newDeviceOk()" x-text="top.enrolled ? 'OK' : 'Enroll factor'"></button></div>
    </div>
  </div></template>

  <!-- RECOVERY: explanation -->
  <template x-if="ready && cur==='recovery-explain'"><div style="display:flex;flex-direction:column;min-height:0">
    <div class="scrhead"><button class="back" @click="back()"><svg style="width:16px;height:16px"><use xlink:href="#i-chev"/></svg></button>
      <div class="ttl"><h1>Recovery code</h1><div class="sub">Your last way back in</div></div></div>
    <div class="deep exp">
      <p>Autonomy is <b>self-sovereign</b>: no company holds your keys, and no support line can reset your password. That is what keeps your data yours — and it means <b>you</b> are the only one who can get back in.</p>
      <p>Your <b>recovery code</b> is the last way in if you ever lose every password and passkey. Printed and kept offline, it opens your identity again and lets you set a new password.</p>
      <p>It also <b>protects you from takeover</b>: because the code lives offline — not on any device an attacker can reach — it can re-secure your account even if someone steals a password or passkey.</p>
      <div class="recwarn"><b>Keep it offline.</b> Print it and store it somewhere safe. Anyone who has it can recover your identity, and it cannot be re-created if you lose it.</div>
      <button class="btn btn-rec" style="width:100%;margin-top:14px" @click="generateRecoveryCode()">Generate recovery code</button>
    </div>
  </div></template>

  <!-- RECOVERY: present the code once -->
  <template x-if="ready && cur==='recovery-present'"><div style="display:flex;flex-direction:column;min-height:0">
    <div class="scrhead"><div class="ttl"><h1>Your recovery code</h1><div class="sub">Print it and keep it offline</div></div></div>
    <div class="deep">
      <p class="lead">Print this and keep it somewhere safe and offline.</p>
      <div class="recrow">
        <div class="recqr" x-html="recoveryQr"></div>
        <div class="reccode" x-text="recoveryPrintable"></div>
      </div>
      <div class="authrow">
        <button class="btn btn-ghost" @click="printRecovery()"><svg><use xlink:href="#i-print"/></svg> Print</button>
        <button class="btn btn-rec" @click="confirmRecoverySaved()">I've saved it</button>
      </div>
      <div class="recwarn"><b>Important:</b> This code cannot be shown again after this enrollment process completes, and you will <b><i>not</i></b> be able to create a new recovery code if you lose this one!</div>
    </div>
  </div></template>

  <!-- RECOVERY: verify (mandatory in the wizard; read-only for an enrolled code) -->
  <template x-if="ready && cur==='recovery-verify'"><div style="display:flex;flex-direction:column;min-height:0">
    <div class="scrhead"><button class="back" @click="back()"><svg style="width:16px;height:16px"><use xlink:href="#i-chev"/></svg></button>
      <div class="ttl"><h1>Verify your recovery code</h1><div class="sub" x-text="top.wizard ? 'Confirm the copy you saved' : 'Confirm it still works'"></div></div></div>
    <div class="deep">
      <template x-if="recoveryVerifyResult==='ok' && !top.wizard"><div class="inlineok" style="justify-content:center;padding:20px 0"><svg><use xlink:href="#i-check"/></svg> Recovery code verified</div></template>
      <template x-if="!(recoveryVerifyResult==='ok' && !top.wizard)"><div>
        <p class="lead" x-text="top.wizard ? 'Scan or type the copy you just saved. You must verify it before it is enrolled.' : 'Scan or type your recovery code. Nothing changes — this only checks it.'"></p>
        <div class="choice" x-show="!recoveryScanning">
          <button class="btn btn-ghost" @click="startRecoveryScan()"><svg><use xlink:href="#i-scan"/></svg> Scan the QR code</button>
        </div>
        <div class="field" style="margin-top:12px"><label>Enter the code</label>
          <input type="text" x-model="recoveryInput" spellcheck="false" autocapitalize="off" placeholder="Recovery code"
            @keydown.enter="submitVerifyRecovery(recoveryInput)"></div>
        <template x-if="recoveryVerifyResult==='fail'"><p class="lead" style="color:var(--danger);margin-top:10px" x-text="top.wizard ? 'That does not match the code shown. Check your saved copy.' : 'That code did not open your identity — check it and try again.'"></p></template>
        <template x-if="recoveryVerifyResult==='malformed'"><p class="lead" style="color:var(--danger);margin-top:10px">That is not a valid recovery code — check the characters.</p></template>
        <div class="authrow">
          <template x-if="top.wizard"><button class="btn btn-ghost" @click="seeCodeAgain()">Show my code again</button></template>
          <template x-if="!top.wizard"><button class="btn btn-ghost" @click="back()">Cancel</button></template>
          <button class="btn btn-rec" :disabled="!recoveryInput || committing" @click="submitVerifyRecovery(recoveryInput)">Verify</button>
        </div>
      </div></template>
    </div>
  </div></template>

  <!-- AUTHORIZE (commit) -->
  <template x-if="ready && cur==='authorize'"><div style="display:flex;flex-direction:column;min-height:0">
    <div class="scrhead"><button class="back" @click="back()" x-show="!verifying"><svg style="width:16px;height:16px"><use xlink:href="#i-chev"/></svg></button>
      <div class="ttl"><h1>Authorize with your root authority</h1><div class="sub" x-text="top.detail"></div></div>
      <template x-if="top.step"><span class="step" x-text="top.step"></span></template></div>
    <div class="deep">
      <template x-if="!verifying"><div>
        <template x-if="top.lines && top.lines.length"><div class="authchanges">
          <div style="font-weight:700;margin-bottom:4px">You are authorizing:</div>
          <template x-for="ln in top.lines" :key="ln"><div style="margin:2px 0">• <span x-text="ln"></span></div></template>
        </div></template>
        <template x-if="authBoth"><p class="lead">Multi-factor is on — approve with your passkey <b>and</b> your password.</p></template>
        <template x-if="authPkDone"><div class="inlineok" style="width:100%;justify-content:center;padding:14px 0"><svg><use xlink:href="#i-check"/></svg> Passkey authorized</div></template>
        <template x-if="authPk && !authPkDone"><button class="pkbtn" @click="authWithPasskey()"><svg><use xlink:href="#i-passkey"/></svg> Use your passkey</button></template>
        <template x-if="(authPk || authPkDone) && (authPw || authPwDone)"><div class="or" x-text="authBoth?'and':'or'"></div></template>
        <template x-if="authPwDone"><div class="field"><label style="visibility:hidden">Enter your current password</label>
          <div class="inlineok" style="margin-top:5px;padding:11px 0"><svg><use xlink:href="#i-check"/></svg> Password verified</div></div></template>
        <template x-if="authPw && !authPwDone"><div class="field"><label>Enter your current password</label>
          <input type="password" x-model="password" autocomplete="current-password" placeholder="Current password"
            @animationstart="if($event.animationName==='fui-afstart'){ authAutofilled=true; setTimeout(()=>authWithPassword(),0); }"
            @keydown="if($event.key&&$event.key.length===1) authAutofilled=false"
            @keydown.enter="authWithPassword()"></div></template>
        <template x-if="authShowMissing || authDeadEnd"><div class="mfadyn">
          <template x-if="authDeadEnd"><div style="font-weight:700;margin-bottom:6px">You can’t authorize on this device.</div></template>
          <div x-text="authMissingLead"></div>
          <template x-for="mp in authMissingPasskeys" :key="mp.label">
            <div style="margin-top:5px">“<span x-text="mp.label"></span>” — enrolled on: <span x-text="mp.devices"></span></div>
          </template>
        </div></template>
        <div class="authrow"><button class="btn btn-ghost" @click="back()">Cancel</button>
          <button class="btn btn-primary" :disabled="!password || authPwDone" @click="authWithPassword()">Authorize</button></div>
      </div></template>
      <template x-if="verifying"><div class="verifying"><span class="spin"></span> Verifying with your root key…</div></template>
    </div>
  </div></template>
  </div>
  <template x-if="ready && cur==='credentials'"><div class="commitbar" x-show="changeCount>0" style="display:none">
    <button class="btn btn-ghost" @click="cancelChanges()">Cancel</button>
    <button class="btn btn-primary" :disabled="committing" @click="commit()">Commit <span x-text="changeCount"></span>&nbsp;<span x-text="changeCount===1?'change':'changes'"></span></button></div></template>

  <template x-if="warnAt"><div class="warnbubble" :style="'left:'+warnAt.x+'px;top:'+warnAt.y+'px'" x-text="warnAt.text" @click="warnAt=null"></div></template>
  <div class="toast" x-show="toast" x-text="toast" x-transition style="display:none"></div>
</div>`;

// ── mount ──────────────────────────────────────────────────────────────────
let overlay = null;
let card = null;
let registered = false;

function injectStyles() {
  if (!document.getElementById('fui-cred-styles')) {
    const el = document.createElement('style');
    el.id = 'fui-cred-styles'; el.textContent = STYLE;
    document.head.appendChild(el);
  }
  if (!document.getElementById('fui-cred-symbols')) {
    const holder = document.createElement('div');
    holder.id = 'fui-cred-symbols'; holder.innerHTML = SYMBOLS;
    document.body.appendChild(holder);
  }
}

function registerComponent() {
  if (registered || !window.Alpine) return;
  window.Alpine.data('fuiCredentials', credentialsPanel);
  registered = true;
}

function close() {
  const cb = hostCallbacks.onClose;
  if (card && card.parentNode) card.parentNode.removeChild(card);
  if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay);
  card = null; overlay = null;
  hostCallbacks = { onBack: null, onClose: null };
  if (cb) cb();
}

async function open(opts) {
  hostCallbacks = {
    onBack: (opts && opts.onBack) || null,
    onClose: (opts && opts.onClose) || close,
  };
  injectStyles();
  if (window.Alpine) registerComponent();
  else document.addEventListener('alpine:init', registerComponent, { once: true });

  card = document.createElement('div');
  card.className = 'fui-cred';
  card.setAttribute('data-testid', 'factor-management');
  card.setAttribute('x-data', 'fuiCredentials');
  card.innerHTML = MARKUP;

  const mount = opts && opts.mount;
  if (mount) {
    overlay = null;
    mount.appendChild(card);
  } else {
    overlay = document.createElement('div');
    overlay.className = 'fui-overlay';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:1100;display:flex;align-items:center;justify-content:center;background:rgba(4,6,11,.72);padding:24px 16px;';
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    card.style.cssText = 'width:100%;max-width:460px;max-height:86vh;border:1px solid #374151;border-radius:14px;overflow:hidden;';
    overlay.appendChild(card);
    document.body.appendChild(overlay);
  }
  // Alpine (already started on dashboard pages) initializes injected trees via
  // its MutationObserver; nothing further to do here.
}

export { open, close };
