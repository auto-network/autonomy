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
(function () {
  'use strict';

  var CERT_DOMAIN = 'autonomy.idkit.cert.v1\n';
  var REQUEST_DOMAIN = 'autonomy.network.registry.request.v1\n';
  var REVOCATION_DOMAIN = 'autonomy.idkit.revocation.v1\n';
  var ARMOR_AAD_PREFIX = 'autonomy.idkit.armor.v1\n';
  var ARMOR_BEGIN = '-----BEGIN AUTONOMY NETWORK ROOT KEY-----';
  var ARMOR_END = '-----END AUTONOMY NETWORK ROOT KEY-----';

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

  // Raw 32-byte Ed25519 seed → PKCS#8 (RFC 8410) for WebCrypto import.
  var PKCS8_ED25519_PREFIX = [
    0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06,
    0x03, 0x2b, 0x65, 0x70, 0x04, 0x22, 0x04, 0x20,
  ];

  // ── canonical JSON (mirror of tools/network/idkit/canonical.py) ────

  var _SHORT_ESCAPES = {
    8: '\\b', 9: '\\t', 10: '\\n', 12: '\\f', 13: '\\r',
    34: '\\"', 92: '\\\\',
  };

  function _escapeString(s) {
    var out = '"';
    for (var i = 0; i < s.length; i++) {
      var c = s.charCodeAt(i);   // UTF-16 code units — Python's ensure_ascii
      if (_SHORT_ESCAPES[c]) {   // emits the same surrogate-pair escapes
        out += _SHORT_ESCAPES[c];
      } else if (c < 0x20 || c > 0x7e) {
        out += '\\u' + ('000' + c.toString(16)).slice(-4);
      } else {
        out += s[i];
      }
    }
    return out + '"';
  }

  function _codePoints(s) {
    return Array.from(s).map(function (ch) { return ch.codePointAt(0); });
  }

  // Python sorts str keys by code POINT; JS '<' compares UTF-16 code
  // units, which disagrees once astral-plane keys are involved.
  function _comparePy(a, b) {
    var pa = _codePoints(a), pb = _codePoints(b);
    var n = Math.min(pa.length, pb.length);
    for (var i = 0; i < n; i++) {
      if (pa[i] !== pb[i]) return pa[i] - pb[i];
    }
    return pa.length - pb.length;
  }

  function canonicalJson(value) {
    if (value === null) return 'null';
    var t = typeof value;
    if (t === 'boolean') return value ? 'true' : 'false';
    if (t === 'number') {
      if (!Number.isSafeInteger(value)) {
        throw new Error('canonical JSON allows integers only, got ' + value);
      }
      return String(value);
    }
    if (t === 'string') return _escapeString(value);
    if (Array.isArray(value)) {
      return '[' + value.map(canonicalJson).join(',') + ']';
    }
    if (t === 'object') {
      var keys = Object.keys(value).sort(_comparePy);
      var parts = [];
      for (var i = 0; i < keys.length; i++) {
        var v = value[keys[i]];
        if (v === undefined) continue;
        parts.push(_escapeString(keys[i]) + ':' + canonicalJson(v));
      }
      return '{' + parts.join(',') + '}';
    }
    throw new Error('type ' + t + ' is not allowed in canonical JSON');
  }

  // ── byte helpers ───────────────────────────────────────────────────

  var _te = new TextEncoder();

  function hexToBytes(hex) {
    if (typeof hex !== 'string' || hex.length % 2 !== 0 || /[^0-9a-f]/.test(hex)) {
      throw new Error('expected lowercase hex');
    }
    var out = new Uint8Array(hex.length / 2);
    for (var i = 0; i < out.length; i++) {
      out[i] = parseInt(hex.substr(i * 2, 2), 16);
    }
    return out;
  }

  function bytesToHex(bytes) {
    var b = new Uint8Array(bytes);
    var out = '';
    for (var i = 0; i < b.length; i++) out += ('0' + b[i].toString(16)).slice(-2);
    return out;
  }

  function b64ToBytes(b64) {
    var bin = atob(b64);
    var out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function _domainBytes(domain, canonicalStr) {
    var d = _te.encode(domain);
    var c = _te.encode(canonicalStr);
    var out = new Uint8Array(d.length + c.length);
    out.set(d, 0);
    out.set(c, d.length);
    return out;
  }

  // ── armor decrypt (mirror of tools/network/idkit/armor.py) ─────────

  async function decryptArmor(armorText, passphrase) {
    if (typeof armorText !== 'string') throw new Error('armor must be text');
    var lines = armorText.split('\n').map(function (l) { return l.trim(); })
      .filter(function (l) { return l.length > 0; });
    if (lines.length < 3 || lines[0] !== ARMOR_BEGIN || lines[lines.length - 1] !== ARMOR_END) {
      throw new Error('this is not an auto.network root key armor');
    }
    var data;
    try {
      data = JSON.parse(new TextDecoder().decode(b64ToBytes(lines.slice(1, -1).join(''))));
    } catch (e) {
      throw new Error('armor body does not decode');
    }
    // STRICT shape check mirroring armor.py's parse_armor: exact key
    // sets, formats, and decoded lengths. Fail closed on ANY unknown
    // field — a tolerated extra field is a smuggling channel for
    // plaintext key material inside an otherwise-valid armor (I1).
    function sameKeys(obj, keys) {
      return obj && typeof obj === 'object' && !Array.isArray(obj) &&
        Object.keys(obj).sort().join(',') === keys.slice().sort().join(',');
    }
    // Decoded length ONLY if the string is canonical base64 — decode,
    // re-encode, compare exactly, mirroring armor.py. atob() tolerates
    // nonzero pad bits (and other lax forms) that Python REJECTS; the
    // two sides must accept one identical byte form or a blob could be
    // valid on one side of the C1/C2 contract and refused on the other.
    function b64CanonLen(s) {
      if (typeof s !== 'string') return -1;
      var bytes;
      try { bytes = b64ToBytes(s); } catch (e) { return -1; }
      var bin = '';
      for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
      if (btoa(bin) !== s) return -1;
      return bytes.length;
    }
    if (!sameKeys(data, ['v', 'kdf', 'cipher', 'root_pub', 'ct']) ||
        data.v !== 1 ||
        !sameKeys(data.kdf, ['name', 'hash', 'iterations', 'salt']) ||
        data.kdf.name !== 'PBKDF2' || data.kdf.hash !== 'SHA-256' ||
        !Number.isSafeInteger(data.kdf.iterations) ||
        data.kdf.iterations < 10000 || data.kdf.iterations > 100000000 ||
        !sameKeys(data.cipher, ['name', 'iv']) ||
        data.cipher.name !== 'AES-256-GCM' ||
        typeof data.root_pub !== 'string' || !/^[0-9a-f]{64}$/.test(data.root_pub) ||
        b64CanonLen(data.kdf.salt) !== 16 ||
        b64CanonLen(data.cipher.iv) !== 12 ||
        b64CanonLen(data.ct) !== 48) {
      throw new Error('unsupported or non-canonical armor format');
    }
    var material = await crypto.subtle.importKey(
      'raw', _te.encode(passphrase), 'PBKDF2', false, ['deriveKey']);
    var aesKey = await crypto.subtle.deriveKey(
      { name: 'PBKDF2', salt: b64ToBytes(data.kdf.salt),
        iterations: data.kdf.iterations, hash: 'SHA-256' },
      material, { name: 'AES-GCM', length: 256 }, false, ['decrypt']);
    var seed;
    try {
      seed = new Uint8Array(await crypto.subtle.decrypt(
        { name: 'AES-GCM', iv: b64ToBytes(data.cipher.iv),
          additionalData: _te.encode(ARMOR_AAD_PREFIX + data.root_pub) },
        aesKey, b64ToBytes(data.ct)));
    } catch (e) {
      throw new Error('wrong passphrase (the key blob did not open)');
    }
    if (seed.length !== 32) throw new Error('armor plaintext is not an Ed25519 seed');
    return { seed: seed, rootPub: data.root_pub };
  }

  async function _importRootKey(seed) {
    var pkcs8 = new Uint8Array(PKCS8_ED25519_PREFIX.length + seed.length);
    pkcs8.set(PKCS8_ED25519_PREFIX, 0);
    pkcs8.set(seed, PKCS8_ED25519_PREFIX.length);
    try {
      // Non-extractable even for the transient root import: the plaintext
      // seed is zeroed by the caller right after this returns (I1).
      return await crypto.subtle.importKey(
        'pkcs8', pkcs8, { name: 'Ed25519' }, false, ['sign']);
    } finally {
      pkcs8.fill(0);
    }
  }

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
      rec = await _idbOp('readonly', function (s) { return s.get(DB_KEY); });
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
      try { await _idbOp('readwrite', function (s) { return s.clear(); }); } catch (e) { /* ignore */ }
      return null;
    }
    return session;
  }

  async function _installSession(record) {
    if (!record || !record.key || record.key.extractable !== false) {
      throw new Error('refusing to install a session key that is not a ' +
        'non-extractable WebCrypto key');
    }
    if (!(record.key instanceof CryptoKey) || record.key.type !== 'private') {
      throw new Error('session key must be a private CryptoKey');
    }
    var cert = JSON.parse(record.certWire);
    var now = _nowS();
    if (typeof cert.not_before !== 'number' || typeof cert.not_after !== 'number' ||
        now < cert.not_before || now >= cert.not_after) {
      throw new Error('refusing to install a session certificate outside its ' +
        'validity window (expired or not yet valid)');
    }
    await _idbOp('readwrite', function (s) { return s.put(record, DB_KEY); });
    _state.session = {
      key: record.key, certWire: record.certWire, cert: cert, org: record.org,
      registryUrl: record.registryUrl, rootPub: record.rootPub,
      orgSlug: record.orgSlug || null, createdAt: record.createdAt,
    };
    return _state.session;
  }

  // ── ceremonies ─────────────────────────────────────────────────────

  async function _fetchJson(url) {
    var resp = await fetch(url);
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
  async function _fetchJsonOrNull(url) {
    var resp = await fetch(url);
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
    var orgKey = await _fetchJson('/api/network/org-key' + orgQ);
    if (!orgKey.armored_private_key) {
      throw new Error('no identity key is stored for this org yet — create ' +
        'one from the getting-started flow first');
    }
    var binding = await _fetchJsonOrNull('/api/network/binding' + orgQ);
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

      var now = _nowS();
      var certPayload = {
        v: 1,
        child_pub: sessionPub,
        scope: SESSION_SCOPES.slice(),
        org: orgId,
        subject: { kind: 'operator', id: _browserSubjectId() },
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
    var orgKey = await _fetchJson('/api/network/org-key' + orgQ);
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

    var resp = await fetch('/api/network/revocations', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
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
  setInterval(function () {
    if (_state.session && !_sessionLive(_state.session)) {
      signOut();
    }
  }, 30000);

  // ── init + exports ─────────────────────────────────────────────────

  _ready = _loadFromStore().then(function (session) {
    _state.session = session;
    return session;
  }).catch(function () { return null; });

  window.AutonomyNetworkSigner = {
    available: available,
    signRegistryRequest: signRegistryRequest,
  };

  window.AutonomyNetworkSession = {
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
    revokeCurrentKey: revokeCurrentKey,
    listKeys: listKeys,
    // Internals exposed for the L2.B sweep + cross-language vectors; the
    // enforcement they exercise lives in the real paths above.
    _internals: {
      canonicalJson: canonicalJson,
      decryptArmor: decryptArmor,
      installSession: _installSession,
      loadFromStore: _loadFromStore,
      hexToBytes: hexToBytes,
      bytesToHex: bytesToHex,
      domains: {
        cert: CERT_DOMAIN, request: REQUEST_DOMAIN, revocation: REVOCATION_DOMAIN,
      },
    },
  };
})();
