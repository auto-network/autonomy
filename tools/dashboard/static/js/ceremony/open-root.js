/* The single, factor-aware "unlock your root and approve this" dialog.
 *
 * ONE common code path for every approval that needs the personal root. It reads
 * the armor's own factor set and presents exactly the openers that armor accepts
 * — a password field when a password opens it, a passkey button when a passkey
 * opens it, BOTH (and requires both) under Multi-Factor, and a chooser when
 * either one works so the operator picks. Callers never touch a password field
 * or decryptArmor again: they call `openRoot({title, detail})`, get back a
 * ready-to-use root signing key (and the raw seed for ceremonies that need it),
 * sign their specific request, and zero the seed. No per-dialog factor logic.
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
.or-card{width:100%;max-width:360px;background:#11141c;border:1px solid #1e2432;border-radius:14px;
  padding:22px 20px 18px;box-shadow:0 20px 50px -12px #000}
.or-ttl{font-size:16px;font-weight:600;text-align:center;margin:0 0 4px}
.or-sub{font-size:12.5px;color:#9aa3b2;text-align:center;margin:0 0 16px;line-height:1.45}
.or-lab{display:block;font-size:13px;color:#9ca3af;margin:6px 0 6px}
.or-in{width:100%;background:#1f2937;border:1px solid #374151;border-radius:8px;padding:12px;
  font-size:16px;color:#e5e7eb;outline:none;font-family:inherit;margin-bottom:10px}
.or-btn{background:#5b57e8;color:#fff;text-align:center;padding:12px;border-radius:9px;
  font-size:14px;font-weight:600;cursor:pointer;margin-top:6px}
.or-btn.alt{background:#161b26;border:1px solid #232a39;color:#e5e7eb}
.or-btn[aria-disabled=true]{opacity:.5;cursor:default}
.or-row{display:flex;align-items:center;gap:10px;padding:12px;margin-bottom:8px;background:#161b26;
  border:1px solid #232a39;border-radius:10px;cursor:pointer}
.or-row.on{border-color:#5b57e8;background:#191a2e}
.or-ic{width:26px;height:26px;flex:0 0 26px;color:#38bdf8}
.or-ic svg{width:100%;height:100%;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linejoin:round}
.or-done{color:#34d399}
.or-warn{font-size:11.5px;color:#fca5a5;background:#2b1616;border:1px solid #4b2222;border-radius:9px;
  padding:10px 12px;line-height:1.5;margin-top:10px}
.or-cancel{font-size:13px;color:#8b93a3;text-align:center;padding:10px;cursor:pointer;margin-top:4px}
.or-cancel:hover{color:#e5e7eb}
`;
const FACE = '<svg viewBox="0 0 24 24"><path d="M4 8.6V6.2A2.2 2.2 0 0 1 6.2 4h2.4M15.4 4h2.4A2.2 2.2 0 0 1 20 6.2v2.4M20 15.4v2.4a2.2 2.2 0 0 1-2.2 2.2h-2.4M8.6 20H6.2A2.2 2.2 0 0 1 4 17.8v-2.4" stroke-linecap="round"/><path d="M9.2 10.2v1.6M14.8 10.2v1.6M12 10.2v3.2M10 15.4a3.6 3.6 0 0 0 4 0" stroke-linecap="round"/></svg>';
const NAME = { password: 'Password', passkey: 'Passkey' };

function injectStyles() {
  if (document.getElementById('open-root-styles')) return;
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
      v3: true,
      policyModule,
      envelope,
      factorViews: fp.factors || [],
    };
  }
  const data = primitives.parseArmor(pj.armored_private_key);
  const factors = data.factors || [];
  const mfa = factors.some((f) => f.type === 'combined');
  const hasPassword = factors.some((f) => f.type === 'password');
  const hasPasskey = factors.some((f) => f.type === 'passkey');
  const openers = mfa
    ? ['both']
    : [...(hasPassword ? ['password'] : []), ...(hasPasskey ? ['passkey'] : [])];
  return {
    armor: pj.armored_private_key,
    rootPub: pj.root_pub || data.root_pub,
    rpId: st.rp_id || undefined,
    passkeys: st.passkeys || [],
    mfa,
    openers,
  };
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
    asrt = await navigator.credentials.get({
      publicKey: {
        challenge: crypto.getRandomValues(new Uint8Array(32)),
        rpId, allowCredentials: allow, userVerification: 'required',
        extensions: prfEvalExtension(),
      },
    });
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

// Open the armor with the gathered factor(s) and return the seed + signing key.
async function openWith(model, { password, prf }) {
  let opened;
  if (model.mfa) opened = await primitives.decryptArmorWithCombined(model.armor, password, prf);
  else if (prf != null) opened = await primitives.decryptArmorWithPasskey(model.armor, prf);
  else opened = await primitives.decryptArmor(model.armor, password);
  const signingKey = await primitives.importEd25519RootSigningKey(opened.seed);
  return { seed: opened.seed, signingKey, rootPub: opened.rootPub };
}

async function openRootPolicy(model, { title, detail }) {
  const policy = model.policyModule;
  const memberIds = new Set(policy.policyFactorIds(model.envelope.policy));
  const factors = model.envelope.factors.filter((factor) => memberIds.has(factor.factor_id));
  const views = Object.fromEntries((model.factorViews || []).map((row) => [row.factor_id, row]));
  const seeds = {};
  const S = { selectedPassword: null, password: '', warn: null, busy: false };

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
    function factorRow(cls, icon, text) {
      const row = el('div', cls);
      const iconNode = el('div'); iconNode.textContent = icon;
      const textNode = el('div'); textNode.textContent = text;
      row.append(iconNode, textNode);
      return row;
    }
    function label(factor) {
      return views[factor.factor_id]?.label
        || (factor.type === 'password' ? 'Password' : 'Passkey');
    }
    async function finishIfSatisfied() {
      if (!policy.policySatisfied(model.envelope.policy, Object.keys(seeds))) {
        S.busy = false; render(); return;
      }
      try {
        const opened = await policy.openFactorPolicyArmor(model.armor, seeds);
        close(opened);
      } catch (error) {
        cleanFactorSeeds(); S.busy = false;
        S.warn = error?.message || 'Those factors did not open your root.';
        render();
      }
    }
    async function addPassword() {
      if (S.busy || !S.selectedPassword || !S.password) return;
      S.busy = true; S.warn = null; render();
      try {
        const factor = factors.find((row) => row.factor_id === S.selectedPassword);
        seeds[factor.factor_id] = await policy.openPasswordFactor(
          model.rootPub, factor, S.password,
        );
        S.password = ''; S.selectedPassword = null;
        await finishIfSatisfied();
      } catch (error) {
        S.busy = false; S.password = '';
        S.warn = error?.message || 'That password did not open the selected factor.';
        render();
      }
    }
    async function addPasskey() {
      if (S.busy) return;
      const remaining = factors.filter(
        (factor) => factor.type === 'passkey' && !seeds[factor.factor_id],
      );
      S.busy = true; S.warn = null; render();
      let result;
      try {
        result = await getPrf(model, remaining.map((factor) => factor.credential_id));
        const recipient = await primitives.deriveEncapsulationKeypair(
          result.prf, policy.FACTOR_RECIPIENT_PURPOSE,
        );
        const match = remaining.find(
          (factor) => factor.credential_id === result.credentialId
            && factor.recipients.some(
              (slot) => slot.recipient_public_key === recipient.publicKeyHex,
            ),
        );
        if (!match) {
          result.prf.fill(0);
          throw new Error(
            'This passkey works for dashboard access, but this device is not enrolled to authorize your root.',
          );
        }
        seeds[match.factor_id] = result.prf;
        await finishIfSatisfied();
      } catch (error) {
        result?.prf?.fill?.(0); S.busy = false;
        S.warn = error?.message || 'That passkey did not open a policy factor.';
        render();
      }
    }

    function render() {
      card.innerHTML = '';
      card.appendChild(el('div', 'or-ttl', title));
      if (detail) card.appendChild(el('div', 'or-sub', detail));
      card.appendChild(el('div', 'or-sub',
        'Choose the factor or factor combination required by your current root policy.'));

      const collected = factors.filter((factor) => seeds[factor.factor_id]);
      collected.forEach((factor) => card.appendChild(
        factorRow('or-row on', '✓', label(factor)),
      ));

      const passwords = factors.filter(
        (factor) => factor.type === 'password' && !seeds[factor.factor_id],
      );
      if (passwords.length) {
        if (passwords.length === 1 && !S.selectedPassword) {
          S.selectedPassword = passwords[0].factor_id;
        }
        if (!S.selectedPassword && passwords.length > 1) {
          card.appendChild(el('div', 'or-lab', 'Password factor'));
          passwords.forEach((factor) => {
            const row = factorRow('or-row', '', label(factor));
            row.onclick = () => { S.selectedPassword = factor.factor_id; render(); };
            card.appendChild(row);
          });
        } else if (S.selectedPassword) {
          const selected = passwords.find((factor) => factor.factor_id === S.selectedPassword);
          if (selected) {
            card.appendChild(el('label', 'or-lab', label(selected)));
            const input = el('input', 'or-in'); input.type = 'password';
            input.autocomplete = 'current-password'; input.value = S.password;
            input.oninput = () => { S.password = input.value; };
            input.onkeydown = (event) => { if (event.key === 'Enter') addPassword(); };
            card.appendChild(input);
            const add = el('div', 'or-btn', S.busy ? 'Checking…' : 'Use this password');
            if (!S.busy) add.onclick = addPassword; card.appendChild(add);
            setTimeout(() => input.focus(), 0);
          }
        }
      }

      const passkeys = factors.filter(
        (factor) => factor.type === 'passkey' && !seeds[factor.factor_id],
      );
      if (passkeys.length) {
        const button = el('div', 'or-btn alt', S.busy ? 'Waiting…' : 'Use a passkey');
        if (!S.busy) button.onclick = addPasskey; card.appendChild(button);
      }
      if (S.warn) card.appendChild(el('div', 'or-warn', S.warn));
      const cancel = el('div', 'or-cancel', 'Cancel'); cancel.onclick = () => close(null);
      card.appendChild(cancel);
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
  if (!model.openers.length) throw new Error('your identity has no factor that can open the root');

  return new Promise((resolve) => {
    const host = document.createElement('div');
    host.className = 'or-overlay';
    host.setAttribute('data-testid', 'open-root');
    const card = document.createElement('div'); card.className = 'or-card';
    host.appendChild(card);
    document.body.appendChild(host);

    // S.method: which single opener is chosen (password|passkey) when either
    // works; null until chosen. Under MFA both are always required.
    const S = { method: model.mfa ? null : (model.openers.length === 1 ? model.openers[0] : null),
      password: null, prf: null, warn: null, busy: false };

    function close(result) {
      if (host.parentNode) host.parentNode.removeChild(host);
      resolve(result);
    }

    async function finish() {
      if (S.busy) return;
      S.busy = true; S.warn = null; render();
      try {
        const out = await openWith(model, { password: S.password, prf: S.prf });
        close(out);
      } catch (e) {
        S.busy = false;
        S.warn = (e && e.message) || 'that proof did not open your identity — try again';
        S.prf = null; render();
      }
    }

    function el(tag, cls, html) {
      const n = document.createElement(tag);
      if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n;
    }

    function passwordField(labelText) {
      const lab = el('label', 'or-lab', labelText); card.appendChild(lab);
      const i = el('input', 'or-in'); i.type = 'password'; i.autocomplete = 'current-password';
      if (S.password) i.value = S.password;
      i.oninput = () => { S.password = i.value; };
      card.appendChild(i);
      setTimeout(() => i.focus(), 0);
      return i;
    }
    function passkeyButton(labelText, cb) {
      const b = el('div', 'or-btn' + (S.prf ? ' alt' : ''),
        S.prf ? '&#10003; Passkey ready' : labelText);
      if (!S.prf) b.onclick = cb; card.appendChild(b);
      return b;
    }
    async function provePasskey() {
      S.warn = null; S.busy = true; render();
      try { S.prf = (await getPrf(model)).prf; S.busy = false; render(); }
      catch (e) { S.busy = false; S.warn = (e && e.message) || String(e); render(); }
    }

    function render() {
      card.innerHTML = '';
      card.appendChild(el('div', 'or-ttl', title));
      if (detail) card.appendChild(el('div', 'or-sub', detail));

      if (model.mfa) {
        card.appendChild(el('div', 'or-sub',
          'Multi-Factor: your password AND your passkey are both required.'));
        passwordField('Your password');
        passkeyButton('Use your passkey', provePasskey);
        const ready = !!S.password && !!S.prf && !S.busy;
        const go = el('div', 'or-btn', S.busy ? 'Working…' : 'Approve');
        go.setAttribute('aria-disabled', String(!ready));
        if (ready) go.onclick = finish;
        card.appendChild(go);
      } else if (!S.method) {
        // Either factor works — let the operator choose which.
        card.appendChild(el('div', 'or-sub', 'Choose a factor to unlock your root.'));
        model.openers.forEach((m) => {
          const row = el('div', 'or-row',
            '<div class="or-ic">' + (m === 'passkey' ? FACE : '') + '</div><div>' + NAME[m] + '</div>');
          row.onclick = () => { S.method = m; S.warn = null; render(); };
          card.appendChild(row);
        });
      } else if (S.method === 'passkey') {
        card.appendChild(el('div', 'or-sub', 'Use your passkey to unlock your root.'));
        if (!S.prf) {
          const b = el('div', 'or-btn', S.busy ? 'Waiting for passkey…' : 'Use your passkey');
          if (!S.busy) b.onclick = () => { provePasskey().then(() => { if (S.prf) finish(); }); };
          card.appendChild(b);
        } else {
          const go = el('div', 'or-btn', S.busy ? 'Working…' : 'Approve');
          go.onclick = finish; card.appendChild(go);
        }
      } else {
        passwordField('Your password');
        const go = el('div', 'or-btn', S.busy ? 'Working…' : 'Approve');
        go.onclick = finish; card.appendChild(go);
      }

      if (model.openers.length > 1 && S.method && !model.mfa) {
        const back = el('div', 'or-cancel', '‹ Use a different factor');
        back.onclick = () => { S.method = null; S.prf = null; S.password = null; S.warn = null; render(); };
        card.appendChild(back);
      }
      if (S.warn) card.appendChild(el('div', 'or-warn', S.warn));
      const cancel = el('div', 'or-cancel', 'Cancel');
      cancel.onclick = () => close(null);
      card.appendChild(cancel);
    }

    render();
  });
}

export default openRoot;
