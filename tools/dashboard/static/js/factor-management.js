/* Factor management — the "Manage my factors" surface (signed-in).
 *
 * Renders the design's Factors screen (unlock-screen-root-required-flags.html,
 * the plan of record) and wires every control to the MERGED factor backend.
 * All authority decisions come from factor-policy.js (the tested model /
 * FactorAuth.tla): a control is offered only when it lands somewhere legal, so
 * an operator can never tap their way to a locked-out identity.
 *
 * Each mutation is a real re-arm: open the current armor with the right
 * factor(s), re-factor via signArmorUpdate, and POST the signed armor to
 * /api/identity/personal/armor. Adding a device is the full enrollPasskey
 * ceremony. Removing one is DELETE /api/identity/passkey/{id}. After any
 * change the model is reloaded from the server, so the screen always reflects
 * the stored armor, never an optimistic guess.
 */
import * as armor from './ceremony/primitives.js';
import * as P from './ceremony/factor-policy.js';
import {
  prfEvalExtension, prfOutputFromResults, deriveProvisioningKey, enrollPasskey,
} from './ceremony/enrollment.js';

const { NAME, LV } = P;

let host = null;      // the overlay root element
let onClosed = null;
let M = null;         // the authority model (factor-policy shape)
let raw = null;       // { status, armor } as fetched
let screen = 'factors';
let warn = null;
let busy = false;

// ── data ───────────────────────────────────────────────────────────────
async function fetchJson(url, opts) {
  const r = await fetch(url, Object.assign({ credentials: 'same-origin' }, opts || {}));
  const body = await r.json().catch(() => ({}));
  if (!r.ok || body.ok === false) {
    throw new Error(body.error || `request failed: ${url} (${r.status})`);
  }
  return body;
}

async function loadModel() {
  const status = await fetchJson('/api/identity/status', {
    cache: 'no-store', headers: { Accept: 'application/json' },
  });
  const personal = await fetchJson('/api/identity/personal', {
    headers: { Accept: 'application/json' },
  });
  const armorData = armor.parseArmor(personal.armored_private_key);
  raw = { status, armor: personal.armored_private_key };
  M = P.buildModel(status, armorData);
  return M;
}

// ── WebAuthn PRF assertion for a specific credential ─────────────────────
function b64uToBytes(s) {
  let b = s.replace(/-/g, '+').replace(/_/g, '/');
  while (b.length % 4) b += '=';
  const bin = atob(b);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
  return out;
}

// Present a passkey and return its 32-byte PRF output. `credentialId` (base64url)
// scopes the assertion to one device; omit it to let the platform choose.
async function prfPresent(credentialId) {
  if (!window.PublicKeyCredential || !navigator.credentials) {
    throw new Error('this browser cannot use Face ID — use a device that can');
  }
  const rpId = raw.status.rp_id || undefined;
  const allow = credentialId
    ? [{ type: 'public-key', id: b64uToBytes(credentialId) }]
    : (raw.status.passkeys || [])
      .filter((p) => p.credential_id && (!rpId || p.rp_id === rpId))
      .map((p) => ({ type: 'public-key', id: b64uToBytes(p.credential_id) }));
  let asrt;
  try {
    asrt = await navigator.credentials.get({ publicKey: {
      challenge: crypto.getRandomValues(new Uint8Array(32)),
      rpId, allowCredentials: allow, userVerification: 'required',
      extensions: prfEvalExtension(),
    } });
  } catch (e) {
    if (e && e.name === 'NotAllowedError') throw new Error('Face ID was cancelled — try again');
    throw e;
  }
  const prf = prfOutputFromResults(asrt.getClientExtensionResults());
  if (!prf) throw new Error('this passkey has no PRF and cannot hold or pair a key');
  return prf;
}

// ── re-arm ceremonies ────────────────────────────────────────────────────
// Each returns after the server has stored the new armor. They take the
// gathered inputs and translate to exactly one signArmorUpdate + POST.
async function postRearm(body) {
  await fetchJson('/api/identity/personal/armor', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

async function ceremonyPromote(key, password) {
  const prf = await prfPresent(key.credentialId);          // present the key being raised
  const provisioningPub = (await deriveProvisioningKey(prf)).publicKeyHex;
  const body = await armor.signArmorUpdate(raw.armor, password,
    { kind: 'promote', credentialId: key.credentialId, provisioningPub }, false);
  await postRearm(body);
}

async function ceremonyDemote(key, password) {
  const body = await armor.signArmorUpdate(raw.armor, password,
    { kind: 'demote', credentialId: key.credentialId }, false);
  await postRearm(body);
}

async function ceremonyChangePassword(current, next) {
  const body = await armor.signArmorUpdate(raw.armor, current,
    { kind: 'setPassword', newPassword: next }, false);
  await postRearm(body);
}

async function ceremonyAddPassword(next) {                 // passkey-only identity
  const prf = await prfPresent();
  const body = await armor.signArmorUpdate(raw.armor, { prf },
    { kind: 'addPassword', newPassword: next }, false);
  await postRearm(body);
}

async function ceremonyRemovePassword(password) {
  const body = await armor.signArmorUpdate(raw.armor, password,
    { kind: 'removePassword' }, false);
  await postRearm(body);
}

async function ceremonyEnableMfa(key, password) {
  const prf = await prfPresent(key.credentialId);          // validate the passkey
  const provisioningPub = (await deriveProvisioningKey(prf)).publicKeyHex;
  const body = await armor.signArmorUpdate(raw.armor, password,
    { kind: 'enableMfa', credentialId: key.credentialId, provisioningPub }, true);
  await postRearm(body);
}

async function ceremonyDisableMfa(password) {
  const key = (M.keys.find((k) => k.credentialId === M.combinedCredentialId)
    || M.keys.find((k) => k.prf));
  const prf = await prfPresent(key && key.credentialId);
  const body = await armor.signArmorUpdate(raw.armor, { password, prf },
    { kind: 'disableMfa', credentialId: key && key.credentialId }, false);
  await postRearm(body);
}

async function ceremonyRemoveDevice(key) {
  await fetchJson(`/api/identity/passkey/${encodeURIComponent(key.credentialId)}`,
    { method: 'DELETE' });
}

async function ceremonyAddDevice(password) {
  // Adding a device needs the root to sign the enrollment statement: open the
  // armor here (the only place the seed lives), enroll, then zero it.
  const opened = await armor.decryptArmor(raw.armor, password);
  const seed = opened.seed;
  try {
    const signingKey = await armor.importEd25519RootSigningKey(seed);
    await enrollPasskey({ root: { signingKey, publicHex: opened.rootPub } });
  } finally {
    seed.fill(0);
  }
}

// ── rendering ────────────────────────────────────────────────────────────
function el(tag, cls, html) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
}

function close() {
  if (host && host.parentNode) host.parentNode.removeChild(host);
  host = null;
  if (typeof onClosed === 'function') onClosed();
}

// A minimal password (+ optional Face ID) ceremony overlay. Resolves with the
// gathered inputs, or rejects/cancels. `opts.newPassword` adds a new-password
// field (change/add/set); `opts.confirm` labels the confirm button.
function ceremonyPrompt(opts) {
  return new Promise((resolve, reject) => {
    const wrap = el('div', 'fm-ceremony');
    const needsCurrent = opts.currentPassword !== false;
    wrap.innerHTML =
      `<div class="fm-ttl">${opts.title}</div>`
      + (opts.body ? `<div class="fm-body">${opts.body}</div>` : '')
      + (needsCurrent
        ? `<label class="fm-lab">${opts.currentLabel || 'Password'}</label>`
          + '<input type="password" class="fm-in" data-f="cur" autocomplete="current-password">'
        : '')
      + (opts.newPassword
        ? '<label class="fm-lab">Choose a password</label>'
          + '<input type="password" class="fm-in" data-f="new" autocomplete="new-password">'
          + '<label class="fm-lab">Confirm password</label>'
          + '<input type="password" class="fm-in" data-f="cfm" autocomplete="new-password">'
        : '')
      + '<div class="fm-err" hidden></div>'
      + `<div class="fm-actions"><button class="fm-btn ghost" data-a="cancel">Cancel</button>`
      + `<button class="fm-btn" data-a="ok">${opts.confirm || 'Continue'}</button></div>`;
    const errBox = wrap.querySelector('.fm-err');
    const get = (f) => { const i = wrap.querySelector(`[data-f="${f}"]`); return i ? i.value : ''; };
    wrap.querySelector('[data-a="cancel"]').onclick = () => { overlay.remove(); reject(null); };
    wrap.querySelector('[data-a="ok"]').onclick = () => {
      const inp = {};
      if (needsCurrent) {
        inp.password = get('cur');
        if (!inp.password) { errBox.hidden = false; errBox.textContent = 'Enter your password.'; return; }
      }
      if (opts.newPassword) {
        inp.newPassword = get('new');
        if (!inp.newPassword) { errBox.hidden = false; errBox.textContent = 'Choose a password.'; return; }
        if (inp.newPassword !== get('cfm')) {
          errBox.hidden = false; errBox.textContent = 'Passwords do not match.'; return;
        }
      }
      overlay.remove();
      resolve(inp);
    };
    const overlay = el('div', 'fm-overlay');
    overlay.appendChild(wrap);
    host.appendChild(overlay);
    const first = wrap.querySelector('.fm-in');
    if (first) first.focus();
  });
}

// Run a mutating action end to end: gather inputs, execute, reload, re-render.
// A refusal from the policy or backend is shown as the warn line, never a
// half-applied change (the server is the source of truth we reload from).
async function run(label, worker) {
  busy = true; warn = null; render();
  try {
    await worker();
    await loadModel();
    screen = 'factors';
  } catch (e) {
    if (e === null) { busy = false; render(); return; }   // user cancelled
    warn = (e && e.message) || String(e);
  }
  busy = false;
  render();
}

function badge(k) {
  const lv = P.level(M, k);
  return `<span class="fm-tag ${lv}">${LV[lv]}</span>`;
}

function factorsScreen() {
  const n = M.keys.length + (M.pass.on ? 1 : 0);
  const p = el('div', 'fm-panel');
  p.appendChild(el('div', 'fm-head',
    `<div class="fm-h1">Factors</div><div class="fm-h2">${n} on this identity</div>`));

  // ── Password ──
  p.appendChild(el('div', 'fm-sec', 'Password'));
  if (!M.pass.on) {
    const add = el('button', 'fm-row act',
      '<div class="fm-rl">Set a password</div>'
      + '<div class="fm-rd">Reach your identity where there is no passkey</div>');
    add.onclick = () => run('set-password', async () => {
      const inp = await ceremonyPrompt({
        title: 'Set a password', currentPassword: false, newPassword: true,
        body: 'Confirm with Face ID, then choose a password.', confirm: 'Set password',
      });
      await ceremonyAddPassword(inp.newPassword);
    });
    p.appendChild(add);
  } else {
    const canChange = !M.mfa && P.actionOn(M, 'pass') !== null;
    const row = el('div', 'fm-row',
      `<div class="fm-rmid"><div class="fm-rl">Password</div></div>${badge('pass')}`
      + (canChange ? '<button class="fm-mini" data-a="chg">Change</button>' : '')
      + (canChange ? '<button class="fm-x" data-a="rm">&times;</button>' : ''));
    const chg = row.querySelector('[data-a="chg"]');
    if (chg) chg.onclick = () => run('change-password', async () => {
      const inp = await ceremonyPrompt({
        title: 'Change your password', currentLabel: 'Current password', newPassword: true,
        confirm: 'Change password',
      });
      await ceremonyChangePassword(inp.password, inp.newPassword);
    });
    const rm = row.querySelector('[data-a="rm"]');
    if (rm) rm.onclick = () => run('remove-password', async () => {
      const inp = await ceremonyPrompt({
        title: 'Remove your password',
        body: 'Your passkey will remain the way in. Enter your password to confirm.',
        confirm: 'Remove password',
      });
      await ceremonyRemovePassword(inp.password);
    });
    p.appendChild(row);
  }

  // ── Passkeys ──
  p.appendChild(el('div', 'fm-sec', 'Passkeys'));
  M.keys.forEach((k) => {
    let tag;
    if (!k.prf) {
      tag = `<span class="fm-tag nop">${M.mfa ? 'cannot pair' : 'no prf'}</span>`;
    } else {
      const lv = k.auth === 'full' ? 'b' : 'a';
      const tappable = !M.mfa && P.actionOn(M, 'face') !== null;
      tag = `<span class="fm-tag ${lv}"${tappable ? ' data-a="auth" style="cursor:pointer"' : ''}>`
        + `${k.auth === 'full' ? 'full authority' : 'unlock only'}</span>`;
    }
    const row = el('div', 'fm-row',
      `<div class="fm-rmid"><div class="fm-rl">${k.label}`
      + `${k.here ? '<span class="fm-here">this device</span>' : ''}</div></div>`
      + `${tag}<button class="fm-x" data-a="rm">&times;</button>`);
    const authBtn = row.querySelector('[data-a="auth"]');
    if (authBtn) authBtn.onclick = () => changeKeyAuthority(k);
    row.querySelector('[data-a="rm"]').onclick = () => run('remove-device', async () => {
      await ceremonyRemoveDevice(k);
    });
    p.appendChild(row);
  });
  const add = el('button', 'fm-row act',
    '<div class="fm-rl">Add a passkey</div>'
    + '<div class="fm-rd">This device, or scan from a phone</div>');
  add.onclick = () => run('add-device', async () => {
    const inp = await ceremonyPrompt({
      title: 'Add a passkey', body: 'Enter your password, then approve the new passkey.',
      confirm: 'Add passkey',
    });
    await ceremonyAddDevice(inp.password);
  });
  p.appendChild(add);

  // ── Multi-Factor ──
  p.appendChild(el('div', 'fm-sec', 'Multi-Factor'));
  const mfaAction = P.actionOn(M, 'both');
  const mfaRow = el('div', 'fm-row',
    `<div class="fm-rmid"><div class="fm-rl">${NAME.both}</div>`
    + '<div class="fm-rd">Require your password and your passkey together</div></div>'
    + `${badge('both')}`);
  if (mfaAction === 'enable') {
    const b = el('button', 'fm-mini', 'Enable');
    b.onclick = () => enableMfaFlow();
    mfaRow.appendChild(b);
  } else if (mfaAction === 'change') {   // MFA on and unwinding it stays legal
    const b = el('button', 'fm-mini', 'Turn off');
    b.onclick = () => run('disable-mfa', async () => {
      const inp = await ceremonyPrompt({
        title: 'Turn off Multi-Factor',
        body: 'Enter your password and confirm with Face ID. Afterwards either one '
          + 'will open your identity on its own again.',
        confirm: 'Turn off',
      });
      await ceremonyDisableMfa(inp.password);
    });
    mfaRow.appendChild(b);
  }
  p.appendChild(mfaRow);

  if (warn) p.appendChild(el('div', 'fm-warn', warn));
  const back = el('button', 'fm-back', '&lsaquo; Close');
  back.onclick = close;
  p.appendChild(back);
  return p;
}

// Tapping a passkey's authority badge: promote (unlock only → full) or demote.
// Probe the target state through the policy first — a refusal is shown, never
// attempted.
function changeKeyAuthority(key) {
  const want = key.auth === 'full' ? 'unlock' : 'full';
  const probe = JSON.parse(JSON.stringify(M));
  const pk = probe.keys.find((x) => x.credentialId === key.credentialId);
  pk.auth = want;
  probe.face.full = probe.keys.some((x) => x.prf && x.auth === 'full');
  const why = P.invalid(probe);
  if (why) { warn = why; render(); return; }
  const promote = want === 'full';
  run(promote ? 'promote' : 'demote', async () => {
    const inp = await ceremonyPrompt({
      title: promote ? `Make ${key.label} unlock your key` : `Make ${key.label} unlock only`,
      body: promote
        ? 'Enter your password, then present this passkey to raise it to full authority.'
        : 'Enter your password to lower this passkey to unlock-only.',
      confirm: promote ? 'Upgrade' : 'Change',
    });
    if (promote) await ceremonyPromote(key, inp.password);
    else await ceremonyDemote(key, inp.password);
  });
}

// Enable MFA: the two-green-checkmark ceremony (password ✓, passkey ✓), then
// the combined factor replaces the individual openers.
function enableMfaFlow() {
  const key = M.keys.find((k) => k.prf);
  if (!key) { warn = 'A passkey that can derive a key is required to pair.'; render(); return; }
  run('enable-mfa', async () => {
    const inp = await ceremonyPrompt({
      title: 'Enable Multi-Factor',
      body: 'Enabling Multi-Factor will require both your password and your passkey in '
        + 'order to grant full authority. Enter your password, then confirm with Face ID.',
      confirm: 'Enable Multi-Factor',
    });
    await ceremonyEnableMfa(key, inp.password);
  });
}

function render() {
  if (!host) return;
  host.textContent = '';
  const backdrop = el('div', 'fm-backdrop');
  backdrop.onclick = close;
  host.appendChild(backdrop);
  if (!M) {
    // No model yet: either still loading, or the load failed (warn set). A
    // failure must surface as an error with a way out, never a stuck "Loading…".
    const p = el('div', 'fm-panel', '<div class="fm-head"><div class="fm-h1">Factors</div>'
      + `<div class="fm-h2">${warn ? 'Could not load your factors' : 'Loading…'}</div></div>`);
    if (warn) {
      p.appendChild(el('div', 'fm-warn', warn));
      const back = el('button', 'fm-back', '&lsaquo; Close');
      back.onclick = close;
      p.appendChild(back);
    }
    host.appendChild(p);
    return;
  }
  host.appendChild(factorsScreen());
  if (busy) {
    const b = el('div', 'fm-busy', 'Working…');
    host.appendChild(b);
  }
}

// ── styles (self-contained; injected once) ───────────────────────────────
const STYLE = `
.fm-host{position:fixed;inset:0;z-index:1000;display:flex;align-items:center;
 justify-content:center;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;color:#e6edf3}
.fm-backdrop{position:absolute;inset:0;background:rgba(2,6,12,.72)}
.fm-panel{position:relative;width:min(480px,94vw);max-height:90vh;overflow:auto;
 background:#0e141b;border:1px solid #232b36;border-radius:14px;padding:1.1rem 1.15rem 1.4rem}
.fm-head{display:flex;flex-direction:column;gap:.15rem;margin-bottom:.4rem}
.fm-h1{font-size:1.15rem;font-weight:600}
.fm-h2{color:#93a1b0;font-size:.82rem}
.fm-sec{margin:1rem 0 .35rem;font:600 .64rem/1 ui-monospace,Menlo,monospace;
 letter-spacing:.14em;text-transform:uppercase;color:#5b6673}
.fm-row{display:flex;align-items:center;gap:.6rem;width:100%;text-align:left;
 background:#121820;border:1px solid #232b36;border-radius:10px;padding:.6rem .7rem;margin:.35rem 0;color:#e6edf3}
button.fm-row{cursor:pointer}
button.fm-row:hover{background:#18202b}
.fm-row.act{flex-direction:column;align-items:flex-start;gap:.1rem}
.fm-rmid{flex:1;min-width:0}
.fm-rl{font-weight:600;font-size:.9rem}
.fm-rd{color:#93a1b0;font-size:.76rem}
.fm-here{margin-left:.4rem;font-size:.6rem;color:#34d399;background:rgba(52,211,153,.12);
 border:1px solid rgba(52,211,153,.3);border-radius:5px;padding:.05rem .3rem;vertical-align:1px}
.fm-tag{font-size:.64rem;font-weight:700;letter-spacing:.03em;padding:.1rem .42rem;border-radius:5px;white-space:nowrap}
.fm-tag.b{color:#d29922;background:rgba(210,153,34,.14)}
.fm-tag.a{color:#58a6ff;background:rgba(88,166,255,.14)}
.fm-tag.off,.fm-tag.en{color:#8b949e;background:rgba(139,148,158,.12)}
.fm-tag.nop{color:#f0883e;background:rgba(240,136,62,.12)}
.fm-mini{font-size:.78rem;color:#c9d1d9;background:#1c2530;border:1px solid #2d3745;
 border-radius:7px;padding:.28rem .6rem;cursor:pointer}
.fm-mini:hover{background:#232d3a}
.fm-x{font-size:1.1rem;line-height:1;color:#8b949e;background:none;border:none;cursor:pointer;padding:.1rem .3rem}
.fm-x:hover{color:#f85149}
.fm-warn{margin:.6rem 0;color:#f85149;font-size:.82rem}
.fm-back{margin-top:1rem;background:none;border:none;color:#58a6ff;cursor:pointer;font-size:.85rem;padding:.3rem 0}
.fm-busy{position:absolute;bottom:1rem;left:1.15rem;color:#5b6673;font-size:.72rem}
.fm-overlay{position:absolute;inset:0;z-index:10;display:flex;align-items:center;
 justify-content:center;background:rgba(2,6,12,.55)}
.fm-ceremony{width:min(400px,90vw);background:#0e141b;border:1px solid #2d3745;
 border-radius:12px;padding:1.1rem}
.fm-ttl{font-size:1rem;font-weight:600;margin-bottom:.3rem}
.fm-body{color:#93a1b0;font-size:.82rem;margin-bottom:.7rem}
.fm-lab{display:block;color:#93a1b0;font-size:.78rem;margin:.5rem 0 .25rem}
.fm-in{width:100%;background:#0b0f14;border:1px solid #232b36;border-radius:8px;
 padding:.55rem .6rem;color:#e6edf3;font-size:.95rem}
.fm-in:focus{outline:none;border-color:#6366f1;box-shadow:0 0 0 2px rgba(99,102,241,.35)}
.fm-err{color:#f85149;font-size:.78rem;margin-top:.5rem}
.fm-actions{display:flex;justify-content:flex-end;gap:.6rem;margin-top:1rem}
.fm-btn{background:#4f46e5;color:#fff;border:none;border-radius:9px;padding:.5rem .9rem;font-weight:600;cursor:pointer}
.fm-btn:hover{background:#4338ca}
.fm-btn.ghost{background:transparent;color:#93a1b0;border:1px solid #2d3745}
`;

function injectStyles() {
  if (document.getElementById('fm-styles')) return;
  const s = document.createElement('style');
  s.id = 'fm-styles';
  s.textContent = STYLE;
  document.head.appendChild(s);
}

// ── entry point ──────────────────────────────────────────────────────────
async function open(opts) {
  onClosed = (opts && opts.onClose) || null;
  injectStyles();
  host = el('div', 'fm-host');
  host.setAttribute('data-testid', 'factor-management');
  document.body.appendChild(host);
  screen = 'factors'; warn = null; busy = false; M = null;
  render();
  try {
    await loadModel();
  } catch (e) {
    warn = (e && e.message) || String(e);
  }
  render();
}

export { open };
export const _internals = {
  buildCeremony: { ceremonyPromote, prfPresent },
  loadModel, factorsScreen, render,
};
