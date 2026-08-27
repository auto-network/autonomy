/* Shared helpers for the factor-panel test suites: the in-process projection
 * of the factor-policy operation contract (mirrors identity_routes.py
 * _project_factor_policy + _factor_policy_view for test stubs), and the
 * synthetic-view builders the matrix enumeration uses.
 *
 * NOTE these mirror the server so the fast suites can run without Python; the
 * client-side realism comes from buildFactorPolicyArmor/openFactorPolicyArmor
 * validating and proving every produced armor with real crypto.
 */
import { canonicalExpression } from '../static/js/ceremony/root-factor-policy.js';

export function applyOps(state, operations) {
  const factors = new Map(state.factors.map((f) => [f.factor_id, JSON.parse(JSON.stringify(f))]));
  const access = new Set(state.access);
  let policy = state.policy;
  for (const op of operations) {
    if (op.op === 'enroll_password' || op.op === 'enroll_passkey') {
      if (factors.has(op.factor.factor_id)) throw new Error('already enrolled');
      factors.set(op.factor.factor_id, JSON.parse(JSON.stringify(op.factor)));
      if (op.access) access.add(op.factor.factor_id);
    } else if (op.op === 'change_password') {
      if (!factors.has(op.factor_id)) throw new Error('change_password targets no factor');
      factors.set(op.factor_id, JSON.parse(JSON.stringify(op.factor)));
    } else if (op.op === 'add_passkey_recipient') {
      const f = factors.get(op.factor_id);
      if (!f) throw new Error('add_passkey_recipient targets no factor');
      if (f.recipients.some((r) => r.recipient_public_key === op.recipient.recipient_public_key)) {
        throw new Error('recipient already enrolled');
      }
      f.recipients.push(op.recipient);
    } else if (op.op === 'remove_passkey_recipient') {
      const f = factors.get(op.factor_id);
      if (!f) throw new Error('remove_passkey_recipient targets no factor');
      const kept = f.recipients.filter((r) => r.recipient_public_key !== op.recipient_public_key);
      if (kept.length === f.recipients.length) throw new Error('recipient is not enrolled');
      f.recipients = kept;
    } else if (op.op === 'remove_factor') {
      if (!factors.delete(op.factor_id)) throw new Error('factor is not enrolled');
      access.delete(op.factor_id);
    } else if (op.op === 'set_access') {
      if (!factors.has(op.factor_id)) throw new Error('set_access targets no factor');
      if (op.enabled) access.add(op.factor_id); else access.delete(op.factor_id);
    } else if (op.op === 'set_root_policy') {
      policy = canonicalExpression(op.policy);
    } else throw new Error('unknown op ' + op.op);
  }
  // final-state validation, as the server does it: every policy member exists
  // and can derive root material
  const leaves = policyLeaves(policy);
  for (const id of leaves) {
    const f = factors.get(id);
    if (!f) throw new Error('root policy names unknown factor ' + id);
    const recips = f.type === 'password' ? 1 : (f.recipients || []).length;
    if (!recips) throw new Error('factor ' + id + ' cannot derive root material');
  }
  return {
    generation: state.generation + 1,
    factors: [...factors.values()].sort((a, b) => a.factor_id.localeCompare(b.factor_id)),
    access: [...access].sort(),
    policy,
  };
}

export function policyLeaves(policy) {
  const acc = [];
  (function walk(n) {
    if (n.op === 'factor') acc.push(n.factor_id);
    else n.children.forEach(walk);
  }(policy));
  return acc;
}

// A server view row set for a projected state (shape of _factor_policy_view).
export function viewFrom(state, rootPub, passkeyMeta = {}) {
  const memberIds = new Set(policyLeaves(state.policy));
  const anyOne = state.policy.op !== 'and';
  return {
    version: 1,
    armor_version: 3,
    generation: state.generation,
    root_pub: rootPub,
    root_policy: state.policy,
    migration_required: false,
    factors: state.factors.map((f) => {
      const member = memberIds.has(f.factor_id);
      const meta0 = f.type === 'passkey' ? (passkeyMeta[f.credential_id] || {}) : {};
      const row = {
        factor_id: f.factor_id,
        type: f.type,
        label: f.type === 'password' ? 'Password' : (meta0.label || 'Passkey'),
        purpose: null,
        access: state.access.includes(f.factor_id) ? 'enabled' : 'disabled',
        root_role: member ? (anyOne ? 'individual' : 'mfa-member') : 'none',
        capabilities: {},
      };
      if (f.type === 'password') {
        row.kdf = {
          name: 'PBKDF2', hash: 'SHA-256',
          iterations: (f.protector && f.protector.kdf && f.protector.kdf.iterations) || 600000,
        };
      } else {
        const meta = passkeyMeta[f.credential_id] || {};
        Object.assign(row, {
          credential_id: f.credential_id,
          rp_id: meta.rp_id || 'localhost',
          transports: meta.transports || ['internal'],
          created_at: meta.created_at || '2026-08-01T00:00:00Z',
          backup_eligible: !!meta.backed_up,
          backed_up: !!meta.backed_up,
          recipients: f.recipients,
        });
      }
      return row;
    }),
  };
}
