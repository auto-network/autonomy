/* The single, factor-aware "unlock your root and approve this" dialog.
 *
 * ONE common code path for every approval that needs the personal root. It reads
 * the armor's own factor set and presents exactly the openers that armor accepts
 * — a password grouping when a password opens it, a passkey button when a passkey
 * opens it, BOTH (and requires both) under Multi-Factor, and either when either
 * one works so the operator picks. Callers never touch a password field
 * again: they call `openRoot({title, detail})`, get back a
 * ready-to-use root signing key (and the raw seed for ceremonies that need it),
 * sign their specific request, and zero the seed. No per-dialog factor logic.
 *
 * Every factor renders as one grouping in a fixed position: the password
 * grouping is an icon-integrated input with an inline OK, the passkey grouping
 * is one button. Completing a factor turns its grouping green with a check in
 * place; when the policy's required set is green the dialog closes itself.
 *
 * This is deliberately NOT the centralized approval inbox (a separate, larger
 * effort) — it is the reusable unlock element that inbox and every current
 * approval will share.
 */
import * as primitives from './primitives.js';
import { prfEvalExtension, prfOutputFromResults } from './enrollment.js';

const STYLE = `
.or-overlay{position:fixed;inset:0;z-index:1200;display:flex;align-items:center;justify-content:center;
  background:rgba(4,6,11,.72);
  padding:max(24px,env(safe-area-inset-top)) max(16px,env(safe-area-inset-right))
    max(24px,env(safe-area-inset-bottom)) max(16px,env(safe-area-inset-left));
  color:#e5e7eb;font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
.or-card{width:100%;max-width:380px;background:#11141c;border:1px solid #1e2432;border-radius:14px;
  padding:22px 20px 18px;box-shadow:0 20px 50px -12px #000}
.or-ttl{font-size:16px;font-weight:600;text-align:center;margin:0 0 4px}
.or-sub{font-size:12.5px;color:#9aa3b2;text-align:center;margin:0 0 6px;line-height:1.45}
.or-req{font-size:12.5px;color:#c7d2fe;text-align:center;margin:0 0 16px;line-height:1.45;font-weight:500}
.or-factor{position:relative;border:1px solid #232a39;background:#161b26;border-radius:12px;
  padding:9px 12px;transition:border-color .25s ease,background .25s ease}
.or-factor.done{border-color:#14532d;background:#0c1a12}
.or-factor-row{display:flex;align-items:center;gap:10px;min-height:40px}
.or-factor-ic{position:relative;width:30px;height:30px;flex:0 0 30px;display:grid;place-items:center;border-radius:8px;
  background:#1f2937;color:#38bdf8}
.or-pin{position:absolute;top:-5px;right:-5px;width:15px;height:15px;border-radius:50%;
  background:#5b57e8;color:#fff;display:grid;place-items:center;box-shadow:0 0 0 2px #161b26}
.or-factor.done .or-pin{box-shadow:0 0 0 2px #0c1a12}
.or-pin svg{width:9px;height:9px;fill:none;stroke:currentColor;stroke-width:2.4;stroke-linecap:round;stroke-linejoin:round}
.or-factor.done .or-factor-ic{background:#052e16;color:#34d399}
.or-factor-ic svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round}
.or-factor-name{font-size:13.5px;font-weight:600;color:#e5e7eb}
.or-in-bare{flex:1;min-width:0;background:transparent;border:0;outline:none;padding:8px 0;
  font-size:16px;color:#e5e7eb;font-family:inherit}
.or-ok{flex:0 0 auto;background:#5b57e8;color:#fff;padding:9px 16px;border-radius:7px;
  font-size:13.5px;font-weight:600;cursor:pointer}
.or-ok[aria-disabled=true]{opacity:.5;cursor:default}
.or-factor-btn{width:100%;text-align:left;cursor:pointer;color:#e5e7eb;font-family:inherit;
  display:flex;align-items:center;gap:10px;min-height:58px}
.or-factor-btn:hover{border-color:#5b57e8}
.or-factor-btn[aria-disabled=true]{opacity:.6;cursor:default}
.or-factor-slot{margin-bottom:10px}
.or-factor-err{font-size:11.5px;color:#fca5a5;line-height:1.5;margin:6px 0 2px 40px}
.or-warn{font-size:11.5px;color:#fca5a5;background:#2b1616;border:1px solid #4b2222;border-radius:9px;
  padding:10px 12px;line-height:1.5;margin-top:10px}
.or-cancel{font-size:13px;color:#8b93a3;text-align:center;padding:10px;cursor:pointer;margin-top:4px}
.or-cancel:hover{color:#e5e7eb}
`;

const ICONS = {
  password: '<svg viewBox="0 0 24 24"><rect x="4.5" y="10" width="15" height="9.5" rx="2"/><path d="M8 10V7.5a4 4 0 0 1 8 0V10"/><path d="M12 14v2.2"/></svg>',
  passkey: '<svg viewBox="0 0 24 24"><circle cx="10" cy="8.5" r="3.2"/><path d="M4.5 19c.6-3.2 2.9-5 5.5-5s4.9 1.8 5.5 5"/><path d="M17.5 9.5v4M15.6 11.5h3.8"/></svg>',
  check: '<svg viewBox="0 0 24 24"><path d="M5 12.5 10 17.5 19 7"/></svg>',
  pin: '<svg viewBox="0 0 24 24"><path d="M12 21v-7M7 14l1.2-6.5h7.6L17 14ZM9.5 7.5V4h5v3.5"/></svg>',
};

const NO_ROOT_AUTHORITY =
  'The factor you provided can unlock your dashboard but has no root authority.';

function injectStyles() {
  const existing = document.getElementById('open-root-styles');
  if (existing) { existing.textContent = STYLE; return; }
  const el = document.createElement('style');
  el.id = 'open-root-styles'; el.textContent = STYLE;
  document.head.appendChild(el);
}

// The openers an armor actually accepts, read from its own factor set.
async function loadModel() {
  const [st, pj, fp] = await Promise.all([
    fetch('/api/identity/status', {
      credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' },
    }).then((r) => r.json()),
    fetch('/api/identity/personal', {
      credentials: 'same-origin', headers: { Accept: 'application/json' },
    }).then((r) => r.json()),
    fetch('/api/identity/factor-policy', {
      credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' },
    }).then((r) => r.json()).catch(() => null),
  ]);
  if (pj && pj.error) throw new Error(pj.error);
  if (!pj || !pj.armored_private_key) throw new Error('No personal identity is available.');
  if (fp && fp.armor_version === 3 && !fp.error) {
    const policyModule = await import('./root-factor-policy.js');
    const envelope = await policyModule.parseFactorPolicyArmor(pj.armored_private_key);
    return {
      armor: pj.armored_private_key,
      rootPub: pj.root_pub || envelope.root_pub,
      rpId: st.rp_id || undefined,
      passkeys: st.passkeys || [],
      sessionMethod: st.method || null,
      sessionCredentialId: st.session_credential_id || null,
      v3: true,
      policyModule,
      envelope,
      factorViews: fp.factors || [],
    };
  }
  throw new Error("this identity's armor is in a retired format and "
    + 'cannot be opened by this software');
}

function b64u(s) {
  let b = s.replace(/-/g, '+').replace(/_/g, '/'); while (b.length % 4) b += '=';
  const bin = atob(b); const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
  return out;
}

// A WebAuthn PRF assertion over the identity's passkeys (rp-scoped).
async function getPrf(model, credentialIds = null) {
  if (!window.PublicKeyCredential || !navigator.credentials) {
    throw new Error('this browser cannot use a passkey');
  }
  const rpId = model.rpId;
  const wanted = credentialIds ? new Set(credentialIds) : null;
  const allow = (model.passkeys || [])
    .filter((p) => p.credential_id && (!rpId || p.rp_id === rpId)
      && (!wanted || wanted.has(p.credential_id)))
    .map((p) => ({ type: 'public-key', id: b64u(p.credential_id) }));
  let asrt;
  try {
    const operation = () => navigator.credentials.get({
      publicKey: {
        challenge: crypto.getRandomValues(new Uint8Array(32)),
        rpId, allowCredentials: allow, userVerification: 'required',
        extensions: prfEvalExtension(),
      },
    });
    const bracket = window.Autonomy && window.Autonomy.systemAuth;
    asrt = await (bracket && typeof bracket.run === 'function'
      ? bracket.run(operation) : operation());
  } catch (e) {
    if (e && e.name === 'NotAllowedError') throw new Error('Passkey was cancelled — try again');
    throw e;
  }
  const prf = prfOutputFromResults(asrt.getClientExtensionResults());
  if (!prf) throw new Error('this passkey has no PRF and cannot open your root');
  let credentialId = '';
  const raw = new Uint8Array(asrt.rawId || allow[0]?.id || []);
  let binary = ''; for (const byte of raw) binary += String.fromCharCode(byte);
  credentialId = btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  return { prf, credentialId };
}


async function openRootPolicy(model, { title, detail }) {
  const policy = model.policyModule;
  const memberIds = new Set(policy.policyFactorIds(model.envelope.policy));
  // Password grouping renders first regardless of armor order (stable sort
  // keeps the relative order within each type).
  const factors = model.envelope.factors
    .filter((factor) => memberIds.has(factor.factor_id))
    .sort((a, b) => (b.type === 'password' ? 1 : 0) - (a.type === 'password' ? 1 : 0));
  const views = Object.fromEntries((model.factorViews || []).map((row) => [row.factor_id, row]));
  const passwordCount = factors.filter((factor) => factor.type === 'password').length;
  const passkeyCount = factors.filter((factor) => factor.type === 'passkey').length;
  const seeds = {};
  const S = { passwords: {}, errors: {}, busy: null, warn: null };

  return new Promise((resolve) => {
    const host = document.createElement('div');
    host.className = 'or-overlay'; host.setAttribute('data-testid', 'open-root');
    const card = document.createElement('div'); card.className = 'or-card';
    host.appendChild(card); document.body.appendChild(host);

    function cleanFactorSeeds() {
      Object.values(seeds).forEach((seed) => seed?.fill?.(0));
      Object.keys(seeds).forEach((factorId) => { delete seeds[factorId]; });
    }
    function close(result) {
      cleanFactorSeeds();
      if (host.parentNode) host.parentNode.removeChild(host);
      resolve(result);
    }
    function el(tag, cls, html) {
      const node = document.createElement(tag);
      if (cls) node.className = cls;
      if (html != null) node.innerHTML = html;
      return node;
    }
    function label(factor) {
      // The operator's renames live on the RECIPIENT metadata (that is what
      // the credential-management pencil writes); the factor-level label is
      // the stale enrollment-time default. Prefer the recipient names.
      const view = views[factor.factor_id] || {};
      const names = (view.recipients || []).map((r) => r.label).filter(Boolean);
      return names.join(' · ') || view.label
        || (factor.type === 'password' ? 'Password' : 'Passkey');
    }
    // The factor this dashboard session signed in with gets a pin badge.
    function isSessionFactor(factor) {
      if (model.sessionMethod === 'password') {
        return factor.type === 'password' && passwordCount === 1;
      }
      return model.sessionMethod === 'passkey' && !!factor.credential_id
        && factor.credential_id === model.sessionCredentialId;
    }
    function factorIcon(markup, factor) {
      const ic = el('span', 'or-factor-ic', markup);
      if (isSessionFactor(factor)) {
        const pin = el('span', 'or-pin', ICONS.pin);
        pin.title = 'You signed in with this factor';
        ic.appendChild(pin);
      }
      return ic;
    }
    // What the armor actually requires, stated up front. The policy is an
    // and/or expression; the two flat shapes cover every real armor today
    // and anything nested falls back to neutral wording.
    function requirementLine() {
      if (factors.length === 1) {
        return `Your root requires your ${factors[0].type === 'password' ? 'password' : 'passkey'}.`;
      }
      const expression = model.envelope.policy;
      const flat = expression && ['and', 'or'].includes(expression.op)
        && (expression.children || []).every((child) => child.op === 'factor');
      if (flat && expression.op === 'or') {
        return factors.length === 2
          ? 'Your root opens with either factor — complete one.'
          : 'Your root opens with any one factor — complete one.';
      }
      if (flat && expression.op === 'and') {
        return factors.length === 2
          ? 'Your root requires both factors — complete each one.'
          : `Your root requires all ${factors.length} factors — complete each one.`;
      }
      return 'Complete the factor combination required by your current root policy.';
    }
    // The required set is green: leave it visible for a beat, then open the
    // armor and resolve. Opening can still fail (corrupt slot); that surfaces
    // as the global warning with all seeds discarded.
    function finishCollected() {
      S.busy = 'closing'; render();
      setTimeout(async () => {
        try {
          const opened = await policy.openFactorPolicyArmor(model.armor, seeds);
          close(opened);
        } catch (error) {
          cleanFactorSeeds(); S.busy = null;
          S.warn = error?.message || 'Those factors did not open your root.';
          render();
        }
      }, 650);
    }
    function noteCollected() {
      if (policy.policySatisfied(model.envelope.policy, Object.keys(seeds))) {
        finishCollected();
      } else {
        S.busy = null; render();
      }
    }
    async function addPassword(factor) {
      const value = S.passwords[factor.factor_id] || '';
      if (S.busy || !value) return;
      S.busy = factor.factor_id; S.errors = {}; S.warn = null; render();
      try {
        seeds[factor.factor_id] = await policy.openPasswordFactor(
          model.rootPub, factor, value,
        );
        S.passwords[factor.factor_id] = '';
        noteCollected();
      } catch (error) {
        S.busy = null; S.passwords[factor.factor_id] = '';
        S.errors = {
          [factor.factor_id]: error?.message || 'That password did not open this factor.',
        };
        render();
      }
    }
    async function addPasskey(factor) {
      if (S.busy) return;
      S.busy = factor.factor_id; S.errors = {}; S.warn = null; render();
      let result;
      try {
        result = await getPrf(model, [factor.credential_id]);
        const recipient = await primitives.deriveEncapsulationKeypair(
          result.prf, policy.FACTOR_RECIPIENT_PURPOSE,
        );
        const enrolled = result.credentialId === factor.credential_id
          && factor.recipients.some(
            (slot) => slot.recipient_public_key === recipient.publicKeyHex,
          );
        if (!enrolled) {
          result.prf.fill(0);
          throw new Error(NO_ROOT_AUTHORITY);
        }
        seeds[factor.factor_id] = result.prf;
        noteCollected();
      } catch (error) {
        result?.prf?.fill?.(0); S.busy = null;
        S.errors = {
          [factor.factor_id]: error?.message || 'That passkey did not open a policy factor.',
        };
        render();
      }
    }

    function render() {
      card.innerHTML = '';
      card.appendChild(el('div', 'or-ttl', title));
      if (detail) card.appendChild(el('div', 'or-sub', detail));
      card.appendChild(el('div', 'or-req', requirementLine()));

      const satisfied = policy.policySatisfied(
        model.envelope.policy, Object.keys(seeds),
      );
      let focusTarget = null;
      factors.forEach((factor) => {
        const slot = el('div', 'or-factor-slot');
        if (seeds[factor.factor_id]) {
          const done = el('div', 'or-factor done');
          const row = el('div', 'or-factor-row');
          row.appendChild(factorIcon(ICONS.check, factor));
          row.appendChild(el('span', 'or-factor-name',
            factor.type === 'password' ? 'Password validated' : 'Passkey verified'));
          done.appendChild(row);
          slot.appendChild(done);
        } else if (satisfied) {
          // Required set already green; a moot factor disappears while the
          // dialog closes itself.
          return;
        } else if (factor.type === 'password') {
          const grouping = el('div', 'or-factor');
          const row = el('div', 'or-factor-row');
          row.appendChild(factorIcon(ICONS.password, factor));
          const input = el('input', 'or-in-bare');
          input.type = 'password'; input.autocomplete = 'current-password';
          input.placeholder = passwordCount > 1 ? label(factor) : 'Enter your password';
          input.value = S.passwords[factor.factor_id] || '';
          input.oninput = () => { S.passwords[factor.factor_id] = input.value; };
          input.onkeydown = (event) => { if (event.key === 'Enter') addPassword(factor); };
          row.appendChild(input);
          const ok = el('div', 'or-ok', S.busy === factor.factor_id ? '…' : 'OK');
          if (S.busy) ok.setAttribute('aria-disabled', 'true');
          else ok.onclick = () => addPassword(factor);
          row.appendChild(ok);
          grouping.appendChild(row);
          slot.appendChild(grouping);
          if (!focusTarget) focusTarget = input;
        } else {
          const button = el('button', 'or-factor or-factor-btn');
          button.type = 'button';
          button.appendChild(factorIcon(ICONS.passkey, factor));
          button.appendChild(el('span', 'or-factor-name',
            S.busy === factor.factor_id ? 'Waiting for your passkey…'
              : passkeyCount > 1 ? label(factor) : 'Select your passkey'));
          if (S.busy) button.setAttribute('aria-disabled', 'true');
          else button.onclick = () => addPasskey(factor);
          slot.appendChild(button);
        }
        if (S.errors[factor.factor_id]) {
          slot.appendChild(el('div', 'or-factor-err', S.errors[factor.factor_id]));
        }
        card.appendChild(slot);
      });

      if (S.warn) card.appendChild(el('div', 'or-warn', S.warn));
      const cancel = el('div', 'or-cancel', 'Cancel');
      cancel.onclick = () => close(null);
      card.appendChild(cancel);
      if (focusTarget) setTimeout(() => focusTarget.focus(), 0);
    }
    render();
  });
}

/**
 * Show the single factor-aware unlock dialog and resolve with a usable root
 * signing key once the operator proves the armor's required factor(s).
 *
 * @returns {Promise<{seed:Uint8Array, signingKey:CryptoKey, rootPub:string}|null>}
 *   null when the operator cancels. The caller uses `signingKey` to sign (or
 *   `seed` for a ceremony that needs it) and MUST zero `seed` when done.
 */
export async function openRoot({ title = 'Approve', detail = '' } = {}) {
  injectStyles();
  const model = await loadModel();
  if (model.v3) return openRootPolicy(model, { title, detail });
  // loadModel refuses every non-v3 armor, so this line is unreachable; it
  // exists so a future model shape fails loudly instead of silently.
  throw new Error('unsupported root model');
}

export default openRoot;
