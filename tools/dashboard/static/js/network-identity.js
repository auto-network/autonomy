/* auto.network create-org-identity ceremony — the C1 surface (spec §6.2–6.3,
 * bead auto-40fob). Browser-only crypto, mirroring the commit-signing model
 * and the C2 sign-on module this file leans on.
 *
 * The ceremony: (1) generate the org root Ed25519 keypair here, in the
 * page; (2) passphrase-encrypt the 32-byte seed into the CANONICAL armor
 * (tools/network/idkit/armor.py — same PBKDF2/AES-GCM parameters, same
 * AAD, same canonical-JSON body) and store ONLY that armor via
 * POST /api/network/org-key; (3) optionally mint a COLD recovery keypair,
 * rendered as a printable/copyable/downloadable block that is NEVER sent
 * anywhere; (4) sign the ROOT-DIRECT registration envelope (registry
 * §4.1) and submit it through POST /api/network/register — the dashboard
 * forwards to its server-configured registry, the browser never picks the
 * destination; (5) the server persists autonomy.network.binding and the
 * success screen shows the binding expiry.
 *
 * Invariants enforced client-side:
 *   I1 — the root seed exists only inside the ceremony functions: it is
 *        zeroed immediately after the armor is built and the transient
 *        non-extractable signing key is imported. Only the armor is ever
 *        POSTed; nothing key-shaped touches localStorage/IndexedDB.
 *   I3 — the recovery policy is declared AT registration; the recovery
 *        private seed lives only in the rendered block (cold storage is
 *        the operator's job — that is the point).
 *   I6 — the registration is self-signed by the root being bound, so the
 *        binding is attributable from its first byte.
 *
 * Depends on network-signon.js (loaded first in base.html) for the
 * byte-exact canonical JSON, hex helpers, domain constants, and — in the
 * resume path — the armor decrypt that C2's sign-on uses. One canonical
 * implementation, shared, so C1's armor is C2's armor by construction.
 */
(function () {
  'use strict';

  var ARMOR_BEGIN = '-----BEGIN AUTONOMY NETWORK ROOT KEY-----';
  var ARMOR_END = '-----END AUTONOMY NETWORK ROOT KEY-----';
  var REGISTRATION_PATH = '/v1/orgs';

  // PBKDF2 work factor — mirrors armor.DEFAULT_ITERATIONS. Tests may
  // lower it through _internals.overrideIterations (floor 10k, the
  // minimum parse_armor accepts).
  var DEFAULT_ITERATIONS = 600000;
  var _iterations = DEFAULT_ITERATIONS;

  var PKCS8_ED25519_PREFIX = [
    0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06,
    0x03, 0x2b, 0x65, 0x70, 0x04, 0x22, 0x04, 0x20,
  ];

  var _te = new TextEncoder();

  function _I() {
    var s = window.AutonomyNetworkSession;
    if (!s || !s._internals) {
      throw new Error('network-signon.js must load before network-identity.js');
    }
    return s._internals;
  }

  function bytesToB64(bytes) {
    var bin = '';
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin);
  }

  function _domainBytes(domain, canonicalStr) {
    var d = _te.encode(domain);
    var c = _te.encode(canonicalStr);
    var out = new Uint8Array(d.length + c.length);
    out.set(d, 0);
    out.set(c, d.length);
    return out;
  }

  function _nowS() { return Math.floor(Date.now() / 1000); }

  // ── ceremony crypto ────────────────────────────────────────────────

  // Transiently-extractable generation: WebCrypto only hands back the
  // seed via a pkcs8 export, so the keypair is minted extractable, the
  // seed is pulled out once, and every byte buffer is zeroed. The seed's
  // onward life is: armor (encrypted) + non-extractable signing import.
  async function generateEd25519() {
    var kp = await crypto.subtle.generateKey({ name: 'Ed25519' }, true, ['sign', 'verify']);
    var pub = new Uint8Array(await crypto.subtle.exportKey('raw', kp.publicKey));
    var pkcs8 = new Uint8Array(await crypto.subtle.exportKey('pkcs8', kp.privateKey));
    if (pkcs8.length < PKCS8_ED25519_PREFIX.length + 32) {
      throw new Error('unexpected pkcs8 shape from WebCrypto');
    }
    var seed = pkcs8.slice(pkcs8.length - 32);
    pkcs8.fill(0);
    return { seed: seed, pubHex: _I().bytesToHex(pub) };
  }

  // The PERSONAL identity's armor, in the multi-lock format (v2). It
  // delegates to the single implementation in ceremony/primitives.js rather
  // than hand-rolling a second one here: the v1 minter below exists because
  // this file predates that module, and duplicating the newer format would
  // be how the two quietly drift apart.
  //
  async function armorSeed(seed, rootPubHex, passphrase, iterations) {
    if (typeof passphrase !== 'string' || !passphrase) {
      throw new Error('passphrase must be a non-empty string');
    }
    if (!/^[0-9a-f]{64}$/.test(rootPubHex)) {
      throw new Error('root_pub must be 64 lowercase hex chars');
    }
    var policy = await import('/static/js/ceremony/root-factor-policy.js');
    return policy.mintPasswordArmor({
      rootSeed: seed, rootPub: rootPubHex, password: passphrase,
      iterations: iterations || _iterations,
    });
  }

  async function importSigningKey(seed) {
    var pkcs8 = new Uint8Array(PKCS8_ED25519_PREFIX.length + seed.length);
    pkcs8.set(PKCS8_ED25519_PREFIX, 0);
    pkcs8.set(seed, PKCS8_ED25519_PREFIX.length);
    try {
      return await crypto.subtle.importKey(
        'pkcs8', pkcs8, { name: 'Ed25519' }, false, ['sign']);
    } finally {
      pkcs8.fill(0);
    }
  }

  // Root-direct registration envelope (registry §4.1): no cert — the
  // binding does not exist yet — and signer === payload.root_pub. The
  // signature binds method+path exactly like registry/signing.py.
  async function signRegistration(rootKey, rootPubHex, payload) {
    var ts = _nowS();
    var signingInput = _domainBytes(_I().domains.request, _I().canonicalJson({
      v: 1,
      method: 'POST',
      path: REGISTRATION_PATH,
      ts: ts,
      signer: rootPubHex,
      payload: payload,
    }));
    var sig = _I().bytesToHex(await crypto.subtle.sign('Ed25519', rootKey, signingInput));
    return { v: 1, signer: rootPubHex, ts: ts, payload: payload, sig: sig };
  }

  // The cold recovery block — rendered once, stored NOWHERE. The private
  // seed's only copy is this text in the operator's hands.
  function buildRecoveryBlock(orgUuid, rootPubHex, recPubHex, recSeedHex) {
    return [
      '========== AUTONOMY NETWORK RECOVERY KEY — KEEP OFFLINE ==========',
      '',
      'Org UUID:          ' + orgUuid,
      'Org root pub:      ' + rootPubHex,
      'Recovery pub:      ' + recPubHex,
      'Recovery seed:     ' + recSeedHex,
      'Created:           ' + new Date().toISOString(),
      '',
      'This is the ONLY copy. It is not stored on any server or in this',
      'browser. Anyone holding the recovery seed can rebind the org',
      'identity to a new root key (the pre-declared recovery policy).',
      'Print it or write it down, keep it cold, never paste it anywhere',
      'except a rebind ceremony.',
      '==================================================================',
    ].join('\n');
  }

  // ── server calls ───────────────────────────────────────────────────

  async function _fetchJsonOrNull(url, org) {
    // auto.network routes scope by X-Graph-Org; plain fetch carries none, so
    // a bare ?org= is refused cross-org. Pass the org header explicitly.
    var resp = await fetch(url, org ? { headers: { 'X-Graph-Org': org } } : undefined);
    if (resp.status === 404) return null;
    var body = await resp.json().catch(function () { return {}; });
    if (!resp.ok) throw new Error(body.error || ('request failed: ' + url));
    return body;
  }

  async function _postJson(url, body, org) {
    var _h = { 'Content-Type': 'application/json' };
    if (org) _h['X-Graph-Org'] = org;
    var resp = await fetch(url, {
      method: 'POST', headers: _h,
      body: JSON.stringify(body),
    });
    var data = await resp.json().catch(function () { return {}; });
    if (!resp.ok || data.ok === false) {
      throw new Error(data.error || ('request failed: ' + url + ' → ' + resp.status));
    }
    return data;
  }

  // ── wizard state machine ───────────────────────────────────────────
  //
  // Steps: status | intro | passphrase | recovery | register | success.
  // The ceremony state W holds at most: the transient non-extractable
  // root signing key, the public halves, the armor text, and (until the
  // operator acknowledges it) the recovery block string.

  var W = null;

  async function open(opts) {
    opts = opts || {};
    // Target the current shell org so the create/register calls land on the
    // right DB (and the X-Graph-Org header matches), not the scopeless default.
    var _org = opts.org
      || (window.Autonomy && (window.Autonomy._activePluginOrg || window.Autonomy._activeShellOrg))
      || null;
    var orgQ = _org ? ('?org=' + encodeURIComponent(_org)) : '';
    W = {
      org: _org, step: 'loading', busy: false, error: null,
      registryUrl: null, orgKey: null, binding: null,
      orgUuid: null, rootPub: null, rootKey: null, armor: null,
      armorStored: false, recovery: 'none', recoveryPub: null,
      recoveryBlock: null, recoveryAck: false, resultBinding: null,
    };
    _render();
    document.addEventListener('keydown', _onKeydown);
    try {
      var results = await Promise.all([
        _fetchJsonOrNull('/api/network/registry', _org),
        _fetchJsonOrNull('/api/network/org-key' + orgQ, _org),
        _fetchJsonOrNull('/api/network/binding' + orgQ, _org),
      ]);
      if (!W) return;   // closed while loading
      W.registryUrl = results[0] ? results[0].registry_url : null;
      W.orgKey = results[1];
      W.binding = results[2];
    } catch (e) {
      if (!W) return;
      W.error = e.message || String(e);
    }
    // An org that already holds a network identity sees its status —
    // never the create flow (the root key is not a thing to re-roll).
    W.step = W.orgKey ? 'status' : 'intro';
    _render();
    return W.step;
  }

  // Escape closes the modal (unless a crypto step is mid-flight). Paired
  // with the X button and the backdrop click — three real ways out.
  function _onKeydown(ev) {
    if (ev.key === 'Escape' && W && !W.busy) { ev.preventDefault(); close(); }
  }

  function close() {
    document.removeEventListener('keydown', _onKeydown);
    W = null;   // drops the transient rootKey reference with it
    var el = document.getElementById('network-identity-modal');
    if (el) el.remove();
  }

  //: The org root is SEALED to the operator's PERSONAL root, never armored
  //: under a passphrase of its own. One password opens every org they own,
  //: and re-sealing to another recipient transfers ownership without ever
  //: exposing the seed. Both seeds live only inside this function and are
  //: zeroed in the same finally (I1).
  var ORG_ROOT_SEAL_PURPOSE = 'autonomy/org-root-armor/v1';

  async function _startCreate(personalPassword) {
    if (typeof personalPassword !== 'string' || !personalPassword) {
      throw new Error('enter your personal password');
    }
    var opened = await _I().openPersonalRoot(personalPassword);
    var pair = await generateEd25519();
    var recipient, sealedHex;
    try {
      recipient = await _I().deriveEncapsulationKeypair(
        opened.seed, ORG_ROOT_SEAL_PURPOSE);
      sealedHex = _I().bytesToHex(await _I().sealToEncapsulationKey(
        pair.seed, recipient.publicKeyHex, ORG_ROOT_SEAL_PURPOSE));
      W.rootPub = pair.pubHex;
      W.orgUuid = crypto.randomUUID();
      W.rootKey = await importSigningKey(pair.seed);
    } finally {
      pair.seed.fill(0); pair.seed = null;
      opened.seed.fill(0); opened.seed = null;
    }
    // Store the sealed key BEFORE registration: an identity that is
    // registered but not stored dies with this tab.
    await _postJson('/api/network/org-key/sealed', {
      org: W.org,
      root_pub: W.rootPub,
      sealed_root_key: sealedHex,
      owner_kem_pub: recipient.publicKeyHex,
      seal_purpose: ORG_ROOT_SEAL_PURPOSE,
    }, W.org);
    W.armorStored = true;
  }

  async function _mintRecovery() {
    var pair = await generateEd25519();
    var seedHex = _I().bytesToHex(pair.seed);
    pair.seed.fill(0);
    pair.seed = null;
    W.recoveryPub = pair.pubHex;
    W.recoveryBlock = buildRecoveryBlock(W.orgUuid, W.rootPub, pair.pubHex, seedHex);
    W.recovery = 'recovery-key';
    W.recoveryAck = false;
  }

  async function _register() {
    if (!W.rootKey) throw new Error('no root key in this ceremony run');
    var payload = {
      org_uuid: W.orgUuid,
      root_pub: W.rootPub,
      recovery_policy: W.recovery,
    };
    if (W.recovery === 'recovery-key') payload.recovery_pub = W.recoveryPub;
    var envelope = await signRegistration(W.rootKey, W.rootPub, payload);
    var result = await _postJson('/api/network/register', {
      org: W.org, envelope: envelope,
    }, W.org);
    W.resultBinding = result.binding;
    W.rootKey = null;         // ceremony over; the armor is the survivor
    W.recoveryBlock = null;   // rendered copy is the operator's now
  }

  // ── rendering ──────────────────────────────────────────────────────

  function _el(tag, attrs, text) {
    var el = document.createElement(tag);
    for (var k in (attrs || {})) el.setAttribute(k, attrs[k]);
    if (text != null) el.textContent = text;
    return el;
  }

  function _shortHex(hex) {
    return hex ? (hex.slice(0, 16) + '…') : '';
  }

  function _busySet(v) { if (W) { W.busy = v; _render(); } }

  function _fail(e) {
    if (!W) return;
    W.busy = false;
    W.error = (e && e.message) || String(e);
    _render();
  }

  function _step(next) {
    if (!W) return;
    W.busy = false;
    W.error = null;
    W.step = next;
    _render();
  }

  function _render() {
    var existing = document.getElementById('network-identity-modal');
    if (existing) existing.remove();
    if (!W) return;

    var overlay = _el('div', {
      id: 'network-identity-modal',
      'data-testid': 'network-identity-modal',
      'data-step': W.step,
      'class': 'network-identity-overlay',
    });
    var card = _el('div', { 'class': 'network-identity-card' });
    overlay.appendChild(card);
    overlay.addEventListener('click', function (ev) {
      if (ev.target === overlay && !W.busy) close();
    });

    // Always-present X — the modal is never a trap. Disabled only while a
    // crypto step is mid-flight (the backdrop + Escape are gated the same).
    var xBtn = _el('button', {
      id: 'network-identity-x', 'data-testid': 'network-identity-x',
      'class': 'network-identity-x', 'aria-label': 'Close', title: 'Close',
    }, '×');
    xBtn.addEventListener('click', function () { if (W && !W.busy) close(); });
    card.appendChild(xBtn);

    _renderStep(card);
    if (W.busy) {
      card.appendChild(_el('div', { 'class': 'network-key-meta' }, 'working…'));
    }
    if (W.error) {
      card.appendChild(_el('div', {
        'data-testid': 'network-identity-error',
        'class': 'network-signon-error',
      }, W.error));
    }
    document.body.appendChild(overlay);
  }

  function _title(card, text, sub) {
    card.appendChild(_el('h2', { 'class': 'network-identity-title' }, text));
    if (sub) card.appendChild(_el('p', { 'class': 'network-identity-lead' }, sub));
  }

  function _button(card, id, label, primary, onClick) {
    var btn = _el('button', {
      id: id, 'data-testid': id,
      'class': 'network-signon-action' + (primary ? '' : ' network-identity-quiet'),
    }, label);
    btn.addEventListener('click', function () { if (!W || !W.busy) onClick(); });
    card.appendChild(btn);
    return btn;
  }

  function _renderStep(card) {
    if (W.step === 'loading') {
      _title(card, 'Getting started', 'Loading identity state…');
      return;
    }

    if (W.step === 'status') {
      _title(card, 'Your identity',
        'This org already holds its sovereign root key.');
      var st = _el('div', { 'data-testid': 'network-identity-status',
                            'class': 'network-key-row' });
      st.appendChild(_el('div', { 'class': 'network-key-id',
        'data-testid': 'network-identity-status-root' },
        'root ' + _shortHex(W.orgKey.root_pub || '')));
      if (W.binding) {
        st.appendChild(_el('div', { 'class': 'network-key-meta' },
          'registered on ' + (W.binding.registry || W.binding.registry_url)));
        st.appendChild(_el('div', { 'class': 'network-key-meta',
          'data-testid': 'network-identity-binding-expiry' },
          'binding valid until ' + W.binding.binding_expires_at));
        st.appendChild(_el('div', { 'class': 'network-key-meta' },
          'recovery: ' + ((W.binding.recovery_policy || {}).mode || 'none')));
      } else {
        st.appendChild(_el('div', { 'class': 'network-key-meta' },
          'Registration incomplete — the key is stored but the org is not ' +
          'bound on the registry yet. Enter the passphrase to finish.'));
      }
      card.appendChild(st);
      if (!W.binding) {
        var pp = _el('input', {
          type: 'password', id: 'network-identity-resume-passphrase',
          'data-testid': 'network-identity-resume-passphrase',
          placeholder: 'Org passphrase', 'class': 'network-signon-input',
        });
        card.appendChild(pp);
        _button(card, 'network-identity-resume', 'Complete registration', true,
          function () {
            _busySet(true);
            _startResume(pp.value)
              .then(function () { _step('recovery'); })
              .catch(_fail);
          });
      }
      _button(card, 'network-identity-close', 'Close', false, close);
      return;
    }

    if (W.step === 'intro') {
      _title(card, 'Create your identity',
        'This generates a sovereign signing key in this browser — the anchor ' +
        'of everything your org signs and publishes. The key stays yours: ' +
        'only a passphrase-encrypted copy is stored. Registering it with a ' +
        'registry is a separate, optional step you can do later.');
      _button(card, 'network-identity-continue', 'Continue', true,
        function () { _step('passphrase'); });
      _button(card, 'network-identity-cancel', 'Not now', false, close);
      return;
    }

    if (W.step === 'passphrase') {
      _title(card, 'Set a passphrase',
        'This protects the org key. You’ll enter it to sign on and to ' +
        'approve root-level actions. It encrypts the key locally — it is ' +
        'never uploaded, and there is no “forgot passphrase” email.');
      var p1 = _el('input', {
        type: 'password', id: 'network-identity-passphrase',
        'data-testid': 'network-identity-passphrase',
        placeholder: 'Passphrase (min 8 characters)', 'class': 'network-signon-input',
      });
      var p2 = _el('input', {
        type: 'password', id: 'network-identity-passphrase2',
        'data-testid': 'network-identity-passphrase2',
        placeholder: 'Confirm passphrase', 'class': 'network-signon-input',
      });
      card.appendChild(p1);
      card.appendChild(p2);
      _button(card, 'network-identity-generate', 'Generate org key', true,
        function () {
          _busySet(true);
          _startCreate(p1.value, p2.value)
            .then(function () { _step('recovery'); })
            .catch(_fail);
        });
      _button(card, 'network-identity-back', 'Back', false,
        function () { _step('intro'); });
      return;
    }

    if (W.step === 'recovery') {
      _title(card, 'Add a way to recover?',
        'If the passphrase is ever lost, a recovery key is the only way ' +
        'to rebind the org. Generate one now and keep it cold, or declare ' +
        'no recovery — that choice is registered and cannot be widened later.');
      if (!W.recoveryBlock) {
        _button(card, 'network-identity-recovery-key', 'Generate recovery key', true,
          function () {
            _busySet(true);
            _mintRecovery()
              .then(function () { W.busy = false; _render(); })
              .catch(_fail);
          });
        _button(card, 'network-identity-recovery-none', 'No recovery (default)', false,
          function () {
            W.recovery = 'none';
            W.recoveryPub = null;
            W.recoveryBlock = null;
            _step('register');
          });
      } else {
        var pre = _el('pre', {
          id: 'network-identity-recovery-block',
          'data-testid': 'network-identity-recovery-block',
          'class': 'network-identity-block',
        }, W.recoveryBlock);
        card.appendChild(pre);
        var dl = _el('a', {
          id: 'network-identity-recovery-download',
          'data-testid': 'network-identity-recovery-download',
          'class': 'network-identity-download',
          download: 'autonomy-recovery-key.txt',
          href: URL.createObjectURL(new Blob([W.recoveryBlock],
                                            { type: 'text/plain' })),
        }, 'Download recovery key');
        card.appendChild(dl);
        var ackRow = _el('label', { 'class': 'network-identity-ack' });
        var ack = _el('input', {
          type: 'checkbox', id: 'network-identity-recovery-ack',
          'data-testid': 'network-identity-recovery-ack',
        });
        ack.addEventListener('change', function () {
          W.recoveryAck = ack.checked;
          var btn = document.getElementById('network-identity-recovery-continue');
          if (btn) btn.disabled = !ack.checked;
        });
        ackRow.appendChild(ack);
        ackRow.appendChild(document.createTextNode(
          ' I stored this block somewhere safe and offline'));
        card.appendChild(ackRow);
        var cont = _button(card, 'network-identity-recovery-continue', 'Continue', true,
          function () { if (W.recoveryAck) _step('register'); });
        cont.disabled = !W.recoveryAck;
      }
      return;
    }

    if (W.step === 'register') {
      _title(card, 'Register with a registry (optional)',
        'Your identity already exists on this device. Registering publishes ' +
        'your org’s reachability so others can resolve the share-links you ' +
        'publish — it’s signed by your new key. You can skip this and register ' +
        'any time later.');
      var rev = _el('div', { 'class': 'network-key-row' });
      rev.appendChild(_el('div', { 'class': 'network-key-id' },
        'root ' + _shortHex(W.rootPub)));
      rev.appendChild(_el('div', { 'class': 'network-key-meta' },
        'org UUID ' + W.orgUuid));
      rev.appendChild(_el('div', { 'class': 'network-key-meta' },
        'registry ' + (W.registryUrl || '(server-configured)')));
      rev.appendChild(_el('div', { 'class': 'network-key-meta' },
        'recovery: ' + W.recovery));
      card.appendChild(rev);
      _button(card, 'network-identity-register', 'Register now', true,
        function () {
          _busySet(true);
          _register()
            .then(function () { _step('success'); })
            .catch(_fail);
        });
      _button(card, 'network-identity-skip', 'Not now — finish', false,
        function () { _step('success'); });
      return;
    }

    if (W.step === 'success') {
      var registered = !!W.resultBinding;
      _title(card, 'Your identity is ready',
        registered
          ? 'Your key is created and registered. Sign in with your passphrase ' +
            'to start publishing.'
          : 'Your key is created and stored on this device. Sign in with your ' +
            'passphrase to use it — you can register with a registry any time.');
      var ok = _el('div', { 'data-testid': 'network-identity-success',
                            'class': 'network-key-row' });
      ok.appendChild(_el('div', { 'class': 'network-key-id' },
        'root ' + _shortHex(W.rootPub)));
      if (W.resultBinding) {
        ok.appendChild(_el('div', { 'class': 'network-key-meta',
          'data-testid': 'network-identity-binding-expiry' },
          'binding valid until ' + W.resultBinding.binding_expires_at));
      }
      card.appendChild(ok);
      _button(card, 'network-identity-done', 'Done', true, function () {
        close();
      });
      return;
    }
  }

  // ── exports ────────────────────────────────────────────────────────

  window.AutonomyNetworkIdentity = {
    open: open,
    close: close,
    // Internals exposed for the L2.B sweep; the enforcement they exercise
    // lives in the real ceremony paths above.
    _internals: {
      generateEd25519: generateEd25519,
      armorSeed: armorSeed,
      importSigningKey: importSigningKey,
      signRegistration: signRegistration,
      buildRecoveryBlock: buildRecoveryBlock,
      state: function () { return W; },
      overrideIterations: function (n) { _iterations = Math.max(10000, n | 0); },
      defaultIterations: DEFAULT_ITERATIONS,
    },
  };
})();
