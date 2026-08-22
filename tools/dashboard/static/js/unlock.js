/* Unlock screen — the human gate's front door (mockup d49be06b, 'Unlock').
 *
 * Two ways in, per the access model (graph://80ef5131-9f0):
 *
 *   Passkey ("Unlock with Face ID") — navigator.credentials.get()
 *     against options the server minted; the server pinned challenge/
 *     RP ID/origin and verifies against the stored credential. Quick,
 *     device-bound, ACCESS only.
 *
 *   Password (the always-available floor) — fetch the armored personal
 *     root, decrypt it LOCALLY with the password (I1: the password and
 *     the plaintext seed never leave this page), sign the server's
 *     single-use challenge with the root key, send only the signature.
 *     Works with zero passkeys enrolled — removing your last passkey
 *     can never lock you out.
 *
 * Crypto internals are shared, not re-implemented: armor decrypt +
 * canonical JSON from network-signon.js, Ed25519 PKCS#8 import from
 * network-identity.js.
 */
(function () {
  'use strict';

  var UNLOCK_DOMAIN = 'autonomy.identity.unlock.v1\n';

  var U = {
    mode: 'loading',      // loading | passkey | password | locked-out | error
    name: '',
    initial: '',
    passkeysForHost: 0,
    hasIdentity: false,
    webauthnOk: false,
    troubleOpen: false,
    techOpen: false,
    busy: false,
    error: null,
    // Fleet completion needs the personal root, not merely an access
    // assertion. Keep the visible action factor-neutral while forcing the
    // password-backed, root-releasing ceremony until a passkey ceremony can
    // release equivalent root material.
    fleetRootRequired: new URLSearchParams(location.search).get('fleet') === '1',
    // Monotonic ceremony id: bumped on every _run and every mode switch,
    // so an abandoned ceremony's late resolve/reject is discarded instead
    // of stamping a stale error or navigating away from the new screen.
    op: 0,
  };

  // ── helpers ────────────────────────────────────────────────────────

  function _signonI() {
    var m = window.AutonomyNetworkSession;
    if (!m || !m._internals) throw new Error('network-signon.js did not load');
    return m._internals;
  }

  function _identityI() {
    var m = window.AutonomyNetworkIdentity;
    if (!m || !m._internals) throw new Error('network-identity.js did not load');
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
      var err = new Error(data.error || ('request failed: ' + url + ' → ' + resp.status));
      err.fallback = data.fallback;
      throw err;
    }
    return data;
  }

  // Same-site path only — mirrors the server-side sanitize_next guard.
  function _nextPath() {
    var raw = new URLSearchParams(location.search).get('next') || '/';
    if (raw.charAt(0) !== '/' || raw.slice(0, 2) === '//' ||
        raw.indexOf('\\') !== -1 || raw.split('?')[0].indexOf(':') !== -1) {
      return '/';
    }
    return raw;
  }

  function _done() {
    location.replace(_nextPath());
  }

  function _esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;',
               '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // ── ceremonies ─────────────────────────────────────────────────────

  async function _unlockWithPasskey() {
    if (!window.PublicKeyCredential || !navigator.credentials) {
      throw new Error('this browser does not support passkeys — use your password instead');
    }
    var minted = await _postJson('/api/identity/unlock/passkey/options', {});
    var pk = minted.options;
    pk.challenge = b64uToBytes(pk.challenge);
    (pk.allowCredentials || []).forEach(function (c) { c.id = b64uToBytes(c.id); });
    var cred;
    try {
      cred = await navigator.credentials.get({ publicKey: pk });
    } catch (e) {
      if (e && e.name === 'NotAllowedError') {
        throw new Error('unlock was cancelled or timed out — try again');
      }
      throw e;
    }
    if (!cred) throw new Error('unlock was cancelled — try again');
    await _postJson('/api/identity/unlock/passkey', {
      credential: {
        id: cred.id,
        rawId: bytesToB64u(cred.rawId),
        type: cred.type,
        authenticatorAttachment: cred.authenticatorAttachment || undefined,
        clientExtensionResults:
          (cred.getClientExtensionResults && cred.getClientExtensionResults()) || {},
        response: {
          clientDataJSON: bytesToB64u(cred.response.clientDataJSON),
          authenticatorData: bytesToB64u(cred.response.authenticatorData),
          signature: bytesToB64u(cred.response.signature),
          userHandle: cred.response.userHandle
            ? bytesToB64u(cred.response.userHandle) : undefined,
        },
      },
    });
  }

  // The plaintext seed exists only inside this function — zeroed the
  // moment the signing key is imported (I1).
  async function _unlockWithPassword(password) {
    if (!password) throw new Error('enter your password');
    var S = _signonI();
    var I = _identityI();
    var stored = await _fetchJson('/api/identity/personal');
    var opened;
    try {
      opened = await S.decryptArmor(stored.armored_private_key, password);
    } catch (e) {
      throw new Error('that password does not open your identity — check it and try again');
    }
    // The vault wake (below, after the session exists) needs the raw seed to
    // derive its KEM credential + delegate. Keep ONE copy past the I1 zero and
    // wipe it the instant the wake is done, so the seed never outlives it.
    var wakeSeed = new Uint8Array(opened.seed);
    var key;
    try {
      key = await I.importSigningKey(opened.seed);
    } finally {
      opened.seed.fill(0);
      opened.seed = null;
    }
    var minted = await _postJson('/api/identity/unlock/password/options', {});
    var message = new TextEncoder().encode(
      UNLOCK_DOMAIN + S.canonicalJson({
        v: 1, challenge: minted.challenge, origin: minted.origin,
      }));
    var sig = S.bytesToHex(await crypto.subtle.sign('Ed25519', key, message));
    await _postJson('/api/identity/unlock/password', {
      challenge: minted.challenge, signature: sig,
    });

    // Wake the vault now that the session exists: publish the KEM credential
    // and hand its private half so this sign-in warms the vault durably — the
    // same ceremony warm_client runs. Best-effort: a wake that fails must never
    // turn a successful unlock into a lockout.
    try {
      var wake = await S.wakeVault({ personalRootSeed: wakeSeed });
      if (window.console && console.info) {
        console.info('vault wake:', JSON.stringify(wake));
      }
    } catch (e) {
      if (window.console && console.warn) {
        console.warn('vault wake failed after unlock:', (e && e.message) || e);
      }
    }

    // A Fleet-linked install may have received its root-signed roster entry
    // while this Dashboard was waiting. Complete it in this same password
    // ceremony: the browser verifies the delivery, derives the machine key,
    // and sends only a machine-key possession proof. The ceremony zeroes the
    // root seed on every exit; no server route receives it.
    try {
      var completion = await _fetchJson(
        '/api/fleet/enrollment/local-completion');
      if (completion.pending) {
        var fleetCeremony = await import('./ceremony/fleet-enrollment.js');
        var proof = await fleetCeremony.completeFleetEnrollment({
          personalRootSeed: wakeSeed,
          requestId: completion.request_id,
          request: completion.request,
          channelBinding: completion.channel_binding,
          approval: completion.approval,
          rosterEntry: completion.roster_entry,
        });
        wakeSeed = null;  // the ceremony zeroed the shared Uint8Array
        await _postJson('/api/fleet/enrollment/local-completion', proof);
      } else {
        // Every process restart loses the ephemeral Fleet sync key by design.
        // Re-mint it during an ordinary later root unlock from the durable
        // machine id and public roster entry; no root or machine seed crosses
        // this browser boundary.
        var runtimeContext = await _fetchJson('/api/fleet/runtime');
        if (runtimeContext.enabled) {
          var fleetRuntimeCeremony = await import('./ceremony/fleet-enrollment.js');
          var runtimeCredential =
            await fleetRuntimeCeremony.mintFleetRuntimeCredential({
              personalRootSeed: wakeSeed,
              rootPub: runtimeContext.personal_root_pub,
              machineId: runtimeContext.machine_id,
              machinePub: runtimeContext.machine_pub,
            });
          wakeSeed = null;  // the ceremony zeroed the shared Uint8Array
          await _postJson('/api/fleet/runtime', runtimeCredential);
        }
      }
    } catch (e) {
      if (window.console && console.warn) {
        console.warn('fleet enrollment completion failed after unlock:',
                     (e && e.message) || e);
      }
      // A Fleet-linked unlock is the enrollment boundary, not a best-effort
      // side effect.  Do not navigate to the synchronization screen unless
      // this machine has verified and acknowledged its delivered evidence.
      if (U.fleetRootRequired) throw e;
    } finally {
      if (wakeSeed) wakeSeed.fill(0);
      wakeSeed = null;
    }

    // Access authentication has succeeded.  Reuse this password-backed root
    // ceremony to maintain the unattended serving credential if necessary.
    // The normal case is a cheap local status read; repair failures never
    // turn successful dashboard access into a lockout.  The access-only
    // passkey path does not run this because it releases no signing material.
    var networkSession = window.AutonomyNetworkSession;
    if (networkSession &&
        typeof networkSession.repairServeCredential === 'function') {
      try {
        if (typeof networkSession.ready === 'function') {
          await networkSession.ready();
        }
        // EVERY organization, not just the default one: the personal seed
        // opens all of their sealed roots, and repairing only the default is
        // how the others drift to expiry unnoticed.
        if (typeof networkSession.repairAllServeCredentials === 'function') {
          var serve = await networkSession.repairAllServeCredentials(
            password, {});
          window.__autonomyServeRepair = serve;
          if (window.console && console.info &&
              (serve.repaired.length || serve.failed.length)) {
            console.info('serving credentials:', JSON.stringify(serve));
          }
        } else {
          await networkSession.repairServeCredential(password, {});
        }
        // Report what happened to the SERVER. This ran only in a console
        // before, which an operator on a phone cannot open -- and a repair
        // whose failures nobody can read is how three organizations drifted
        // to the edge of expiry unnoticed. Diagnostic only: statuses and
        // error strings, never key material.
        try {
          await fetch('/api/network/unlock-report', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              serve: window.__autonomyServeRepair || null,
            }),
          });
        } catch (e) { /* diagnostics must never break an unlock */ }
      } catch (e) {
        if (window.console && console.warn) {
          console.warn('serving credential maintenance failed after unlock:',
                       (e && e.message) || e);
        }
      }
    }
  }

  // ── rendering (markup mirrors the mockup's Unlock state) ───────────

  var _BOX_CLS = 'bg-gray-800 md:bg-gray-900 border border-gray-700 rounded-xl';
  var _ROW_CLS = 'w-full flex items-center gap-3 min-h-[46px] px-2 rounded-lg ' +
    'hover:bg-gray-700 active:bg-gray-700 text-sm text-gray-200 text-left';

  function _avatarHtml() {
    return '<div class="w-16 h-16 md:w-12 md:h-12 rounded-full bg-teal-600 flex items-center ' +
      'justify-center font-bold text-2xl md:text-lg mt-9 md:mt-7" data-testid="unlock-initial">' +
      _esc(U.initial || '?') + '</div>' +
      '<div class="text-lg md:text-base font-semibold mt-3" data-testid="unlock-name">' +
      _esc(U.name || '') + '</div>';
  }

  function _troubleHtml() {
    var rows = '';
    if (U.hasIdentity && U.mode === 'passkey' && !U.fleetRootRequired) {
      rows +=
        '<button id="unlock-use-password" data-testid="unlock-use-password" class="' + _ROW_CLS + '">' +
        '<svg class="w-4.5 h-4.5 text-gray-400 flex-shrink-0" fill="none" stroke="currentColor" stroke-width="1.7" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="10" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>' +
        'Use your password instead</button>';
    }
    if (U.passkeysForHost > 0 && !U.fleetRootRequired) {
      rows +=
        '<button id="unlock-other-device" class="' + _ROW_CLS + '">' +
        '<svg class="w-4.5 h-4.5 text-gray-400 flex-shrink-0" fill="none" stroke="currentColor" stroke-width="1.7" viewBox="0 0 24 24"><rect x="5" y="2" width="14" height="20" rx="2"/><path stroke-linecap="round" d="M12 18h.01"/></svg>' +
        'Use another device you’ve enrolled</button>';
    }
    rows +=
      '<button class="' + _ROW_CLS + '" disabled>' +
      '<svg class="w-4.5 h-4.5 text-gray-400 flex-shrink-0" fill="none" stroke="currentColor" stroke-width="1.7" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M21 2l-2 2m-7.6 7.6a5.5 5.5 0 1 1-7.78 7.78 5.5 5.5 0 0 1 7.78-7.78zm0 0L15.5 7.5m0 0 3 3L22 7l-3-3"/></svg>' +
      'Recover access <span class="ml-auto text-[10px] text-amber-400 bg-amber-400/10 border border-amber-400/30 rounded-full px-2 py-0.5 whitespace-nowrap">not yet designed</span></button>';
    return '<div id="unlock-trouble" data-testid="unlock-trouble" class="mt-4 ' + _BOX_CLS + ' p-4 text-left">' +
      '<div class="text-sm font-semibold mb-2">' +
      (U.mode === 'passkey' ? 'Face&nbsp;ID not working?' : 'Trouble signing in?') +
      '</div>' + rows + '</div>';
  }

  function _techHtml() {
    var text = U.mode === 'password'
      ? 'Your password decrypts your identity key right here in the browser — it is never typed ' +
        'into anything that leaves this device. The dashboard only sees a signature proving the ' +
        'key opened. Unlocking opens the dashboard; approving actions asks for your password again.'
      : 'Face&nbsp;ID unlocks a passkey kept on this device — it proves it’s you to your dashboard, ' +
        'and nothing is typed or sent anywhere. This only opens the dashboard; approving actions ' +
        'still asks for your password.';
    return '<div id="unlock-tech" data-testid="unlock-tech" class="mt-4 ' + _BOX_CLS + ' p-4 text-left text-sm text-gray-400 leading-relaxed">' +
      text + '</div>';
  }

  function _passwordFormHtml(standalone) {
    return '<div class="mt-6 text-left" data-testid="unlock-password-form">' +
      '<label class="block text-sm text-gray-400 mb-1.5" for="unlock-password">Password</label>' +
      '<input type="password" id="unlock-password" data-testid="unlock-password" autocomplete="current-password" ' +
      'class="w-full bg-gray-800 md:bg-gray-900 border border-gray-700 rounded-lg px-3 py-3 md:py-2.5 text-base ' +
      'focus:outline-none focus:ring-2 focus:ring-indigo-500">' +
      (standalone || !U.webauthnOk ? '' :
        '<button id="unlock-back-passkey" class="block mt-3 text-sm text-indigo-400 hover:underline">Back to Face&nbsp;ID</button>') +
      '</div>';
  }

  function _render() {
    var card = document.getElementById('unlock-card');
    if (!card) return;

    if (U.mode === 'loading') return;

    if (U.mode === 'error') {
      card.innerHTML =
        '<div class="flex flex-col items-center flex-1 md:flex-none justify-center md:justify-start">' +
        '<h1 class="text-2xl md:text-xl font-semibold">Unlock dashboard</h1>' +
        '<p class="text-gray-400 mt-2" data-testid="unlock-loaderror">Couldn’t reach your dashboard to ' +
        'see how to unlock it. Check your connection and try again.</p>' +
        '<button id="unlock-retry" data-testid="unlock-retry" class="mt-6 bg-indigo-600 hover:bg-indigo-500 ' +
        'text-white font-semibold rounded-xl py-3 px-6">Try again</button></div>';
      var retry = card.querySelector('#unlock-retry');
      if (retry) retry.addEventListener('click', function () {
        U.mode = 'loading'; _render(); _init();
      });
      return;
    }

    if (U.mode === 'locked-out') {
      // Two shapes: a browser that CAN'T use passkeys but has one enrolled
      // here (send them to a capable browser), vs nothing this browser can
      // unlock at all (send them to their set-up device).
      var msg = (U.passkeysForHost > 0 && !U.webauthnOk)
        ? 'A passkey is enrolled for this address, but this browser can’t use ' +
          'passkeys (they need a secure context — https or localhost — and a ' +
          'supported browser). Open your dashboard in a browser that supports ' +
          'Face ID / Touch ID on this device.'
        : 'This dashboard is locked, and this browser has no way to unlock it — ' +
          'no passkey is enrolled for this address and no password-protected ' +
          'identity is stored. Open the dashboard from the device you set it up on.';
      card.innerHTML =
        '<div class="flex flex-col items-center flex-1 md:flex-none justify-center md:justify-start">' +
        '<h1 class="text-2xl md:text-xl font-semibold">Unlock dashboard</h1>' +
        '<p class="text-gray-400 mt-2" data-testid="unlock-lockedout">' + _esc(msg) + '</p></div>';
      return;
    }

    // Preserve an in-progress password across the innerHTML re-render that
    // toggling Trouble/Technical (or any _render) performs.
    var priorPw = '';
    var priorInput = card.querySelector('#unlock-password');
    if (priorInput) priorPw = priorInput.value;

    var passkey = U.mode === 'passkey';
    var sub = passkey ? 'Use Face ID to open your dashboard.'
      : 'Enter your password to open your dashboard.';
    var primary = passkey ? 'Unlock with Face ID' : 'Unlock';

    card.innerHTML =
      '<div class="flex flex-col items-center flex-1 md:flex-none justify-center md:justify-start">' +
      '<h1 class="text-2xl md:text-xl font-semibold">Unlock dashboard</h1>' +
      '<p class="text-gray-400 mt-2">' + sub + '</p>' +
      _avatarHtml() +
      (passkey ? '' : _passwordFormHtml(
        U.passkeysForHost === 0 || U.fleetRootRequired)) +
      '</div>' +
      '<div class="md:mt-7 pt-7 md:pt-0">' +
      '<button id="unlock-primary" data-testid="unlock-primary" class="w-full bg-indigo-600 hover:bg-indigo-500 ' +
      'text-white font-semibold rounded-xl py-4 md:py-3 text-lg md:text-base">' + primary + '</button>' +
      '<div class="flex justify-center gap-6 mt-4 text-sm">' +
      '<button id="unlock-trouble-toggle" data-testid="unlock-trouble-toggle" class="text-indigo-400 hover:underline">Trouble signing in?</button>' +
      '<button id="unlock-tech-toggle" data-testid="unlock-tech-toggle" class="text-indigo-400 hover:underline">Technical detail</button>' +
      '</div>' +
      (U.troubleOpen ? _troubleHtml() : '') +
      (U.techOpen ? _techHtml() : '') +
      '<div id="unlock-error" data-testid="unlock-error" class="' +
      (U.error ? '' : 'hidden ') + 'text-sm text-red-400 mt-4">' +
      _esc(U.error || '') + '</div>' +
      '<div id="unlock-busy" class="' + (U.busy ? '' : 'hidden ') +
      'text-xs text-gray-500 mt-3">working&hellip;</div>' +
      '</div>';
    _wire(card);
    if (!passkey) {
      var input = card.querySelector('#unlock-password');
      if (input) {
        if (priorPw) input.value = priorPw;
        input.focus();
      }
    }
  }

  function _fail(e) {
    U.busy = false;
    U.error = (e && e.message) || String(e);
    _render();
  }

  function _run(ceremony) {
    if (U.busy) return;
    var myOp = ++U.op;
    U.busy = true;
    U.error = null;
    _render();
    ceremony().then(function () {
      if (myOp !== U.op) return;   // superseded by a mode switch — discard
      _done();
    }).catch(function (e) {
      if (myOp !== U.op) return;   // stale ceremony's error — don't stamp it
      if (e && e.fallback === 'password' && U.hasIdentity) {
        U.mode = 'password';
        U.busy = false;
        _render();
        return;
      }
      _fail(e);
    });
  }

  // Switch screens, abandoning any in-flight ceremony: bump op so its late
  // outcome is ignored, and clear busy so the new screen accepts input.
  function _switchMode(mode) {
    U.op++;
    U.mode = mode;
    U.busy = false;
    U.error = null;
    U.troubleOpen = false;
    _render();
  }

  function _wire(card) {
    function on(id, fn) {
      var el = card.querySelector('#' + id);
      if (el) el.addEventListener('click', fn);
    }
    on('unlock-primary', function () {
      if (U.mode === 'passkey') {
        _run(_unlockWithPasskey);
      } else {
        var input = card.querySelector('#unlock-password');
        var pw = input ? input.value : '';
        _run(function () { return _unlockWithPassword(pw); });
      }
    });
    var pwInput = card.querySelector('#unlock-password');
    if (pwInput) {
      pwInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
          _run(function () { return _unlockWithPassword(pwInput.value); });
        }
      });
    }
    on('unlock-trouble-toggle', function () {
      U.troubleOpen = !U.troubleOpen;
      U.techOpen = false;
      _render();
    });
    on('unlock-tech-toggle', function () {
      U.techOpen = !U.techOpen;
      U.troubleOpen = false;
      _render();
    });
    on('unlock-use-password', function () {
      _switchMode('password');
    });
    on('unlock-other-device', function () {
      // The browser's own cross-device (hybrid/QR) flow rides the same
      // assert ceremony — re-trigger it.
      U.troubleOpen = false;
      _render();
      _run(_unlockWithPasskey);
    });
    on('unlock-back-passkey', function () {
      // Only offered when this browser can actually assert; guard anyway.
      _switchMode(U.webauthnOk ? 'passkey' : 'password');
    });
  }

  // ── init ───────────────────────────────────────────────────────────

  async function _init() {
    U.webauthnOk = !!(window.PublicKeyCredential && navigator.credentials);
    var status;
    try {
      status = await _fetchJson('/api/identity/status');
    } catch (e) {
      // /unlock is only served when auth IS enrolled, so a failed status
      // fetch means the network/server hiccuped — NOT that there's nothing
      // to unlock. Offer a retry instead of the false "give up" screen.
      U.mode = 'error';
      _render();
      return;
    }
    U.name = (status.personal_identity || {}).display_name || '';
    U.initial = (U.name || '?').trim().charAt(0).toUpperCase();
    U.hasIdentity = !!status.personal_identity;
    U.passkeysForHost = status.passkeys_for_host || 0;
    if (!U.fleetRootRequired && U.passkeysForHost > 0 && U.webauthnOk) {
      U.mode = 'passkey';
    } else if (U.hasIdentity) {
      U.mode = 'password';        // the always-available floor
    } else {
      // No password floor, and either no passkey here or a browser that
      // can't assert one — the locked-out screen distinguishes the two.
      U.mode = 'locked-out';
    }
    _render();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _init);
  } else {
    _init();
  }

  window.AutonomyUnlock = {
    _internals: {
      state: function () { return U; },
      unlockWithPasskey: _unlockWithPasskey,
      unlockWithPassword: _unlockWithPassword,
      nextPath: _nextPath,
      domain: UNLOCK_DOMAIN,
    },
  };
})();
