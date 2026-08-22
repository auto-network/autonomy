/* Factor authority policy — the browser half of the factor state machine.
 *
 * A DIRECT port of the authority model the operator built in the design
 * `unlock-screen-root-required-flags.html` (the plan of record), and the same
 * machine proven in tools/network/idkit/TLA/FactorAuth.tla. Pure and
 * DOM-free so it is node-testable: every function takes the model `m`
 * explicitly (the design read a module global; here it is a parameter).
 *
 * The one invariant, verbatim from the design: "Every policy state must leave
 * at least one way to reach the root." rootReachable is the only thing that
 * has to be proven, because a root-reaching route is by construction also an
 * unlocking route.
 *
 * `m` shape (mirrors the design's M):
 *   face: {on, full, canUnlock}   // passkey CLASS (on = any enrolled)
 *   pass: {on, full, canUnlock}   // password
 *   mfa:  bool                    // a combined (both-required) factor exists
 *   keys: [{ credentialId, label, prf, provisioningPub, here, auth }]
 *         auth ∈ 'full' | 'unlock'
 */

const NAME = { face: 'Passkey', pass: 'Password', both: 'Multi-Factor' };
const LV = { a: 'unlock only', b: 'full authority', off: 'disabled', en: 'not set up' };
const K = ['face', 'pass', 'both'];

// ── THE INVARIANT ────────────────────────────────────────────────────────
function rootReachable(m) {
  const capable = m.keys.some((k) => k.prf);
  if (m.mfa) return capable && m.pass.on;              // the pair needs both halves
  return m.keys.some((k) => k.prf && k.auth === 'full')
      || (m.pass.on && m.pass.full);
}

function unlockWays(m) {
  const w = [];
  if (m.mfa) {
    if (m.face.canUnlock && m.keys.length) w.push('face');
    if (m.pass.canUnlock && m.pass.on) w.push('pass');
    if (rootReachable(m)) w.push('both');
  } else {
    if (m.keys.length) w.push('face');
    if (m.pass.on) w.push('pass');
  }
  return w;
}

function invalid(m) {
  if (!rootReachable(m)) return 'Nothing would be able to reach your root.';
  if (m.keys.some((k) => !k.prf && k.auth === 'full')) {
    return 'A passkey that cannot derive cannot hold full authority.';
  }
  if (!unlockWays(m).length) return 'Nothing would be able to unlock the dashboard.';
  return null;
}

// Apply an authority choice to a model COPY (the design's applyAuthority):
// f/p = whether the single passkey/password may still unlock; b = pair on.
function applyAuthority(n, f, p, b) {
  n.mfa = !!b;
  if (n.mfa) {
    n.pass.full = false;
    n.face.canUnlock = !!f;
    n.pass.canUnlock = !!p;
    n.keys.forEach((k) => { k.auth = 'unlock'; });
  } else {
    n.pass.full = !!p;
    n.face.canUnlock = true;
    n.pass.canUnlock = true;
    if (!f) {
      n.keys.forEach((k) => { k.auth = 'unlock'; });
    } else if (!n.keys.some((k) => k.prf && k.auth === 'full')) {
      n.keys.forEach((k) => { if (k.prf) k.auth = 'full'; });
    }
  }
  return n;
}

function authoritySig(m) {
  return [m.mfa, m.keys.some((k) => k.prf && k.auth === 'full'),
    m.pass.full, m.face.canUnlock, m.pass.canUnlock].join('|');
}

// An action is offered only when it could actually land somewhere legal —
// otherwise no tap is shown ("no dead taps").
function hasAlternative(m) {
  const cur = authoritySig(m);
  const pairOK = m.pass.on && m.keys.some((k) => k.prf);
  for (let f = 0; f < 2; f += 1) {
    for (let p = 0; p < 2; p += 1) {
      for (let b = 0; b < 2; b += 1) {
        if (b && !pairOK) continue;
        if (!b && f && !m.face.on) continue;
        if (!b && p && !m.pass.on) continue;
        const n = applyAuthority(JSON.parse(JSON.stringify(m)), f, p, b);
        if (invalid(n)) continue;
        if (authoritySig(n) !== cur) return true;
      }
    }
  }
  return false;
}

function anyKeyFull(m) {
  return m.keys.some((x) => x.auth === 'full' && x.prf === true);
}

function level(m, k) {
  if (k === 'both') return m.mfa ? 'b' : 'off';
  if (!m[k].on) return 'en';
  if (m.mfa) return m[k].canUnlock ? 'a' : 'off';   // unlock only, or nothing at all
  if (k === 'face') return anyKeyFull(m) ? 'b' : 'a';
  return m.pass.full ? 'b' : 'a';
}

function actionOn(m, k) {
  if (k === 'both') {
    if (m.mfa) return hasAlternative(m) ? 'change' : null;
    return (m.face.on && m.pass.on && m.keys.some((x) => x.prf)) ? 'enable' : null;
  }
  if (!m[k].on) return 'enroll';
  if (m.mfa) return null;               // a single cannot be raised on its own
  if (!hasAlternative(m)) return null;
  return m[k].full ? 'change' : 'upgrade';
}

function rootFactor(m) {
  if (m.mfa) return 'both';
  if (m.face.on && level(m, 'face') === 'b') return 'face';
  if (m.pass.on && level(m, 'pass') === 'b') return 'pass';
  return null;
}

function narrowest(m) {
  if (m.face.on && level(m, 'face') !== 'off') return 'face';
  if (m.pass.on && level(m, 'pass') !== 'off') return 'pass';
  return m.mfa ? 'both' : null;
}

// ── build the model from real backend data ────────────────────────────────
// status: /api/identity/status ; armorData: parseArmor(/api/identity/personal).
function buildModel(status, armorData) {
  const factors = (armorData && armorData.factors) || [];
  const combined = factors.find((f) => f.type === 'combined') || null;
  const mfa = !!combined;
  const pwStandalone = factors.some((f) => f.type === 'password');
  const rootFactorIds = new Set(
    factors.filter((f) => f.type === 'passkey').map((f) => f.credential_id),
  );
  const rpId = status && status.rp_id;
  const keys = ((status && status.passkeys) || []).map((p) => ({
    credentialId: p.credential_id,
    label: p.label || 'Passkey',
    prf: !!p.provisioning_public_key,
    provisioningPub: p.provisioning_public_key || null,
    here: rpId && p.rp_id === rpId ? 1 : 0,
    // A standalone passkey ROOT factor is 'full'; everything else (access-only,
    // or a passkey folded into the combined factor) is 'unlock'.
    auth: rootFactorIds.has(p.credential_id) ? 'full' : 'unlock',
  }));
  return {
    face: {
      on: keys.length > 0,
      full: keys.some((k) => k.prf && k.auth === 'full'),
      // A passkey credential can always assert for dashboard ACCESS.
      canUnlock: true,
    },
    pass: {
      on: pwStandalone || mfa,
      full: pwStandalone && !mfa,        // a standalone password opens the root
      // In MFA there is no standalone password unlock; otherwise it unlocks.
      canUnlock: !mfa,
    },
    mfa,
    combinedCredentialId: combined ? combined.credential_id : null,
    keys,
  };
}

export {
  NAME, LV, K,
  rootReachable, unlockWays, invalid, applyAuthority, authoritySig,
  hasAlternative, anyKeyFull, level, actionOn, rootFactor, narrowest,
  buildModel,
};
