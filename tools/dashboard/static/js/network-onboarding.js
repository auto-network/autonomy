/* "Get started" onboarding — personal identity + passkey enrollment.
 *
 * The 3-step ceremony from the converged mockup (design d49be06b):
 * You → This device → Organizations. This surface CREATES the personal
 * root (distinct from the org key C1 mints) and enrolls a WebAuthn
 * passkey for this device; it is enrollment only — the sign-in gate
 * that enforces it is a later bead.
 *
 *   Step 1 (You): name + password → generate an Ed25519 personal root
 *     in this page, armor it with the password (the SAME canonical
 *     armor as network-identity.js — its crypto internals are reused,
 *     so one implementation serves both ceremonies), and store ONLY the
 *     armor via POST /api/identity/personal (I1: the plaintext seed is
 *     zeroed the moment the armor exists; the password never leaves
 *     the browser).
 *   Step 2 (This device): navigator.credentials.create() against
 *     options minted by the server — the RP ID follows the host the
 *     page is on, so localhost and the .ts.net name both enroll
 *     working, domain-bound passkeys. Access only, never signing.
 *   Step 3 (Organizations): what's already on this machine ("found
 *     here"), plus the entry into the C1 create-org ceremony.
 *
 * ACTIVATION: on load, /api/identity/status decides. No personal
 * identity or no passkey → the flow opens (resuming at the first
 * incomplete step), including on accounts that predate the identity
 * system — running it ADDS identity on top of existing data. "Not now"
 * snoozes for 24h; the sign-in panel's "Get started" reopens it any
 * time.
 *
 * Depends on network-signon.js + network-identity.js (loaded first in
 * base.html) for the armor/keygen internals.
 */
(function () {
  'use strict';

  var SNOOZE_KEY = 'autonomy.onboarding.snoozedUntil';
  var SNOOZE_MS = 24 * 3600 * 1000;

  // ── helpers ────────────────────────────────────────────────────────

  function _idI() {
    var m = window.AutonomyNetworkIdentity;
    if (!m || !m._internals) {
      throw new Error('network-identity.js must load before network-onboarding.js');
    }
    return m._internals;
  }

  function b64uToBytes(s) {
    var b64 = s.replace(/-/g, '+').replace(/_/g, '/');
    while (b64.length % 4) b64 += '=';
    var bin = atob(b64);
    var out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function bytesToB64u(bytes) {
    var b = new Uint8Array(bytes);
    var bin = '';
    for (var i = 0; i < b.length; i++) bin += String.fromCharCode(b[i]);
    return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }

  async function _fetchJson(url) {
    var resp = await fetch(url);
    var body = await resp.json().catch(function () { return {}; });
    if (!resp.ok) throw new Error(body.error || ('request failed: ' + url));
    return body;
  }

  async function _postJson(url, body) {
    var resp = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    var data = await resp.json().catch(function () { return {}; });
    if (!resp.ok || data.ok === false) {
      throw new Error(data.error || ('request failed: ' + url + ' → ' + resp.status));
    }
    return data;
  }

  function _snoozed() {
    try {
      var until = parseInt(localStorage.getItem(SNOOZE_KEY) || '0', 10);
      return Date.now() < until;
    } catch (e) { return false; }
  }

  function _snooze() {
    try { localStorage.setItem(SNOOZE_KEY, String(Date.now() + SNOOZE_MS)); }
    catch (e) { /* ignore */ }
  }

  // ── ceremony steps ─────────────────────────────────────────────────

  // Step 1: generate + armor + store the personal root. The plaintext
  // seed exists only inside this function (I1) — zeroed in finally.
  async function _createIdentity(name, password, confirm) {
    if (!name) throw new Error('enter your name');
    if (typeof password !== 'string' || password.length < 8) {
      throw new Error('the password must be at least 8 characters');
    }
    if (password !== confirm) throw new Error('the passwords do not match');
    var I = _idI();
    var pair = await I.generateEd25519();
    var armor;
    try {
      armor = await I.armorSeed(pair.seed, pair.pubHex, password);
    } finally {
      pair.seed.fill(0);
      pair.seed = null;
    }
    await _postJson('/api/identity/personal', {
      display_name: name,
      armored_private_key: armor,
      root_pub: pair.pubHex,
    });
  }

  // Step 2: the WebAuthn dance. Options come base64url-encoded; the
  // browser API wants ArrayBuffers; the verify route wants base64url
  // back. The server pinned challenge/RP ID/origin at options time.
  async function _enrollPasskey() {
    if (!window.PublicKeyCredential || !navigator.credentials) {
      throw new Error('this browser does not support passkeys — ' +
        'you can enroll another device later from Account settings');
    }
    var minted = await _postJson('/api/identity/passkey/register-options', {});
    var pk = minted.options;
    pk.challenge = b64uToBytes(pk.challenge);
    pk.user.id = b64uToBytes(pk.user.id);
    (pk.excludeCredentials || []).forEach(function (c) { c.id = b64uToBytes(c.id); });
    var cred;
    try {
      cred = await navigator.credentials.create({ publicKey: pk });
    } catch (e) {
      if (e && e.name === 'InvalidStateError') {
        throw new Error('this device is already enrolled');
      }
      if (e && e.name === 'NotAllowedError') {
        throw new Error('enrollment was cancelled or timed out — try again');
      }
      throw e;
    }
    if (!cred) throw new Error('enrollment was cancelled — try again');
    await _postJson('/api/identity/passkey/register', {
      label: 'This device',
      credential: {
        id: cred.id,
        rawId: bytesToB64u(cred.rawId),
        type: cred.type,
        authenticatorAttachment: cred.authenticatorAttachment || undefined,
        clientExtensionResults:
          (cred.getClientExtensionResults && cred.getClientExtensionResults()) || {},
        response: {
          clientDataJSON: bytesToB64u(cred.response.clientDataJSON),
          attestationObject: bytesToB64u(cred.response.attestationObject),
          transports:
            (cred.response.getTransports && cred.response.getTransports()) || [],
        },
      },
    });
  }

  // ── state ──────────────────────────────────────────────────────────

  // O holds NO secrets: the password lives only in its input elements
  // and inside _createIdentity's frames.
  var O = null;

  async function open(opts) {
    opts = opts || {};
    O = {
      step: opts.step || 1, busy: false,
      hasIdentity: false, userName: '', orgs: [],
    };
    try {
      var status = await _fetchJson('/api/identity/status');
      if (!O) return;
      O.hasIdentity = !!status.personal_identity;
      O.userName = (status.personal_identity || {}).display_name || '';
      if (!opts.step) {
        O.step = O.hasIdentity ? 2 : 1;
      }
    } catch (e) { /* fresh install: no status yet → step 1 */ }
    try {
      var orgs = await _fetchJson('/api/orgs');
      if (!O) return;
      O.orgs = (orgs.orgs || []).map(function (entry) {
        var id = entry.identity_resolved || {};
        return { slug: id.slug || (entry.org || {}).slug || '',
                 name: id.name || (entry.org || {}).slug || '',
                 initial: id.initial || '?', color: id.color || '#4b5563' };
      }).filter(function (o) { return o.slug; });
    } catch (e) { /* org list is decorative on step 3 */ }
    if (!O) return;
    _render();
    return O.step;
  }

  function close() {
    O = null;
    var el = document.getElementById('network-onboarding');
    if (el) el.remove();
  }

  function _notNow() {
    _snooze();
    close();
  }

  async function maybeOpen() {
    // Never auto-open inside an embedded frame (Design Studio previews,
    // plugin iframes) — only the top-level dashboard shell onboards.
    if (window.top !== window.self) return;
    if (_snoozed()) return;
    try {
      var status = await _fetchJson('/api/identity/status');
      if (status.onboarding_needed) {
        await open({ step: status.personal_identity ? 2 : 1 });
      }
    } catch (e) { /* no server-side identity support → stay quiet */ }
  }

  // ── rendering ──────────────────────────────────────────────────────
  //
  // Markup mirrors the converged mockup (design d49be06b) byte-for-byte
  // where it can: same Tailwind utilities, same copy. One responsive
  // document — md: is the desktop card, below md the full-bleed phone
  // layout with the sticky action bar.

  function _esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;',
               '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  var _INFO = '&#8505;&#65039;';

  function _headerHtml(step) {
    if (step === 1) {
      return '<h1 class="text-2xl md:text-xl font-semibold">Get started</h1>' +
        '<p class="text-gray-400 text-sm mt-2 leading-relaxed">Set up who you are. Your identity is yours &mdash; ' +
        'it lives on your devices and works across every organization you’re part of.</p>';
    }
    if (step === 2) {
      return '<h1 class="text-2xl md:text-xl font-semibold">Add this device</h1>' +
        '<p class="text-gray-400 text-sm mt-2 leading-relaxed">Face&nbsp;ID will open your dashboard on this ' +
        'device. It only unlocks access &mdash; it never signs anything for you.</p>';
    }
    return '<h1 class="text-2xl md:text-xl font-semibold">Organizations</h1>' +
      '<p class="text-gray-400 text-sm mt-2 leading-relaxed">An organization is how you group and arrange ' +
      'your work &mdash; from an organization of one (your private notes, a hobby) to an organization of ' +
      'thousands coordinating an entire company. Create one for yourself, or share it with others.</p>';
  }

  function _progressHtml(step) {
    function seg(n, label) {
      var bar = step >= n ? 'bg-indigo-500' : 'bg-gray-700';
      var text = step === n ? 'text-indigo-300 font-semibold'
        : step > n ? 'text-indigo-400' : 'text-gray-500';
      return '<div class="flex-1"><div class="h-0.5 rounded mb-1.5 ' + bar + '"></div>' +
        '<span class="' + text + '">' + label + '</span></div>';
    }
    return '<div class="flex gap-2 mt-5 mb-1 text-center text-xs">' +
      seg(1, 'You') + seg(2, 'This device') + seg(3, 'Organizations') + '</div>';
  }

  var _INPUT_CLS = 'w-full bg-gray-800 md:bg-gray-900 border border-gray-700 rounded-lg ' +
    'px-3 py-3 md:py-2.5 text-base focus:outline-none focus:ring-2 focus:ring-indigo-500';
  var _BOX_CLS = 'bg-gray-800 md:bg-gray-900 border border-gray-700 rounded-lg';

  function _step1Html(userName) {
    return '<div data-testid="onboarding-step-you">' +
      '<label class="block text-sm text-gray-400 mt-4 mb-1.5">Your name</label>' +
      '<input id="onboarding-name" data-testid="onboarding-name" value="' + _esc(userName) + '" class="' + _INPUT_CLS + '">' +
      '<label class="block text-sm text-gray-400 mt-4 mb-1.5">Choose a password</label>' +
      '<input type="password" id="onboarding-password" data-testid="onboarding-password" class="' + _INPUT_CLS + '">' +
      '<label class="block text-sm text-gray-400 mt-4 mb-1.5">Confirm password</label>' +
      '<input type="password" id="onboarding-password2" data-testid="onboarding-password2" class="' + _INPUT_CLS + '">' +
      '<p class="text-xs text-gray-500 mt-2">Choose a password to encrypt and protect your identity, and ensure only you can access and approve changes to your account.</p>' +
      '<div class="flex gap-2.5 ' + _BOX_CLS + ' p-3 mt-5 text-sm text-gray-400 leading-relaxed">' +
      '<span>' + _INFO + '</span>' +
      '<span>Anything already on this machine &mdash; beads, sessions, organizations &mdash; stays exactly as it is. ' +
      'You’re adding an identity on top of it.</span></div>' +
      '<details class="mt-4">' +
      '<summary class="text-xs text-gray-500 cursor-pointer">Technical detail</summary>' +
      '<div class="mt-2 ' + _BOX_CLS + ' p-3 text-xs text-gray-400 leading-relaxed">' +
      'A signing keypair is generated on this device and never leaves it. Your password encrypts the ' +
      'private key at rest. The passkey you enroll next (Face&nbsp;ID) opens the dashboard &mdash; it does not ' +
      'unlock this key: access and signing stay separate.</div>' +
      '</details></div>';
  }

  function _step2Html() {
    return '<div class="text-center" data-testid="onboarding-step-device">' +
      '<svg class="w-20 h-20 mx-auto mt-8" viewBox="0 0 24 24" fill="none" stroke="#818cf8" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/>' +
      '<path d="M8 10v1"/><path d="M16 10v1"/><path d="M12 10v3a1 1 0 0 1-1 1"/><path d="M9 16.5a5 5 0 0 0 6 0"/></svg>' +
      '<p class="text-sm text-gray-400 mt-2">This device &middot; Face&nbsp;ID / Touch&nbsp;ID</p>' +
      '<div class="flex gap-2.5 ' + _BOX_CLS + ' p-3 mt-6 text-sm text-gray-400 leading-relaxed text-left">' +
      '<span>' + _INFO + '</span>' +
      '<span>You can enroll more devices later from Account settings. Lose this device and your identity ' +
      'is still yours &mdash; it’s your password that matters.</span></div>' +
      '<details class="mt-4 text-left">' +
      '<summary class="text-xs text-gray-500 cursor-pointer">Technical detail</summary>' +
      '<div class="mt-2 ' + _BOX_CLS + ' p-3 text-xs text-gray-400 leading-relaxed">' +
      'This registers a WebAuthn passkey bound to this device and this dashboard. The server refuses ' +
      'every request &mdash; API and UI &mdash; until a valid passkey assertion is presented. The passkey cannot ' +
      'decrypt your signing key.</div>' +
      '</details></div>';
  }

  function _step3Html(orgs) {
    var rows = orgs.map(function (org) {
      return '<div class="flex items-center gap-3 ' + _BOX_CLS.replace('rounded-lg', 'rounded-xl') + ' p-3.5 mt-3 first:mt-4" data-testid="onboarding-org-row">' +
        '<div class="w-9 h-9 rounded-lg flex items-center justify-center font-bold text-sm flex-shrink-0" style="background:' + _esc(org.color) + '">' + _esc(org.initial) + '</div>' +
        '<div class="text-sm leading-snug flex-1">' + _esc(org.name) +
        '<span class="block text-xs text-gray-400 mt-0.5">Already on this machine &mdash; linked to your new identity automatically.</span></div>' +
        '<span class="text-[11px] text-emerald-400 bg-emerald-400/10 border border-emerald-400/30 rounded-full px-2.5 py-1 whitespace-nowrap">found here</span>' +
        '</div>';
    }).join('');
    return '<div data-testid="onboarding-step-orgs">' + rows +
      '<button id="onboarding-create-org" data-testid="onboarding-create-org" class="w-full flex items-center gap-3 ' + _BOX_CLS.replace('rounded-lg', 'rounded-xl') + ' hover:border-indigo-500 p-3.5 mt-3 text-left">' +
      '<div class="w-9 h-9 rounded-lg bg-gray-700 text-gray-400 flex items-center justify-center font-bold flex-shrink-0">+</div>' +
      '<div class="text-sm leading-snug">Create a new organization' +
      '<span class="block text-xs text-gray-400 mt-0.5">Set up a private workspace &mdash; share it with others when you’re ready.</span></div>' +
      '</button>' +
      '<button id="onboarding-skip-orgs" data-testid="onboarding-skip-orgs" class="block mx-auto mt-4 text-xs text-gray-500 hover:text-gray-300">' +
      'Skip for now &mdash; add an org anytime</button></div>';
  }

  function _actionsHtml(step, hasIdentity) {
    var back;
    if (step === 1 || (step === 2 && hasIdentity)) {
      // Resuming with an identity already created: step 1 is behind us
      // for good, so the secondary action is the mockup's "Not now"
      // escape, not a Back into a completed step.
      back = '<button id="onboarding-notnow" data-testid="onboarding-notnow" class="border border-gray-600 text-gray-300 rounded-lg px-4 py-3 md:py-2 text-sm font-semibold">Not now</button>';
    } else {
      back = '<button id="onboarding-back" data-testid="onboarding-back" class="border border-gray-600 text-gray-300 rounded-lg px-4 py-3 md:py-2 text-sm font-semibold">Back</button>';
    }
    var primaryLabel = step === 1 ? 'Continue'
      : step === 2 ? 'Enroll Face&nbsp;ID' : 'Finish';
    return '<div class="mt-auto md:mt-7 pt-6 md:pt-0">' +
      '<div class="flex items-center gap-2.5">' +
      '<span class="text-xs text-gray-500 mr-auto hidden md:inline">Step ' + step + ' of 3</span>' +
      back +
      '<button id="onboarding-primary" data-testid="onboarding-primary" class="flex-1 md:flex-none bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg px-5 py-3 md:py-2 text-sm font-semibold">' + primaryLabel + '</button>' +
      '</div>' +
      '<div id="onboarding-error" data-testid="onboarding-error" class="hidden text-sm text-red-400 mt-3"></div>' +
      '<div id="onboarding-busy" class="hidden text-xs text-gray-500 mt-3">working&hellip;</div>' +
      '<p class="text-xs text-gray-500 text-center mt-3 md:hidden">Step ' + step + ' of 3</p>' +
      '</div>';
  }

  function _render() {
    var existing = document.getElementById('network-onboarding');
    if (existing) existing.remove();
    if (!O) return;

    var overlay = document.createElement('div');
    overlay.id = 'network-onboarding';
    overlay.setAttribute('data-testid', 'network-onboarding');
    overlay.setAttribute('data-step', String(O.step));
    overlay.className = 'fixed inset-0 z-[70] bg-gray-900 text-gray-100 overflow-y-auto';
    overlay.innerHTML =
      '<div class="min-h-full flex flex-col items-center md:justify-center px-5 py-6 md:p-8">' +
      '<div class="text-lg md:text-xl font-bold text-indigo-400 self-start md:self-center md:mb-6">Autonomy</div>' +
      '<div class="w-full md:max-w-lg flex flex-col flex-1 md:flex-none ' +
      'md:bg-gray-800 md:border md:border-gray-700 md:rounded-2xl md:shadow-2xl md:p-8 pt-4 md:pt-8">' +
      _headerHtml(O.step) +
      _progressHtml(O.step) +
      (O.step === 1 ? _step1Html(O.userName)
        : O.step === 2 ? _step2Html() : _step3Html(O.orgs)) +
      _actionsHtml(O.step, O.hasIdentity) +
      '</div></div>';
    document.body.appendChild(overlay);
    _wire(overlay);
  }

  function _fail(e) {
    if (!O) return;
    O.busy = false;
    var err = document.getElementById('onboarding-error');
    var busy = document.getElementById('onboarding-busy');
    if (busy) busy.classList.add('hidden');
    if (err) {
      err.textContent = (e && e.message) || String(e);
      err.classList.remove('hidden');
    }
  }

  function _busySet(v) {
    if (!O) return;
    O.busy = v;
    var busy = document.getElementById('onboarding-busy');
    var err = document.getElementById('onboarding-error');
    if (busy) busy.classList.toggle('hidden', !v);
    if (v && err) err.classList.add('hidden');
  }

  function _goto(step) {
    if (!O) return;
    O.busy = false;
    O.step = step;
    _render();
  }

  function _wire(overlay) {
    function on(id, fn) {
      var el = overlay.querySelector('#' + id);
      if (el) el.addEventListener('click', function () { if (O && !O.busy) fn(); });
    }
    on('onboarding-notnow', _notNow);
    on('onboarding-back', function () { _goto(O.step - 1); });
    on('onboarding-skip-orgs', close);
    on('onboarding-create-org', function () {
      close();
      if (window.AutonomyNetworkIdentity) window.AutonomyNetworkIdentity.open();
    });
    on('onboarding-primary', function () {
      if (O.step === 1) {
        var name = (overlay.querySelector('#onboarding-name').value || '').trim();
        var p1 = overlay.querySelector('#onboarding-password').value;
        var p2 = overlay.querySelector('#onboarding-password2').value;
        _busySet(true);
        _createIdentity(name, p1, p2)
          .then(function () {
            if (!O) return;
            O.hasIdentity = true;
            O.userName = name;
            _goto(2);
          })
          .catch(_fail);
      } else if (O.step === 2) {
        _busySet(true);
        _enrollPasskey()
          .then(function () { _goto(3); })
          .catch(_fail);
      } else {
        close();
        try {
          var s = window.AutonomyNetworkSession;
          if (s && s._internals) s._internals.renderIndicator();
        } catch (e) { /* chrome refresh only */ }
      }
    });
  }

  // ── init + exports ─────────────────────────────────────────────────

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { maybeOpen(); });
  } else {
    maybeOpen();
  }

  window.AutonomyOnboarding = {
    open: open,
    close: close,
    maybeOpen: maybeOpen,
    // Internals exposed for the L2.B sweep; enforcement lives in the
    // real ceremony paths above.
    _internals: {
      createIdentity: _createIdentity,
      enrollPasskey: _enrollPasskey,
      b64uToBytes: b64uToBytes,
      bytesToB64u: bytesToB64u,
      state: function () { return O; },
      snoozed: _snoozed,
    },
  };
})();
