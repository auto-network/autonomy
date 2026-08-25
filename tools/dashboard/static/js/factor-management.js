/* Factor management — the "Manage my factors" surface.
 *
 * This is the operator's design VERBATIM: revision 32 of the Design Studio
 * design "Unlock screen — root-required flags". The CSS and every screen's
 * markup (keysScreen / authScreen / progScreen / setpwScreen / panelScreen /
 * lockScreen / sheet / tray) are the design's own, byte-for-byte. ONLY the mock
 * hooks are swapped for the real backend: the fake `preset()` model → a real
 * load from /api/identity/status + the armor; the local `commit()` mutations →
 * real re-arm ceremonies (signArmorUpdate → POST /api/identity/personal/armor);
 * the fake "············" password inputs and the WebAuthn sheets → real
 * password capture and real Face ID / PRF. The design's flow already IS the
 * ceremony flow (gather the root in progScreen/the sheet, then apply), so the
 * wiring maps straight onto it. The preview harness (chrome/preset tabs/boot)
 * is removed; the entry screen is the Factors screen and Back closes.
 *
 * Authority policy (rootReachable / level / actionOn / applyAuthority / …) is
 * the design's own, identical to tools/network/idkit/TLA/FactorAuth.tla and the
 * node-tested ceremony/factor-policy.js.
 */
import * as primitives from './ceremony/primitives.js';
import {
  prfEvalExtension, prfOutputFromResults, deriveProvisioningKey, enrollPasskey,
} from './ceremony/enrollment.js';

// ── the design's CSS, verbatim (only the `body{}` rule is rehomed onto the
//    modal overlay so it cannot restyle the host page) ──────────────────────
const STYLE = `
.fui-overlay{position:fixed;inset:0;z-index:1000;overflow:auto;display:flex;
  align-items:center;justify-content:center;background:#080a0f;
  padding:max(32px,calc(env(safe-area-inset-top) + 24px)) max(16px,env(safe-area-inset-right))
    max(32px,calc(env(safe-area-inset-bottom) + 24px)) max(16px,env(safe-area-inset-left));
  color:#e5e7eb;font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
*{box-sizing:border-box}
.card{position:relative;max-width:360px;margin:0 auto;width:100%}
.hide{display:none!important}

.screen{position:relative;background:#11141c;border:1px solid #1e2432;border-radius:14px;
  padding:26px 20px 20px}
.hd{text-align:center;font-size:14px;font-weight:600;color:#8b93a3;margin:0 0 15px}
.av{width:52px;height:52px;border-radius:50%;margin:0 auto 9px;
  background:linear-gradient(150deg,#6366f1,#8b5cf6);display:flex;align-items:center;
  justify-content:center;font:600 21px/1 inherit;color:#fff}
.nm{text-align:center;font-size:15.5px;font-weight:600;margin-bottom:19px}
.ttl{font-size:16px;font-weight:600;text-align:center;margin:2px 0 16px}

.opt{display:flex;align-items:center;gap:11px;padding:11px 12px;margin-bottom:7px;
  background:#161b26;border:1px solid #232a39;border-radius:10px;cursor:pointer}
.opt.sel{border-color:#5b57e8;background:#191a2e}
.opt.dim{opacity:.62;cursor:default}
.ic{width:29px;height:29px;flex:0 0 29px;color:#38bdf8}
.ic svg{width:100%;height:100%;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linejoin:round}
.ic.root{color:#f59e0b}.ic.off{color:#3f4756}
.ot{font-size:13.5px;font-weight:500;white-space:nowrap;display:flex;align-items:center;
  justify-content:space-between;gap:8px;width:100%}
.ot.off{color:#5b6472}
.tag{font:600 8px/1 ui-monospace,monospace;letter-spacing:.07em;text-transform:uppercase;
  padding:4px 6px;border-radius:4px;white-space:nowrap}
.tag.a{color:#38bdf8;background:#0d2f42}
.tag.b{color:#f59e0b;background:#3a2708}
.tag.off{color:#4b5563;background:#191d26}
.tag.act{color:#c7d2fe;background:#312e81;border:1px solid #4f46e5;cursor:pointer}
.tag.live{color:#a5b4fc;background:#1e1b4b;border:1px solid #4338ca;cursor:pointer}
.tag.nop{color:#fca5a5;background:#3f1d1d}

.tray{position:relative;display:flex;justify-content:center;gap:7px;margin:16px 0 15px}
.fl{width:31px;height:31px;border-radius:8px;background:#161b26;border:1px solid #232a39;
  display:flex;align-items:center;justify-content:center;color:#3f4756;cursor:pointer}
.fl svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.7;
  stroke-linecap:round;stroke-linejoin:round}
.fl.needs{color:#f59e0b;border-color:#4a3a13;background:#221a08}
.balloon{position:absolute;bottom:calc(100% + 11px);width:214px;background:#1d232f;
  border:1px solid #333c4d;border-radius:10px;padding:10px 12px;
  box-shadow:0 12px 30px -8px #000;z-index:9}
.balloon .bt{font-size:12.5px;font-weight:600;margin-bottom:2px}
.balloon .bc{font:600 8.5px/1 ui-monospace,monospace;letter-spacing:.06em;
  text-transform:uppercase;color:#f59e0b;margin-bottom:5px}
.balloon .bc.clear{color:#6b7280}
.balloon .bd{font-size:11.5px;line-height:1.45;color:#9aa3b2}
.notch{position:absolute;top:100%;width:11px;height:11px;background:#1d232f;
  border-right:1px solid #333c4d;border-bottom:1px solid #333c4d;
  transform:translate(-50%,-6px) rotate(45deg)}

.btn{background:#5b57e8;color:#fff;text-align:center;padding:12px;border-radius:9px;
  font-size:14px;font-weight:600;margin-top:15px;cursor:pointer}
.btn.flat{margin-top:8px}
.olab{display:block;font-size:14px;color:#9ca3af;margin:4px 0 6px}
.oin{width:100%;background:#1f2937;border:1px solid #374151;border-radius:8px;padding:12px;
  font-size:16px;color:#e5e7eb;outline:none;font-family:inherit;margin-bottom:10px}

.st{display:flex;align-items:center;gap:11px;padding:12px;margin-bottom:8px;
  background:#161b26;border:1px solid #232a39;border-radius:10px}
.st.wait{opacity:.45}
.st.busy{border-color:#4a3a13;background:#1d1808}
.sic{width:29px;height:29px;flex:0 0 29px;color:#4b5563}
.st.busy .sic{color:#f59e0b}.st.done .sic{color:#34d399}
.sic svg{width:100%;height:100%;fill:none;stroke:currentColor;stroke-width:1.6}
.snm{font-size:13.5px;font-weight:500;flex:1}
.spin{width:15px;height:15px;border:2px solid #3a2708;border-top-color:#f59e0b;
  border-radius:50%;animation:sp .7s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.tick{width:17px;height:17px;color:#34d399}
.tick svg{width:100%;height:100%;fill:none;stroke:currentColor;stroke-width:3;
  stroke-linecap:round;stroke-linejoin:round}

.pick{display:flex;align-items:center;gap:11px;padding:12px;margin-bottom:7px;
  background:#161b26;border:1px solid #232a39;border-radius:10px;cursor:pointer}
.pick.on{border-color:#f59e0b;background:#1d1808}
.pick.forced{opacity:.4;cursor:not-allowed}
.chk{width:18px;height:18px;flex:0 0 18px;border-radius:5px;border:1.6px solid #3f4756;
  display:flex;align-items:center;justify-content:center}
.pick.on .chk{background:#f59e0b;border-color:#f59e0b}
.pick.lim.on .chk{background:#38bdf8;border-color:#38bdf8}
.chk svg{width:12px;height:12px;fill:none;stroke:#11141c;stroke-width:3;
  stroke-linecap:round;stroke-linejoin:round;opacity:0}
.pick.on .chk svg{opacity:1}
.pn{font-size:13.5px;font-weight:500;flex:1}
.note{font-size:11.5px;color:#8b93a3;line-height:1.5;margin:12px 0 0;padding:10px 12px;
  background:#161b26;border-radius:9px;border:1px solid #232a39}

.panel{position:relative;background:#1f2430;border:1px solid #374151;border-radius:14px;
  box-shadow:0 16px 36px rgba(0,0,0,.48);overflow:hidden}
.ph{display:flex;align-items:center;gap:10px;padding:12px;border-bottom:1px solid #374151}
.pav{width:32px;height:32px;flex:0 0 32px;border-radius:50%;background:#3f8f7c;color:#fff;
  display:flex;align-items:center;justify-content:center;font:600 13px/1 inherit}
.pnm{font-size:14px;font-weight:600}
.pst{font-size:12px;color:#9ca3af;display:flex;align-items:center;gap:6px;margin-top:1px}
.dot{width:7px;height:7px;border-radius:50%;background:#34d399}
.band{padding:9px 12px 8px}.band .tray{margin:0}
.plab{font:600 11px/1 inherit;letter-spacing:.04em;text-transform:uppercase;color:#9ca3af;
  padding:8px 12px 6px;border-top:1px solid #374151}
.porg,.pact{display:flex;align-items:center;gap:10px;padding:8px 12px;cursor:pointer}
.pact:hover,.porg:hover{background:#252b38}
.pmk{width:26px;height:26px;flex:0 0 26px;border-radius:7px;display:flex;align-items:center;
  justify-content:center;font:600 12px/1 inherit;color:#fff}
.pl{font-size:13.5px}.pd{font-size:11.5px;color:#9ca3af}
.pico{width:26px;height:26px;flex:0 0 26px;display:flex;align-items:center;justify-content:center}
.pico:before{content:'';width:13px;height:15px;border:1.6px solid #9ca3af;border-radius:2px;
  border-top-left-radius:7px;border-top-right-radius:7px;box-sizing:border-box}
.pico.key:before{width:15px;height:15px;border-radius:50% 50% 2px 50%}
.pico.plus:before{width:14px;height:14px;border-radius:2px;border-style:dashed}
.krow{display:flex;align-items:center;gap:9px;padding:9px 12px;border-top:1px solid #2b3240}
.kmid{flex:1;min-width:0}
.knm{font-size:13.5px;display:flex;align-items:center;gap:6px}
.kmeta{font-size:11.5px;color:#9ca3af;margin-top:1px}
.chg{font:600 9px/1 ui-monospace,monospace;letter-spacing:.06em;text-transform:uppercase;
  color:#c7d2fe;background:#312e81;border:1px solid #4f46e5;padding:5px 7px;border-radius:5px;
  cursor:pointer}
.chg:hover{background:#3f3aa0}
.kx{width:24px;height:24px;flex:0 0 24px;border-radius:6px;color:#6b7280;cursor:pointer;
  display:flex;align-items:center;justify-content:center;font-size:17px;line-height:1}
.kx:hover{background:#3f2226;color:#f87171}
.weak{font:600 8px/1 ui-monospace,monospace;letter-spacing:.06em;text-transform:uppercase;
  color:#fbbf24;background:#3a2708;padding:3px 5px;border-radius:3px;margin-left:6px}
.here{font:600 8px/1 ui-monospace,monospace;letter-spacing:.06em;text-transform:uppercase;
  color:#34d399;background:#0d2f22;padding:3px 5px;border-radius:3px}
#s-auth .fui-warn,.screen .fui-warn{margin-top:10px;border-radius:9px;border:1px solid #4b2222}
.fui-warn{font-size:11.5px;color:#fca5a5;background:#2b1616;border-top:1px solid #4b2222;
  padding:10px 12px;line-height:1.5}
.back{font-size:12px;color:#8b93a3;padding:10px 12px;cursor:pointer;
  border-top:1px solid #374151}
.back:hover{color:#e5e7eb}

.sheet{position:absolute;inset:0;border-radius:14px;overflow:hidden;z-index:30}
.shdim{position:absolute;inset:0;background:rgba(0,0,0,.55)}
.shbox{position:absolute;left:0;right:0;bottom:0;background:#e8e8ed;color:#1c1c1e;
  border-radius:14px 14px 0 0;padding:20px 18px 16px;text-align:center}
.shic{width:42px;height:42px;margin:0 auto 10px;color:#1c1c1e}
.shic svg{width:100%;height:100%;fill:none;stroke:currentColor;stroke-width:1.5}
.shttl{font-size:16px;font-weight:600}
.shsub{font-size:13px;color:#6b6b70;margin:2px 0 16px}
.shbtn{background:#0a84ff;color:#fff;border-radius:10px;padding:11px;font-size:15px;
  font-weight:600;cursor:pointer}
.shcancel{font-size:15px;color:#0a84ff;padding:11px;cursor:pointer}
`;

// ── module state ───────────────────────────────────────────────────────────
let host = null;        // the overlay element (null when mounted inside a host drawer)
let cardEl = null;      // the #card the design renders into
let onClosed = null;
let onBackFn = null;    // when set (drawer mode), "Back" returns instead of closing
let M, S;               // M = the backend model (design shape); S = UI state
let armorText = null;   // the current armor, as fetched
let statusData = null;  // /api/identity/status
let trayWired = false;

// ── design constants (verbatim) ──────────────────────────────────────────
const G = {
  face: '<svg viewBox="0 0 24 24"><path d="M4 8.6V6.2A2.2 2.2 0 0 1 6.2 4h2.4M15.4 4h2.4A2.2 2.2 0 0 1 20 6.2v2.4M20 15.4v2.4a2.2 2.2 0 0 1-2.2 2.2h-2.4M8.6 20H6.2A2.2 2.2 0 0 1 4 17.8v-2.4" stroke-linecap="round"/><path d="M9.2 10.2v1.6M14.8 10.2v1.6M12 10.2v3.2M10 15.4a3.6 3.6 0 0 0 4 0" stroke-linecap="round"/></svg>',
  pass: '<svg viewBox="0 0 24 24"><rect x="2.6" y="6.6" width="18.8" height="10.8" rx="2.6"/><circle cx="8" cy="12" r="1.15" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.15" fill="currentColor" stroke="none"/><circle cx="16" cy="12" r="1.15" fill="currentColor" stroke="none"/></svg>',
  both: '<svg viewBox="0 0 24 24"><path d="M4 8.6V6.2A2.2 2.2 0 0 1 6.2 4h2.4M15.4 4h2.4A2.2 2.2 0 0 1 20 6.2v2.4M20 15.4v2.4a2.2 2.2 0 0 1-2.2 2.2h-2.4M8.6 20H6.2A2.2 2.2 0 0 1 4 17.8v-2.4" stroke-linecap="round"/><circle cx="8.4" cy="12" r="1.15" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.15" fill="currentColor" stroke="none"/><circle cx="15.6" cy="12" r="1.15" fill="currentColor" stroke="none"/></svg>',
};
const NAME = { face: 'Passkey', pass: 'Password', both: 'Multi-Factor' };
const K = ['face', 'pass', 'both'];
const TICK = '<div class="tick"><svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7"/></svg></div>';

const FLAGS = [
  { t: 'Background agent', d: 'Nothing is running to fetch your secrets. Unlocking with your root starts it.',
    ok: 'Running. It can fetch secrets without asking you again.',
    g: '<rect x="7.5" y="7.5" width="9" height="9" rx="1.6"/><path d="M10 4v3.5M14 4v3.5M10 16.5V20M14 16.5V20M4 10h3.5M4 14h3.5M16.5 10H20M16.5 14H20"/>' },
  { t: 'Its permission', d: 'Ran out 4 hours ago. Time-limited on purpose, so this is expected.',
    ok: 'Valid for another 3 hours. It renews itself while you are signed in.',
    g: '<path d="M6.5 3h11M6.5 21h11M8 3v3.6c0 1.4 4 3.4 4 5.4 0-2 4-4 4-5.4V3M8 21v-3.6c0-1.4 4-3.4 4-5.4 0 2 4 4 4 5.4V21"/>' },
  { t: 'Your own store', d: 'Not set up yet. Your personal secrets have nowhere to live until it is.',
    ok: 'Set up and working. It follows you to any machine you sign in on.',
    g: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.2"/><circle cx="12" cy="12" r="3.4"/><path d="M12 8.6V6.8M12 17.2v-1.8M15.4 12h1.8M6.8 12h1.8"/>' },
  { t: 'Certificates', d: 'One expires in 6 days. Renewing it needs your root key.',
    ok: 'All current. The next renewal is months away.',
    g: '<circle cx="12" cy="9.2" r="5.2"/><path d="M9 13.6L8 21l4-2.2L16 21l-1-7.4"/>' },
  { t: 'Serving', d: 'Not reachable from outside. Bringing it back needs your root key.',
    ok: 'Reachable from outside. Nothing to do here.',
    g: '<path d="M5 19v-6a7 7 0 0 1 14 0v6"/><path d="M9.5 19v-6a2.5 2.5 0 0 1 5 0v6"/><path d="M3 19h18"/>' },
  { t: 'Approvals', d: 'One request is waiting for you to approve it.',
    ok: 'Nothing is waiting on you.',
    g: '<path d="M4.5 5.5h15a1.5 1.5 0 0 1 1.5 1.5v8a1.5 1.5 0 0 1-1.5 1.5H12l-4.5 3.5v-3.5H4.5A1.5 1.5 0 0 1 3 15V7a1.5 1.5 0 0 1 1.5-1.5z"/><path d="M8.8 11l2.1 2.1 4.3-4.3"/>' },
];

// ── authority policy (verbatim from the design) ──────────────────────────
function rootReachable(m) {
  const capable = m.keys.some((k) => k.prf);
  if (m.mfa) return capable && m.pass.on;
  return m.keys.some((k) => k.prf && k.auth === 'full') || (m.pass.on && m.pass.full);
}
function unlockWays(m) {
  const w = [];
  if (m.mfa) {
    if (m.face.canUnlock && m.keys.length) w.push('face');
    if (m.pass.canUnlock && m.pass.on) w.push('pass');
    if (rootReachable(m)) w.push('both');
  } else { if (m.keys.length) w.push('face'); if (m.pass.on) w.push('pass'); }
  return w;
}
function invalid(m) {
  if (!rootReachable(m)) return 'Nothing would be able to reach your root.';
  if (m.keys.some((k) => !k.prf && k.auth === 'full')) return 'A passkey that cannot derive cannot hold full authority.';
  if (!unlockWays(m).length) return 'Nothing would be able to unlock the dashboard.';
  return null;
}
function applyAuthority(n, f, p, b) {
  n.mfa = !!b;
  if (n.mfa) {
    n.pass.full = false; n.face.canUnlock = !!f; n.pass.canUnlock = !!p;
    n.keys.forEach((k) => { k.auth = 'unlock'; });
  } else {
    n.pass.full = !!p; n.face.canUnlock = true; n.pass.canUnlock = true;
    if (!f) n.keys.forEach((k) => { k.auth = 'unlock'; });
    else if (!n.keys.some((k) => k.prf && k.auth === 'full')) n.keys.forEach((k) => { if (k.prf) k.auth = 'full'; });
  }
  return n;
}
function authoritySig(m) {
  return [m.mfa, m.keys.some((k) => k.prf && k.auth === 'full'), m.pass.full, m.face.canUnlock, m.pass.canUnlock].join('|');
}
function hasAlternative() {
  const cur = authoritySig(M); const pairOK = M.pass.on && M.keys.some((k) => k.prf);
  for (let f = 0; f < 2; f += 1) for (let p = 0; p < 2; p += 1) for (let b = 0; b < 2; b += 1) {
    if (b && !pairOK) continue;
    if (!b && f && !M.face.on) continue;
    if (!b && p && !M.pass.on) continue;
    const n = applyAuthority(JSON.parse(JSON.stringify(M)), f, p, b);
    if (invalid(n)) continue;
    if (authoritySig(n) !== cur) return true;
  }
  return false;
}
function anyKeyFull() { return M.keys.some((x) => x.auth === 'full' && x.prf === true); }
function level(k) {
  if (k === 'both') return M.mfa ? 'b' : 'off';
  if (!M[k].on) return 'en';
  if (M.mfa) return M[k].canUnlock ? 'a' : 'off';
  if (k === 'face') return anyKeyFull() ? 'b' : 'a';
  return M.pass.full ? 'b' : 'a';
}
function actionOn(k) {
  if (k === 'both') {
    if (M.mfa) return hasAlternative() ? 'change' : null;
    return (M.face.on && M.pass.on && M.keys.some((x) => x.prf)) ? 'enable' : null;
  }
  if (!M[k].on) return 'enroll';
  if (M.mfa) return null;
  if (!hasAlternative()) return null;
  return M[k].full ? 'change' : 'upgrade';
}
function rootFactor() {
  if (M.mfa) return 'both';
  if (M.face.on && level('face') === 'b') return 'face';
  if (M.pass.on && level('pass') === 'b') return 'pass';
  return null;
}
function narrowest() {
  if (M.face.on && level('face') !== 'off') return 'face';
  if (M.pass.on && level('pass') !== 'off') return 'pass';   // (design's k= global removed for strict mode)
  return M.mfa ? 'both' : null;
}

// ── real-identity helpers ────────────────────────────────────────────────
function displayName() {
  return (statusData && statusData.personal_identity && statusData.personal_identity.display_name) || '';
}
function initial() { return (displayName() || '?').trim().charAt(0).toUpperCase(); }

// Build the design's M shape from the real /status + parsed armor.
async function loadModel() {
  const st = await (await fetch('/api/identity/status', {
    credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' },
  })).json();
  const pj = await (await fetch('/api/identity/personal', {
    credentials: 'same-origin', headers: { Accept: 'application/json' },
  })).json();
  if (pj && pj.error) throw new Error(pj.error);
  armorText = pj.armored_private_key;
  statusData = st;
  const data = primitives.parseArmor(armorText);
  const factors = data.factors || [];
  const combined = factors.find((f) => f.type === 'combined') || null;
  const mfa = !!combined;
  const pwF = factors.find((f) => f.type === 'password') || null;
  const rootIds = {};
  factors.filter((f) => f.type === 'passkey').forEach((f) => { rootIds[f.credential_id] = 1; });
  const keys = (st.passkeys || []).map((p) => ({
    l: p.label || 'Passkey',
    v: (p.transports && p.transports.length) ? p.transports.join(', ') : 'passkey',
    prf: !!p.provisioning_public_key,
    w: p.created_at || '',
    here: p.rp_id === st.rp_id ? 1 : 0,
    auth: rootIds[p.credential_id] ? 'full' : 'unlock',
    credentialId: p.credential_id,
    provisioningPub: p.provisioning_public_key || null,
  }));
  const iters = (pwF && pwF.kdf && pwF.kdf.iterations)
    || (combined && combined.kdf && combined.kdf.iterations) || 0;
  M = {
    face: { on: keys.length > 0, full: keys.some((k) => k.prf && k.auth === 'full'), canUnlock: true },
    pass: { on: !!pwF || mfa, full: !!pwF && !mfa, canUnlock: !mfa },
    mfa,
    lit: [],
    pw: {
      kdf: 'PBKDF2',
      mem: iters,
      // 600000 is the PBKDF2 iteration COUNT, not a memory size — label it as
      // iterations, grouped (600,000). created_at is a creation time, not a
      // change; render it in the viewer's local time zone, never raw UTC.
      itersLabel: (Number(iters) || 0).toLocaleString() + ' iterations',
      createdLabel: pj.created_at
        ? 'created ' + new Date(pj.created_at).toLocaleString()
        : '',
    },
    kdfNow: { mem: 600000 },
    keys,
    rootCached: false,
  };
  if (!S) S = { screen: 'keys', method: null, act: null, need: [], si: 0, after: null, sheet: null, pick: null, primed: 0 };
}

// ── real ceremony layer (replaces the mock commit()) ─────────────────────
// Report a client-side ceremony failure to the server so it survives past the
// browser (the crypto runs here, so these never reach the dashboard log on their
// own). DIAGNOSTIC ONLY — error text + non-secret context; NEVER a password,
// PRF, seed, or armor. Best-effort: telemetry must never break a ceremony.
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
        context: {
          mfa: !!(M && M.mfa),
          passwordOn: !!(M && M.pass && M.pass.on),
          passkeys: (M && M.keys) ? M.keys.length : 0,
          screen: (S && S.screen) || null,
        },
      }),
    }).catch(() => {});
  } catch (e) { /* diagnostics must never throw */ }
}

async function doRearm(opener, action, requirePair) {
  try {
    const body = await primitives.signArmorUpdate(armorText, opener, action, requirePair);
    const r = await fetch('/api/identity/personal/armor', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) throw new Error(j.error || ('re-arm failed (' + r.status + ')'));
    return true;
  } catch (err) {
    S.warn = (err && err.message) || String(err);
    reportCeremonyError('rearm', action && action.kind, err);
    return false;
  }
}

// The opener a root-mutating ceremony must present is the one the re-arm
// primitive actually unwraps with. Under MFA that is BOTH halves (the combined
// factor). Off MFA, EVERY re-arm the panel issues — set/remove password, add a
// passkey factor, enable MFA, promote/demote — unwraps the master KEK through
// the PASSWORD when one exists (the passkey-native re-arms are only reachable on
// a passkey-only armor); so prefer the password, and fall back to the passkey
// only when there is no password. Preferring rootFactor() here (which is the
// passkey on a password+passkey armor) handed the password primitives no
// passphrase — the wiring gap behind "can't upgrade / can't add a password".
function rootNeed() {
  if (M.mfa) return ['face', 'pass'];
  if (M.pass.on) return ['pass'];
  return ['face'];
}
function rootOpener() {
  if (M.mfa) return { password: S.password, prf: S.prf };
  if (S.password) return { password: S.password };
  return { prf: S.prf };
}

// A WebAuthn PRF assertion; `credentialId` (base64url) scopes it to one device.
async function getPrf(credentialId) {
  if (!window.PublicKeyCredential || !navigator.credentials) throw new Error('this browser cannot use Face ID');
  const rpId = statusData.rp_id || undefined;
  const allow = credentialId
    ? [{ type: 'public-key', id: b64u(credentialId) }]
    : (statusData.passkeys || []).filter((p) => p.credential_id && (!rpId || p.rp_id === rpId))
      .map((p) => ({ type: 'public-key', id: b64u(p.credential_id) }));
  let asrt;
  try {
    asrt = await navigator.credentials.get({ publicKey: {
      challenge: crypto.getRandomValues(new Uint8Array(32)),
      rpId, allowCredentials: allow, userVerification: 'required', extensions: prfEvalExtension(),
    } });
  } catch (e) { if (e && e.name === 'NotAllowedError') throw new Error('Face ID was cancelled — try again'); throw e; }
  const prf = prfOutputFromResults(asrt.getClientExtensionResults());
  if (!prf) throw new Error('this passkey has no PRF and cannot hold or pair a key');
  return prf;
}
function b64u(s) {
  let b = s.replace(/-/g, '+').replace(/_/g, '/'); while (b.length % 4) b += '=';
  const bin = atob(b); const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i); return out;
}

// authScreen Save → the real re-arm the chosen authority state implies.
async function saveAuthorityReal(pick) {
  const wasMfa = M.mfa;
  const prfKey = M.keys.filter((x) => x.prf)[0];
  if (!wasMfa && pick.both) {
    if (!prfKey) { S.warn = 'A passkey that can derive a key is required to pair.'; return false; }
    return doRearm(rootOpener(), { kind: 'enableMfa', credentialId: prfKey.credentialId, provisioningPub: prfKey.provisioningPub }, true);
  }
  if (wasMfa && !pick.both) {
    return doRearm(rootOpener(), { kind: 'disableMfa', credentialId: prfKey && prfKey.credentialId }, false);
  }
  if (!wasMfa && !pick.both) {
    const wantFace = pick.face; const haveFace = anyKeyFull();
    if (wantFace !== haveFace && prfKey) {
      return wantFace
        ? doRearm(rootOpener(), { kind: 'promote', credentialId: prfKey.credentialId, provisioningPub: prfKey.provisioningPub }, false)
        : doRearm(rootOpener(), { kind: 'demote', credentialId: prfKey.credentialId }, false);
    }
    if (M.pass.on && !pick.pass) { S.warn = 'A password without Multi-Factor always reaches the root; it cannot be unlock-only.'; return false; }
    return true;   // no effective change
  }
  // MFA on, staying on: per-single canUnlock changes have no backend op yet.
  S.warn = 'Per-factor unlock settings under Multi-Factor are not yet supported.'; return false;
}
async function changeKeyReal(idx, to) {
  const k = M.keys[idx];
  if (to === 'full') {
    let pub = k.provisioningPub;
    if (S.prf) pub = (await deriveProvisioningKey(S.prf)).publicKeyHex;
    if (!pub) { S.warn = 'This passkey has no provisioning key to promote.'; return false; }
    return doRearm(rootOpener(), { kind: 'promote', credentialId: k.credentialId, provisioningPub: pub }, false);
  }
  return doRearm(rootOpener(), { kind: 'demote', credentialId: k.credentialId }, false);
}
async function setPasswordReal(next) { return doRearm(rootOpener(), { kind: 'setPassword', newPassword: next }, false); }
async function addPasswordReal(next) { return doRearm(rootOpener(), { kind: 'addPassword', newPassword: next }, false); }
// On a combined (MFA) armor there is no standalone password to set or change —
// the password lives INSIDE the require-both pair. A password that opens on its
// own is the opposite of require-both, so establishing one dissolves the pair:
// disable_mfa yields a standalone password (this new value) AND a standalone
// passkey, either of which then opens the root.
async function disableMfaSetPasswordReal(next) { return doRearm(rootOpener(), { kind: 'disableMfa', newPassword: next }, false); }
async function removePasswordReal() { return doRearm(rootOpener(), { kind: 'removePassword' }, false); }
async function removeDeviceReal(idx) {
  const k = M.keys[idx];
  try {
    const r = await fetch('/api/identity/passkey/' + encodeURIComponent(k.credentialId), {
      method: 'DELETE', credentials: 'same-origin',
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) throw new Error(j.error || ('remove failed (' + r.status + ')'));
    return true;
  } catch (err) {
    S.warn = (err && err.message) || String(err);
    reportCeremonyError('removeDevice', 'delete', err);
    return false;
  }
}
async function addDeviceReal() {
  let opened;
  try {
    // Open with the CURRENT state's root factor — under MFA that is the
    // combined (password + passkey), not the password alone. Opening with the
    // password alone is exactly what stopped "add a passkey" working under MFA.
    opened = M.mfa
      ? await primitives.decryptArmorWithCombined(armorText, S.password, S.prf)
      : S.password
        ? await primitives.decryptArmor(armorText, S.password)
        : await primitives.decryptArmorWithPasskey(armorText, S.prf);
  } catch (e) {
    S.warn = 'that root proof did not open your identity — try again';
    reportCeremonyError('enroll', 'open', e);
    return false;
  }
  const seed = opened.seed;
  try {
    const signingKey = await primitives.importEd25519RootSigningKey(seed);
    await enrollPasskey({ root: { signingKey, publicHex: opened.rootPub } });
    return true;
  } catch (err) {
    S.warn = (err && err.message) || String(err);
    reportCeremonyError('enroll', 'create', err);
    return false;
  }
  finally { seed.fill(0); }
}

// After a successful ceremony: refresh from the server and show the Factors screen.
async function afterCommit(nextScreen) {
  await loadModel();
  S.prf = null; S.act = null; S.method = null; S.pick = null; S.warn = null;
  S.screen = nextScreen || 'keys';
  render();
}

// Gather the root (password and/or Face ID) then run `after`. `need` is the
// explicit list of factors to prove (['pass'], ['face'], or ['face','pass']).
function gatherThen(after, need, extra) {
  if (extra) Object.assign(S, extra);
  S.warn = null;
  const have = need.every((n) => (n === 'pass' ? !!S.password : !!S.prf));
  if (have) { runAfter(after); return; }
  S.need = need; S.si = 0; S.after = after; S.screen = 'prog';
  S.sheet = (S.need[0] === 'face') ? 'use' : null;
  render();
}
function runAfter(after) {
  if (after === 'authorize') { S.screen = 'authorize'; S.primed = 0; render(); return; }
  if (after === 'setpw') { S.screen = 'setpw'; render(); return; }
  if (after === 'create') { S.screen = 'keys'; S.sheet = 'create'; render(); return; }
  if (after === 'applykey') { applyKey(); return; }
  if (after === 'removepw') { removePasswordReal().then((ok) => { if (ok) afterCommit('keys'); else render(); }); return; }
  if (after === 'removekey') { removeDeviceReal(S.keyIdx).then((ok) => { if (ok) afterCommit('keys'); else render(); }); return; }
}

// ── the flag tray, shared by both surfaces (verbatim) ──────────────────────
function tray() {
  const t = document.createElement('div'); t.className = 'tray';
  FLAGS.forEach((f, i) => {
    const e = document.createElement('div');
    e.className = 'fl' + (M.lit.indexOf(i) >= 0 ? ' needs' : '');
    e.innerHTML = '<svg viewBox="0 0 24 24">' + f.g + '</svg>';
    e.onclick = (ev) => {
      ev.stopPropagation();
      const open = t.querySelector('.balloon');
      if (open && t.dataset.at === String(i)) { t.dataset.at = ''; open.remove(); return; }
      if (open) open.remove(); t.dataset.at = i;
      const on = M.lit.indexOf(i) >= 0;
      const b = document.createElement('div'); b.className = 'balloon';
      b.innerHTML = '<div class="bt">' + f.t + '</div><div class="bc' + (on ? '' : ' clear') + '">'
        + (on ? '1 alert' : 'no alerts') + '</div><div class="bd">' + (on ? f.d : f.ok)
        + '</div><div class="notch"></div>';
      t.appendChild(b);
      const hostEl = t.closest('.screen') || t.closest('.panel');
      const c = e.offsetLeft + e.offsetWidth / 2; let left = c - b.offsetWidth / 2; const base = t.offsetLeft;
      left = Math.max(-base + 8, Math.min(left, hostEl.clientWidth - b.offsetWidth - base - 8));
      b.style.left = left + 'px'; b.querySelector('.notch').style.left = (c - left) + 'px';
    };
    t.appendChild(e);
  });
  return t;
}

function el(tag, cls, html) {
  const n = document.createElement(tag);
  if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n;
}

// ── screens (markup verbatim; only mock hooks swapped) ─────────────────────
function lockScreen() {
  const s = el('div', 'screen');
  s.appendChild(el('div', 'hd', 'Dashboard locked'));
  s.appendChild(el('div', 'av', initial()));
  s.appendChild(el('div', 'nm', displayName()));
  if (!S.method) S.method = narrowest();
  K.forEach((k) => {
    const lv = level(k); const act = actionOn(k); const sel = (S.method === k && !S.act);
    const pickable = (k === 'both') ? M.mfa : (M[k].on && level(k) !== 'off');
    const row = el('div', 'opt' + (sel ? ' sel' : '') + (pickable ? '' : ' dim'));
    const LV = { a: 'unlock only', b: 'full authority', off: 'disabled', en: 'not set up' };
    let tag; const creates = (act === 'enroll' || act === 'enable');
    if (S.act === k) tag = '<span class="tag live" data-a="' + k + '">' + ({ enroll: 'enrolling', enable: 'enabling', upgrade: 'upgrading', change: 'changing' })[act] + '&hellip;</span>';
    else if (creates) tag = '<span class="tag act" data-a="' + k + '">' + act + '</span>';
    else if (act) tag = '<span class="tag ' + lv + '" data-a="' + k + '" style="cursor:pointer">' + LV[lv] + '</span>';
    else tag = '<span class="tag ' + lv + '">' + LV[lv] + '</span>';
    row.innerHTML = '<div class="ic ' + (lv === 'b' ? 'root' : (lv === 'a' ? '' : 'off')) + '">' + G[k] + '</div>'
      + '<div class="ot' + (pickable ? '' : ' off') + '">' + NAME[k] + tag + '</div>';
    if (pickable) row.onclick = (e) => { e.stopPropagation(); S.act = null; S.method = k; render(); };
    const b = row.querySelector('[data-a]');
    if (b) b.onclick = (e) => { e.stopPropagation(); S.act = (S.act === k) ? null : k; render(); };
    s.appendChild(row);
  });
  s.appendChild(tray());
  const btn = el('div', 'btn');
  if (S.act) {
    const a = actionOn(S.act); const cap = a.charAt(0).toUpperCase() + a.slice(1);
    btn.textContent = (a === 'change') ? 'Unlock to Change Authority' : ('Unlock to ' + cap + ' ' + NAME[S.act]);
  } else btn.textContent = S.method ? ('Unlock with ' + NAME[S.method]) : 'Unlock';
  btn.onclick = (e) => { e.stopPropagation(); begin(); };
  s.appendChild(btn);
  return s;
}

function begin() {
  const m = S.act || S.method;
  if (!m) return;
  if (S.act) {
    const a = actionOn(S.act); const root = rootFactor();
    S.need = root === 'both' ? ['face', 'pass'] : (root ? [root] : ['pass']);
    S.after = (a === 'enroll' && S.act === 'pass') ? 'setpw' : (a === 'enroll' && S.act === 'face') ? 'create' : 'authorize';
  } else { S.need = (m === 'both') ? ['face', 'pass'] : [m]; S.after = 'unlocked'; }
  S.si = 0; S.screen = 'prog';
  S.sheet = (S.need[0] === 'face') ? 'use' : null;
  render();
}

function stepDone() {
  S.si += 1;
  if (S.si < S.need.length) { S.sheet = (S.need[S.si] === 'face') ? 'use' : null; render(); return; }
  S.sheet = null;
  if (S.after === 'unlocked') { finishUnlock(); }
  else runAfter(S.after);
}

function finishUnlock() { S.screen = 'keys'; S.act = null; render(); }

// The concise 2–3 word status for the current ceremony step, shown as a header
// so the operator always knows which step is happening — especially the
// two-prompt passkey enroll (Authorizing → Enrolling).
function ceremonyPhase() {
  if (S.sheet === 'create') return 'Enrolling';
  if (S.sheet === 'present') return 'Confirming';
  if (S.after === 'unlocked') return 'Unlocking';
  return 'Authorizing';
}

function progScreen() {
  const s = el('div', 'screen');
  s.appendChild(el('div', 'hd', ceremonyPhase()));
  S.need.forEach((k, i) => {
    const st = i < S.si ? 'done' : (i === S.si ? 'busy' : 'wait');
    const r = el('div', 'st ' + st, '<div class="sic">' + G[k] + '</div><div class="snm">' + NAME[k] + '</div>'
      + (st === 'done' ? TICK : st === 'busy' ? '<div class="spin"></div>' : ''));
    s.appendChild(r);
    if (k === 'pass' && st === 'busy' && !S.sheet) {
      s.appendChild(el('label', 'olab', 'Enter your password'));
      const i2 = el('input', 'oin'); i2.type = 'password'; i2.autocomplete = 'current-password'; s.appendChild(i2);
      const b = el('div', 'btn flat', 'Continue');
      b.onclick = (e) => { e.stopPropagation(); if (!i2.value) { i2.focus(); return; } S.password = i2.value; stepDone(); };
      s.appendChild(b);
      setTimeout(() => i2.focus(), 0);
    }
  });
  return s;
}

function setpwScreen() {
  const s = el('div', 'screen');
  const title = M.mfa ? 'New password'
    : (S.pwMode === 'changepw') ? 'Change password' : 'Set password';
  s.appendChild(el('div', 'ttl', title));
  if (M.mfa) {
    s.appendChild(el('div', 'note', 'This password will open your identity on its own. '
      + 'Because a password that opens on its own is the opposite of require-both, saving it '
      + 'turns off Multi-Factor — your passkey will then also open on its own.'));
  }
  s.appendChild(el('label', 'olab', 'Choose a password'));
  const a = el('input', 'oin'); a.type = 'password'; a.autocomplete = 'new-password'; s.appendChild(a);
  s.appendChild(el('label', 'olab', 'Confirm password'));
  const b = el('input', 'oin'); b.type = 'password'; b.autocomplete = 'new-password'; s.appendChild(b);
  const go = el('div', 'btn', M.mfa ? 'Save & turn off Multi-Factor' : 'Continue');
  go.onclick = async (e) => {
    e.stopPropagation();
    if (!a.value) { a.focus(); return; }
    if (a.value !== b.value) { S.warn = 'Passwords do not match.'; render(); return; }
    const ok = M.mfa
      ? await disableMfaSetPasswordReal(a.value)
      : (S.pwMode === 'changepw') ? await setPasswordReal(a.value) : await addPasswordReal(a.value);
    if (!ok) { render(); return; }
    S.pwMode = null; await afterCommit('keys');
  };
  s.appendChild(go);
  if (S.warn) s.appendChild(el('div', 'fui-warn', S.warn));
  return s;
}

function authScreen() {
  const s = el('div', 'screen');
  s.appendChild(el('div', 'ttl', 'Authorize your factors'));
  if (!S.pick || !S.primed) {
    S.pick = M.mfa
      ? { face: M.face.canUnlock, pass: M.pass.canUnlock, both: true }
      : { face: anyKeyFull(), pass: M.pass.on && M.pass.full, both: false };
    if (S.act === 'face' || S.act === 'pass') { S.pick[S.act] = true; S.pick.both = false; }
    if (S.act === 'both' && !M.mfa) { S.pick.both = true; S.pick.face = true; S.pick.pass = true; }
    if (!S.pick.face && !S.pick.pass && !S.pick.both) S.pick[narrowest()] = true;
    S.primed = 1;
  }
  K.forEach((k) => {
    if (k !== 'both' && !M[k].on) return;
    if (k === 'both' && !(M.pass.on && M.keys.some((x) => x.prf))) return;
    const on = S.pick[k]; const lim = S.pick.both && k !== 'both';
    const lbl = k === 'both' ? (on ? 'full authority' : 'disabled')
      : lim ? (on ? 'unlock only' : 'disabled') : (on ? 'full authority' : 'unlock only');
    const cls = k === 'both' ? (on ? 'b' : 'off') : (lim ? (on ? 'a' : 'off') : (on ? 'b' : 'a'));
    const r = el('div', 'pick' + (on ? ' on' : '') + (lim ? ' lim' : ''),
      '<div class="chk"><svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7"/></svg></div>'
      + '<div class="pn">' + NAME[k] + '</div><span class="tag ' + cls + '">' + lbl + '</span>');
    r.onclick = (e) => {
      e.stopPropagation();
      if (k === 'both') {
        S.pick.both = !S.pick.both;
        if (S.pick.both) { S.pick.face = M.face.on; S.pick.pass = M.pass.on; }
        else { S.pick.face = M.face.on; S.pick.pass = false; if (!S.pick.face) S.pick.pass = M.pass.on; }
      } else {
        S.pick[k] = !S.pick[k];
        if (!S.pick.both && !S.pick.face && !S.pick.pass) S.pick[k] = true;
      }
      render();
    };
    s.appendChild(r);
  });
  let note;
  if (S.pick.both) {
    note = 'Enabling Multi-Factor will require both your password and your passkey in order to grant full authority.';
    const dead = []; if (M.face.on && !S.pick.face) dead.push('passkey'); if (M.pass.on && !S.pick.pass) dead.push('password');
    if (dead.length === 2) note += ' Neither will unlock the dashboard on its own.';
    else if (dead.length) note += ' Your ' + dead[0] + ' will not unlock the dashboard on its own.';
  } else {
    note = (S.pick.face && S.pick.pass) ? 'Either your password or your passkey can individually grant full authority.'
      : S.pick.face ? 'Only your passkey will grant full authority.' : 'Only your password will grant full authority.';
  }
  s.appendChild(el('div', 'note', note));
  if (S.warn) s.appendChild(el('div', 'fui-warn', S.warn));
  const save = el('div', 'btn', 'Save');
  save.onclick = async (e) => {
    e.stopPropagation();
    const pk = S.pick;
    const ok = await saveAuthorityReal(pk);
    if (!ok) { render(); return; }
    await afterCommit('keys');
  };
  s.appendChild(save);
  return s;
}

function panelScreen() {
  const p = el('div', 'panel');
  p.appendChild(el('div', 'ph', '<div class="pav">' + initial() + '</div><div><div class="pnm">' + displayName()
    + '</div><div class="pst"><span class="dot"></span>Unlocked</div></div>'));
  const band = el('div', 'band'); band.appendChild(tray()); p.appendChild(band);
  const mk = el('div', 'pact', '<div class="pico key"></div><div><div class="pl">Manage my factors'
    + '</div><div class="pd">Change your password, add or remove a device</div></div>');
  mk.onclick = (e) => { e.stopPropagation(); S.screen = 'keys'; render(); };
  p.appendChild(mk);
  return p;
}

function keysScreen() {
  const p = el('div', 'panel');
  const n = M.keys.length + (M.pass.on ? 1 : 0);
  p.appendChild(el('div', 'ph', '<div class="pav">' + initial() + '</div><div><div class="pnm">Factors</div>'
    + '<div class="pst">' + n + ' on this identity</div></div>'));

  p.appendChild(el('div', 'plab', 'Password'));
  if (!M.pass.on) {
    const add0 = el('div', 'pact', '<div class="pico plus"></div><div><div class="pl">Set a password'
      + '</div><div class="pd">Reach your identity where there is no passkey</div></div>');
    add0.onclick = (e) => { e.stopPropagation(); S.pwMode = 'enrollpw'; gatherThen('setpw', rootNeed()); };
    p.appendChild(add0);
  } else {
    const lv = level('pass');
    // The authority editor (authScreen) is the single common control for every
    // authority change, in EVERY state — including MFA, where the only legal
    // move is dissolving the pair. Gating it off under MFA left the panel with
    // no path back to password+passkey, so the badge stays tappable whenever an
    // alternative authority state exists.
    const canTap = hasAlternative();
    const ptag = '<span class="tag ' + lv + '"' + (canTap ? ' data-p="1" style="cursor:pointer"' : '') + '>'
      + ({ a: 'unlock only', b: 'full authority', off: 'disabled' })[lv] + '</span>';
    const weak = M.pw.mem < M.kdfNow.mem;
    const pr = el('div', 'krow', '<div class="kmid"><div class="knm">Password'
      + (weak ? '<span class="weak">below current strength</span>' : '') + '</div>'
      + '<div class="kmeta">' + M.pw.kdf + ' &middot; ' + M.pw.itersLabel
      + (M.pw.createdLabel ? ' &middot; ' + M.pw.createdLabel : '') + '</div></div>' + ptag
      + '<div class="chg">Change</div><div class="kx">&times;</div>');
    const pb = pr.querySelector('[data-p]');
    if (pb) pb.onclick = (e) => { e.stopPropagation(); gatherThen('authorize', rootNeed()); };
    pr.querySelector('.chg').onclick = (e) => { e.stopPropagation(); S.pwMode = 'changepw'; gatherThen('setpw', rootNeed()); };
    pr.querySelector('.kx').onclick = (e) => { e.stopPropagation(); gatherThen('removepw', rootNeed()); };
    p.appendChild(pr);
  }

  p.appendChild(el('div', 'plab', 'Passkeys'));
  M.keys.forEach((k, i) => {
    const tag = k.prf === false
      ? '<span class="tag nop">' + (M.mfa ? 'cannot pair' : 'no prf') + '</span>'
      : '<span class="tag ' + (k.auth === 'full' ? 'b' : 'a') + '" data-k="' + i + '" style="cursor:pointer">'
        + (k.auth === 'full' ? 'full authority' : 'unlock only') + '</span>';
    const r = el('div', 'krow', '<div class="kmid"><div class="knm">' + k.l
      + (k.here ? '<span class="here">this device</span>' : '') + '</div><div class="kmeta">'
      + k.v + ' &middot; added ' + k.w + '</div></div>' + tag + '<div class="kx">&times;</div>');
    const kb = r.querySelector('[data-k]');
    // Under MFA a passkey cannot be raised on its own — a full standalone
    // passkey and require-both are contradictory — so tapping its authority
    // opens the same authority editor (where dissolving the pair makes it full).
    // Off MFA, the direct promote/demote path applies.
    if (kb) kb.onclick = (e) => {
      e.stopPropagation();
      if (M.mfa) gatherThen('authorize', rootNeed()); else changeKey(i);
    };
    r.querySelector('.kx').onclick = (e) => { e.stopPropagation(); gatherThen('removekey', rootNeed(), { keyIdx: i }); };
    p.appendChild(r);
  });
  if (S.warn) p.appendChild(el('div', 'fui-warn', S.warn));
  const add = el('div', 'pact', '<div class="pico plus"></div><div><div class="pl">Add a passkey</div>'
    + '<div class="pd">This device, or scan from a phone</div></div>');
  add.onclick = (e) => { e.stopPropagation(); gatherThen('create', rootNeed()); };
  p.appendChild(add);
  const back = el('div', 'back', '&lsaquo; Back');
  back.onclick = (e) => { e.stopPropagation(); if (onBackFn) onBackFn(); else close(); };
  p.appendChild(back);
  return p;
}

// Changing a factor's authority needs the root proven; then land on authorize.
function changeKey(i) {
  const k = M.keys[i];
  if (k.prf === false) return;
  const want = (k.auth === 'full') ? 'unlock' : 'full';
  const probe = JSON.parse(JSON.stringify(M)); probe.keys[i].auth = want;
  const why = invalid(probe);
  if (why) { S.warn = why; render(); return; }
  S.warn = null; S.keyIdx = i; S.keyTo = want;
  gatherThen('applykey', rootNeed());
}
function applyKey() {
  if (S.keyTo === 'full' && !S.presented) { S.presented = 1; S.sheet = 'present'; render(); return; }
  const idx = S.keyIdx; const to = S.keyTo;
  changeKeyReal(idx, to).then((ok) => {
    S.presented = 0; S.keyIdx = null;
    if (ok) afterCommit('keys'); else { S.screen = 'keys'; render(); }
  });
}

function sheet() {
  const creating = S.sheet === 'create'; const presenting = S.sheet === 'present';
  // 2–3 word phase label so a two-step enroll reads clearly: Authorizing (use)
  // → Enrolling (create). Present = confirming the specific key being raised.
  const title = creating ? 'Enrolling' : presenting ? 'Confirming' : 'Authorizing';
  const sub = creating ? 'Create a passkey'
    : presenting ? ('Present ' + M.keys[S.keyIdx].l)
      : 'Use your passkey' + ((statusData && statusData.rp_id) ? ' for ' + statusData.rp_id : '');
  const s = el('div', 'sheet', '<div class="shdim"></div><div class="shbox">'
    + '<div class="shic">' + G.face + '</div><div class="shttl">' + title + '</div>'
    + '<div class="shsub">' + sub + '</div><div class="shbtn">Continue</div>'
    + '<div class="shcancel">Cancel</div>');
  s.querySelector('.shbtn').onclick = async (e) => {
    e.stopPropagation();
    if (creating) {
      const ok = await addDeviceReal();
      S.sheet = null;
      if (ok) afterCommit('keys'); else render();
      return;
    }
    // 'use' (a passkey root step) or 'present' (the key being promoted): real PRF.
    try {
      S.prf = await getPrf(presenting ? M.keys[S.keyIdx].credentialId : null);
    } catch (err) {
      S.warn = (err && err.message) || String(err);
      reportCeremonyError('prf', presenting ? 'present' : 'authorize', err);
      S.sheet = null; render(); return;
    }
    if (presenting) { S.sheet = null; applyKey(); return; }
    S.sheet = null; stepDone();
  };
  s.querySelector('.shcancel').onclick = (e) => {
    e.stopPropagation();
    S.sheet = null; S.presented = 0;
    if (presenting) { S.keyIdx = null; S.screen = 'keys'; }
    else if (S.screen === 'prog') { S.screen = 'keys'; S.act = null; }
    render();
  };
  return s;
}

function render() {
  if (!cardEl) return;
  cardEl.innerHTML = '';
  if (!M) { cardEl.appendChild(el('div', 'panel', '<div class="ph"><div><div class="pnm">Factors</div><div class="pst">' + (S && S.warn ? S.warn : 'Loading…') + '</div></div></div>')); return; }
  const body = S.screen === 'lock' ? lockScreen()
    : S.screen === 'prog' ? progScreen()
      : S.screen === 'setpw' ? setpwScreen()
        : S.screen === 'authorize' ? authScreen()
          : S.screen === 'panel' ? panelScreen()
            : keysScreen();
  cardEl.appendChild(body);
  if (S.sheet) cardEl.appendChild(sheet());
}

// ── entry / plumbing ──────────────────────────────────────────────────────
function injectStyles() {
  if (document.getElementById('factor-ui-styles')) return;
  const el2 = document.createElement('style'); el2.id = 'factor-ui-styles'; el2.textContent = STYLE;
  document.head.appendChild(el2);
}
function close() {
  if (S) S.password = null;
  if (host && host.parentNode) host.parentNode.removeChild(host);
  if (cardEl && cardEl.parentNode) cardEl.parentNode.removeChild(cardEl);
  host = null; cardEl = null; onBackFn = null;
  if (typeof onClosed === 'function') onClosed();
}
// open({onClose}) → full-screen overlay (default). open({mount, onBack, onClose})
// → render the SAME designed screens inside `mount` (e.g. the profile-settings
//   drawer), filling its width; "Back" calls onBack instead of closing.
async function open(opts) {
  onClosed = (opts && opts.onClose) || null;
  onBackFn = (opts && opts.onBack) || null;
  injectStyles();
  M = null; S = null;
  const mount = opts && opts.mount;
  cardEl = document.createElement('div'); cardEl.className = 'card';
  if (mount) {
    host = null;
    cardEl.style.maxWidth = 'none';   // the drawer is wider than the modal card
    mount.appendChild(cardEl);
  } else {
    host = document.createElement('div');
    host.className = 'fui-overlay';
    host.setAttribute('data-testid', 'factor-management');
    host.addEventListener('click', (e) => { if (e.target === host) close(); });
    host.appendChild(cardEl);
    document.body.appendChild(host);
  }
  if (!trayWired) {
    trayWired = true;
    document.addEventListener('click', () => {
      [].forEach.call(document.querySelectorAll('.fui-overlay .tray'), (t) => {
        const b = t.querySelector('.balloon'); if (b) { t.dataset.at = ''; b.remove(); }
      });
    });
  }
  render();
  try { await loadModel(); } catch (e) { if (!S) S = { screen: 'keys' }; S.warn = (e && e.message) || String(e); }
  render();
}

export { open };
