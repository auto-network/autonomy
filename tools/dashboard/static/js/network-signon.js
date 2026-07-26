import {
  canonicalJson,
  hexToBytes,
  bytesToHex,
  domainBytes,
  decryptArmor,
  importEd25519RootSigningKey,
} from './ceremony/primitives.js';

var CryptoKeyConstructor = globalThis.CryptoKey;
if (
  !CryptoKeyConstructor
  && typeof process !== 'undefined'
  && process.versions && process.versions.node
) {
  ({ CryptoKey: CryptoKeyConstructor } = (await import('node:crypto')).webcrypto);
}
if (!CryptoKeyConstructor) {
  throw new Error('network sign-on requires WebCrypto CryptoKey support');
}

/* auto.network sign-on ceremony — the C2 session-key surface (spec §6.3).
 *
 * Signing on decrypts the org root key ONCE (passphrase → PBKDF2 →
 * AES-GCM, the armor produced by tools/network/idkit/armor.py), mints a
 * NON-EXTRACTABLE WebCrypto Ed25519 session key with a root-signed
 * delegation certificate, and drops the root plaintext immediately: the
 * seed bytes are zeroed and the imported root CryptoKey never leaves the
 * minting function. The session key + cert live in IndexedDB; routine
 * operations sign with the session key and never see a passphrase again.
 *
 * Installs the seam C3's link approvals build against:
 *
 *   window.AutonomyNetworkSigner = {
 *     available(): boolean,
 *     signRegistryRequest(method, path, payload): Promise<envelope>,
 *   }
 *
 * The envelope matches tools/network/registry/signing.py exactly:
 *   {v, signer, ts, payload, cert, sig} with sig over
 *   REQUEST_DOMAIN + canonical_json({v, method, path, ts, signer, payload}).
 *
 * Canonical JSON here mirrors tools/network/idkit/canonical.py BYTE FOR
 * BYTE (sorted keys, no whitespace, ensure_ascii, ints only) — the
 * registry re-canonicalizes and compares, so any divergence is a signature
 * failure. The L2.B sweep cross-checks a vector against the Python side.
 *
 * Invariants enforced client-side (the registry re-enforces its own):
 *   I1 — root plaintext exists only inside signOn()/revoke step-up; zeroed
 *        and dropped before either returns. Nothing root-shaped is stored.
 *   §6.3 — the session private key is created with extractable:false and
 *        _installSession refuses any key claiming otherwise.
 *   I7 — the cert validity window is enforced on load: an expired or
 *        not-yet-valid cert reads as signed-out and the store is purged.
 */
var configureCore;
var signOnCore;
var signRegistryRequestCore;

(function () {
  'use strict';

  var _domainBytes = domainBytes;
  var _importRootKey = importEd25519RootSigningKey;

  var CERT_DOMAIN = 'autonomy.idkit.cert.v1\n';
  var REQUEST_DOMAIN = 'autonomy.network.registry.request.v1\n';
  var REVOCATION_DOMAIN = 'autonomy.idkit.revocation.v1\n';

  var DEFAULT_TTL_S = 24 * 3600;          // spec §6.3 default
  var MIN_TTL_S = 60;
  var MAX_TTL_S = 30 * 24 * 3600;
  var NOT_BEFORE_SKEW_S = 60;             // tolerate modest clock skew
  var SESSION_SCOPES = [                  // spec §6.3 defaults; sorted (idkit)
    'delegate:agent', 'link:publish', 'link:revoke', 'tunnel:serve',
    'viewer:identify',
  ];

  var DB_NAME = 'autonomy-network';
  var DB_STORE = 'session';
  var DB_KEY = 'current';

  // ── IndexedDB session store ────────────────────────────────────────

  function _idb() {
    return new Promise(function (res, rej) {
      var req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = function () { req.result.createObjectStore(DB_STORE); };
      req.onsuccess = function () { res(req.result); };
      req.onerror = function () { rej(req.error); };
    });
  }

  async function _idbOp(mode, fn) {
    var db = await _idb();
    try {
      return await new Promise(function (res, rej) {
        var store = db.transaction(DB_STORE, mode).objectStore(DB_STORE);
        var r = fn(store);
        r.onsuccess = function () { res(r.result); };
        r.onerror = function () { rej(r.error); };
      });
    } finally {
      db.close();
    }
  }

  // ── module state ───────────────────────────────────────────────────

  var _state = {
    session: null,     // {key: CryptoKey, certWire, cert, org, registryUrl,
                       //  rootPub, orgSlug, createdAt}
  };
  var _storage = null;
  var _transport = null;

  function _nowS() { return Math.floor(Date.now() / 1000); }

  function _sessionLive(session) {
    // Both window bounds: a FUTURE not_before is as dead as an expired
    // not_after — the registry 403s either, so the chrome must never
    // claim signed-in for a cert the registry would refuse.
    if (!session || !session.cert) return false;
    var now = _nowS();
    return typeof session.cert.not_before === 'number' &&
           typeof session.cert.not_after === 'number' &&
           now >= session.cert.not_before && now < session.cert.not_after;
  }

  function _browserSubjectId() {
    var KEY = 'autonomy.network.browser-id';
    var id = null;
    try { id = localStorage.getItem(KEY); } catch (e) { /* ignore */ }
    if (!id) {
      id = 'browser-' + bytesToHex(crypto.getRandomValues(new Uint8Array(4)));
      try { localStorage.setItem(KEY, id); } catch (e) { /* ignore */ }
    }
    return id;
  }

  async function _loadFromStore() {
    var rec = null;
    try {
      rec = await _storage.getSession();
    } catch (e) {
      return null;
    }
    if (!rec || !rec.key || typeof rec.certWire !== 'string') return null;
    var cert;
    try { cert = JSON.parse(rec.certWire); } catch (e) { return null; }
    var session = {
      key: rec.key, certWire: rec.certWire, cert: cert, org: rec.org,
      registryUrl: rec.registryUrl, rootPub: rec.rootPub,
      orgSlug: rec.orgSlug || null, createdAt: rec.createdAt,
    };
    // Tested rejection: a session key that is somehow extractable, or a
    // cert outside its validity window (expired OR not yet valid), must
    // not come back to life on load.
    if (rec.key.extractable !== false || !_sessionLive(session)) {
      try { await _storage.clearSession(); } catch (e) { /* ignore */ }
      return null;
    }
    return session;
  }

  async function _installSession(record) {
    if (!record || !record.key || record.key.extractable !== false) {
      throw new Error('refusing to install a session key that is not a ' +
        'non-extractable WebCrypto key');
    }
    if (!(record.key instanceof CryptoKeyConstructor) ||
        record.key.type !== 'private') {
      throw new Error('session key must be a private CryptoKey');
    }
    var cert = JSON.parse(record.certWire);
    var now = _nowS();
    if (typeof cert.not_before !== 'number' || typeof cert.not_after !== 'number' ||
        now < cert.not_before || now >= cert.not_after) {
      throw new Error('refusing to install a session certificate outside its ' +
        'validity window (expired or not yet valid)');
    }
    await _storage.putSession(record);
    _state.session = {
      key: record.key, certWire: record.certWire, cert: cert, org: record.org,
      registryUrl: record.registryUrl, rootPub: record.rootPub,
      orgSlug: record.orgSlug || null, createdAt: record.createdAt,
    };
    return _state.session;
  }

  // ── ceremonies ─────────────────────────────────────────────────────

  async function _fetchJson(url, org) {
    // auto.network routes scope by the X-Graph-Org header; a bare ?org= is
    // refused cross-org without it. Plain fetch() carries no header, so pass
    // the org through explicitly.
    var resp = await _transport.fetch(
      url, org ? { headers: { 'X-Graph-Org': org } } : undefined);
    if (!resp.ok) {
      var detail = '';
      try { detail = (await resp.json()).error || ''; } catch (e) { /* ignore */ }
      var error = new Error(detail || ('request failed: ' + url + ' → ' + resp.status));
      error.status = resp.status;
      throw error;
    }
    return resp.json();
  }

  // Like _fetchJson but a 404 reads as "not configured" → null. Used for
  // the OPTIONAL registry binding: unlocking a key is a local act and
  // must not require the org to have registered with any registry (the
  // sovereign model — auto.network is a broker, not part of sign-in).
  async function _fetchJsonOrNull(url, org) {
    var resp = await _transport.fetch(
      url, org ? { headers: { 'X-Graph-Org': org } } : undefined);
    if (resp.status === 404) return null;
    if (!resp.ok) {
      var detail = '';
      try { detail = (await resp.json()).error || ''; } catch (e) { /* ignore */ }
      throw new Error(detail || ('request failed: ' + url + ' → ' + resp.status));
    }
    return resp.json();
  }

  // Passphrase → decrypt root ONCE → mint session key + cert → drop root.
  async function signOn(passphrase, opts) {
    opts = opts || {};
    var ttl = opts.ttlSeconds || DEFAULT_TTL_S;
    if (typeof ttl !== 'number' || !Number.isSafeInteger(ttl) ||
        ttl < MIN_TTL_S || ttl > MAX_TTL_S) {
      throw new Error('session TTL must be between 1 minute and 30 days');
    }
    var orgQ = opts.org ? ('?org=' + encodeURIComponent(opts.org)) : '';
    // Sign-in unlocks YOUR key locally. The org key is REQUIRED; the
    // registry binding is OPTIONAL — an org that has minted its sovereign
    // key but not (yet) registered with any broker can still sign in. When
    // a binding is present its coordinates pin the session; when absent the
    // sovereign root itself anchors the local session.
    var orgKey = await _fetchJson('/api/network/org-key' + orgQ, opts.org);
    if (!orgKey.armored_private_key) {
      throw new Error('no identity key is stored for this org yet — create ' +
        'one from the getting-started flow first');
    }
    var binding = await _fetchJsonOrNull('/api/network/binding' + orgQ, opts.org);
    var bound = !!(binding && binding.org_uuid && binding.root_pub &&
                   binding.registry_url);
    // The root the session must match: the registry-bound root when the
    // org is registered, else the root recorded alongside the stored key.
    var expectedRoot = bound ? binding.root_pub : (orgKey.root_pub || null);
    var orgId = bound ? binding.org_uuid : orgKey.root_pub;
    var registryUrl = bound ? binding.registry_url : null;

    var opened = await decryptArmor(orgKey.armored_private_key, passphrase);
    var diagnostics = { rootDropped: false, extractable: null };
    var sessionKeys, certWire;
    try {
      if (expectedRoot && opened.rootPub !== expectedRoot) {
        throw new Error('the stored org key does not match its recorded ' +
          'root — refusing to sign in');
      }
      var rootKey = await _importRootKey(opened.seed);
      // I1: the plaintext seed dies here, before any signing happens.
      opened.seed.fill(0);
      opened.seed = null;

      sessionKeys = await crypto.subtle.generateKey(
        { name: 'Ed25519' }, false, ['sign', 'verify']);
      var sessionPub = bytesToHex(
        await crypto.subtle.exportKey('raw', sessionKeys.publicKey));

      var subjectId = await _storage.getSubjectId();
      if (!subjectId) {
        subjectId = 'browser-' +
          bytesToHex(crypto.getRandomValues(new Uint8Array(4)));
        await _storage.setSubjectId(subjectId);
      }

      var now = _nowS();
      var certPayload = {
        v: 1,
        child_pub: sessionPub,
        scope: SESSION_SCOPES.slice(),
        org: orgId,
        subject: { kind: 'operator', id: subjectId },
        not_before: now - NOT_BEFORE_SKEW_S,
        not_after: now + ttl,
      };
      var sigBytes = await crypto.subtle.sign(
        'Ed25519', rootKey,
        _domainBytes(CERT_DOMAIN, canonicalJson(certPayload)));
      rootKey = null;   // I1: last root reference dropped
      diagnostics.rootDropped = true;

      var certFull = Object.assign({}, certPayload, { sig: bytesToHex(sigBytes) });
      certWire = canonicalJson(certFull);
    } finally {
      if (opened.seed) { opened.seed.fill(0); opened.seed = null; }
    }

    diagnostics.extractable = sessionKeys.privateKey.extractable;
    var session = await _installSession({
      key: sessionKeys.privateKey,
      certWire: certWire,
      org: orgId,
      registryUrl: registryUrl,
      rootPub: expectedRoot || opened.rootPub,
      orgSlug: opts.org || null,
      createdAt: _nowS(),
    });
    return {
      sessionPub: session.cert.child_pub,
      certWire: certWire,
      notAfter: session.cert.not_after,
      diagnostics: diagnostics,
    };
  }

  // Provision the org's tunnel SERVING delegate — a standalone step in a
  // publish approve, fired only when the enrich precondition
  // (serve_cert_required) says no usable serve-cert exists (the rare
  // first-publish / post-expiry path; the common case already has one and
  // skips this entirely).
  //
  // The delegate is ROOT-signed: a 30-day serving TTL cannot nest inside the
  // 24h session cert, so a session-key sub-delegate will not do. It signs the
  // SAME properties idkit's issue_cert does — {v, child_pub,
  // scope:['tunnel:serve'], org, subject, not_before, not_after} over
  // CERT_DOMAIN — with the org ROOT, decrypted from the armor with the same
  // approve password and zeroed the instant it is imported (I1).
  //
  // Its key is the ONE key this system exports: unlike the session key
  // (extractable:false, browser-only), the serving delegate's private half is
  // exported and POSTed to /api/network/serve-cert, because the unattended
  // connector subprocess must sign SERVER_HELLO with it. The server re-verifies
  // the chain to the org's OWN bound root before storing the key 0600.
  async function provisionServeCert(passphrase, opts) {
    opts = opts || {};
    var orgUuid = opts.orgUuid;
    if (!orgUuid) throw new Error('serve-cert provisioning requires the org uuid');
    var orgSlug = opts.org || null;
    var orgHeaders = orgSlug ? { 'X-Graph-Org': orgSlug } : {};
    var orgQ = orgSlug ? ('?org=' + encodeURIComponent(orgSlug)) : '';

    var keyResp = await fetch('/api/network/org-key' + orgQ, { headers: orgHeaders });
    if (!keyResp.ok) {
      throw new Error('could not load the org signing key (' + keyResp.status + ')');
    }
    var orgKey = await keyResp.json();
    if (!orgKey.armored_private_key) {
      throw new Error('this org has no signing key to mint a serving delegate');
    }

    var opened = await decryptArmor(orgKey.armored_private_key, passphrase);
    var privateKeyHex = null;
    try {
      var rootKey = await _importRootKey(opened.seed);
      opened.seed.fill(0); opened.seed = null;   // I1: root seed gone at import

      // The one exportable key: the server needs its private half to drive the
      // unattended connector. extractable:true, unlike the session key.
      var delegate = await crypto.subtle.generateKey(
        { name: 'Ed25519' }, true, ['sign', 'verify']);
      var childPub = bytesToHex(new Uint8Array(
        await crypto.subtle.exportKey('raw', delegate.publicKey)));
      var pkcs8 = new Uint8Array(await crypto.subtle.exportKey('pkcs8', delegate.privateKey));
      privateKeyHex = bytesToHex(pkcs8.slice(16, 48));  // RFC 8410: 16B header + 32B seed
      pkcs8.fill(0);

      var now = _nowS();
      var certPayload = {
        v: 1,
        child_pub: childPub,
        scope: ['tunnel:serve'],
        org: orgUuid,
        subject: { kind: 'operator', id: _browserSubjectId() },
        not_before: now - NOT_BEFORE_SKEW_S,
        not_after: now + MAX_TTL_S,          // 30-day serving delegate
      };
      var sig = bytesToHex(new Uint8Array(await crypto.subtle.sign(
        'Ed25519', rootKey, _domainBytes(CERT_DOMAIN, canonicalJson(certPayload)))));
      rootKey = null;                        // I1: last root reference dropped
      var certWire = canonicalJson(Object.assign({}, certPayload, { sig: sig }));

      var resp = await fetch('/api/network/serve-cert', {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, orgHeaders),
        body: JSON.stringify({ org: orgSlug, cert: certWire, private_key: privateKeyHex }),
      });
      var body = await resp.json().catch(function () { return {}; });
      if (!resp.ok || body.ok === false) {
        throw new Error(body.error || ('serve-cert provisioning was refused (' + resp.status + ')'));
      }
      return { childPub: childPub, notAfter: certPayload.not_after };
    } finally {
      if (opened && opened.seed) { opened.seed.fill(0); opened.seed = null; }
      privateKeyHex = null;   // drop the reference (a JS string cannot be zeroed)
    }
  }

  // Sign-out destroys the key and cert locally (spec §6.3): the store is
  // CLEARED, not just the current row.
  async function signOut() {
    _state.session = null;
    try { await _idbOp('readwrite', function (s) { return s.clear(); }); } catch (e) { /* ignore */ }
  }

  // Root step-up: revoking a session key is a root-authority act — it
  // takes the passphrase again and publishes to /v1/revocations (§4.5)
  // through the dashboard's forwarding route.
  async function revokeCurrentKey(passphrase, reason) {
    var session = _state.session;
    if (!session) throw new Error('no session key to revoke');
    if (!_sessionLive(session)) {
      // Natural expiry already ended this key's life (I7); local cleanup
      // is all that is left to do.
      await signOut();
      return { revoked: false, expired: true };
    }
    var orgQ = session.orgSlug ? ('?org=' + encodeURIComponent(session.orgSlug)) : '';
    var orgKey = await _fetchJson('/api/network/org-key' + orgQ, session.orgSlug);
    if (!orgKey.armored_private_key) {
      throw new Error('no auto.network org key is stored for this org');
    }
    var opened = await decryptArmor(orgKey.armored_private_key, passphrase);
    var recordWire;
    try {
      if (opened.rootPub !== session.rootPub) {
        throw new Error('the stored org key does not match this session\'s root');
      }
      var rootKey = await _importRootKey(opened.seed);
      opened.seed.fill(0);
      opened.seed = null;
      var payload = {
        v: 1,
        revoked_key_id: session.cert.child_pub,
        org: session.org,
        revoked_at: _nowS(),
        expires_at: session.cert.not_after,   // I7: bounded by natural expiry
        issuer_pub: session.rootPub,
      };
      if (reason) payload.reason = String(reason).slice(0, 512);
      var sigBytes = await crypto.subtle.sign(
        'Ed25519', rootKey,
        _domainBytes(REVOCATION_DOMAIN, canonicalJson(payload)));
      rootKey = null;
      recordWire = canonicalJson(Object.assign({}, payload, { sig: bytesToHex(sigBytes) }));
    } finally {
      if (opened.seed) { opened.seed.fill(0); opened.seed = null; }
    }

    var revHeaders = { 'Content-Type': 'application/json' };
    if (session.orgSlug) revHeaders['X-Graph-Org'] = session.orgSlug;
    var resp = await fetch('/api/network/revocations', {
      method: 'POST', headers: revHeaders,
      body: JSON.stringify({
        org: session.orgSlug, record: recordWire, revoked_cert: session.certWire,
      }),
    });
    var body = await resp.json().catch(function () { return {}; });
    if (!resp.ok || body.ok === false) {
      throw new Error(body.error || 'the registry refused the revocation');
    }
    await signOut();
    return { revoked: true, revoked_key_id: body.revoked_key_id };
  }

  function listKeys() {
    var s = _state.session;
    if (!s) return [];
    return [{
      key_id: s.cert.child_pub,
      subject: s.cert.subject,
      scope: s.cert.scope,
      not_after: s.cert.not_after,
      org: s.org,
      this_browser: true,
    }];
  }

  // ── the C3 signer seam ─────────────────────────────────────────────

  var _ready = null;

  function configure(adapters) {
    var storage = adapters && adapters.storage;
    var storageMethods = [
      'getSession', 'putSession', 'clearSession', 'getSubjectId', 'setSubjectId',
    ];
    for (var i = 0; i < storageMethods.length; i++) {
      var method = storageMethods[i];
      if (!storage || typeof storage[method] !== 'function') {
        throw new Error('storage adapter is missing method ' + method);
      }
    }
    var transport = adapters && adapters.transport;
    if (!transport || typeof transport.fetch !== 'function') {
      throw new Error('transport adapter is missing method fetch');
    }

    _storage = storage;
    _transport = transport;
    _ready = _loadFromStore().then(function (session) {
      _state.session = session;
      return session;
    }).catch(function () { return null; });
    return _ready;
  }

  function available() {
    return _sessionLive(_state.session);
  }

  async function signRegistryRequest(method, path, payload) {
    await _ready;
    var session = _state.session;
    if (!_sessionLive(session)) {
      throw new Error('no live operator session key — sign on to auto.network first');
    }
    if (typeof method !== 'string' || typeof path !== 'string') {
      throw new Error('signRegistryRequest needs (method, path, payload)');
    }
    var ts = _nowS();
    var signer = session.cert.child_pub;
    var signingInput = _domainBytes(REQUEST_DOMAIN, canonicalJson({
      v: 1,
      method: method.toUpperCase(),
      path: path,
      ts: ts,
      signer: signer,
      payload: payload,
    }));
    var sig = bytesToHex(await crypto.subtle.sign('Ed25519', session.key, signingInput));
    return {
      v: 1, signer: signer, ts: ts, payload: payload,
      cert: session.certWire, sig: sig,
    };
  }

  // Expiry watchdog: an expired delegation is destroyed locally. This
  // module no longer owns shell chrome; authority is acquired on demand by
  // the action-specific Gate 2 flow.
  if (typeof window !== 'undefined') {
    setInterval(function () {
      if (_state.session && !_sessionLive(_state.session)) {
        signOut();
      }
    }, 30000);
  }

  // ── init + exports ─────────────────────────────────────────────────

  var networkSigner = {
    available: available,
    signRegistryRequest: signRegistryRequest,
  };

  var networkSession = {
    configure: configure,
    ready: function () { return _ready; },
    state: function () {
      var s = _state.session;
      return s ? {
        signedIn: available(), sessionPub: s.cert.child_pub,
        notAfter: s.cert.not_after, org: s.org, subject: s.cert.subject,
      } : { signedIn: false };
    },
    signOn: signOn,
    signOut: signOut,
    provisionServeCert: provisionServeCert,
    revokeCurrentKey: revokeCurrentKey,
    listKeys: listKeys,
    // Internals exposed for the L2.B sweep + cross-language vectors; the
    // enforcement they exercise lives in the real paths above.
    _internals: {
      canonicalJson: canonicalJson,
      decryptArmor: decryptArmor,
      provisionServeCert: provisionServeCert,
      installSession: _installSession,
      loadFromStore: _loadFromStore,
      hexToBytes: hexToBytes,
      bytesToHex: bytesToHex,
      domains: {
        cert: CERT_DOMAIN, request: REQUEST_DOMAIN, revocation: REVOCATION_DOMAIN,
      },
    },
  };

  configureCore = configure;
  signOnCore = signOn;
  signRegistryRequestCore = signRegistryRequest;

  if (typeof window !== 'undefined') {
    window.AutonomyNetworkSigner = networkSigner;
    window.AutonomyNetworkSession = networkSession;
  }
})();

export {
  configureCore as configure,
  signOnCore as signOn,
  signRegistryRequestCore as signRegistryRequest,
};
