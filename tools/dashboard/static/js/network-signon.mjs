import {
  canonicalJson,
  hexToBytes,
  bytesToHex,
  domainBytes,
  decryptArmor,
  decryptArmorAny,
  encryptArmorV2,
  importEd25519RootSigningKey,
  openSealedArmor,
} from './ceremony/primitives.js';
import { derivePersona } from './ceremony/ledger-event.js';
import { createBrowserStorage } from './ceremony/storage.js';

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
 * SIGN-ON IS A PERSONAL ACT (design §2, §1c). One passphrase opens the
 * PERSONAL root armor ONCE. From that single unlock the ceremony derives
 * one persona per organization — persona = HKDF(personal_root_seed,
 * info=genesis_id) — and each persona signs a delegation certificate over
 * ONE non-extractable WebCrypto Ed25519 session key. No organization root
 * key is decrypted at sign-on: the actor on an organization action is that
 * organization's persona (§2, "ACTOR IS ALWAYS THE PERSONA"; the personal
 * key is never bound into any action), and authority resolves by walking
 * the delegation chain to a member persona and checking the fold (§7).
 *
 * The personal seed is zeroed the moment the last persona is derived; the
 * per-org persona signing keys are dropped with it. The session key + the
 * per-org certificates live in IndexedDB; routine operations sign with the
 * session key and never see a passphrase again.
 *
 * An organization with no founded ledger has no genesis_id, therefore no
 * persona (§2: "no genesis ⇒ no persona ⇒ FOUNDING IS EAGER + ATOMIC"),
 * and is reported as skipped rather than signed on with a stand-in label.
 *
 * The opportunistic re-key interval (§1d trigger 2) is evaluated PER
 * ORGANIZATION inside this one unlock: a single sign-on can re-key several
 * organizations, and re-keys none whose interval has not elapsed.
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
 *   I1 — root plaintext exists only inside signOn(), serving provisioning,
 *        or revoke step-up; it is zeroed and dropped before those functions
 *        return. Nothing root-shaped is stored.
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

  // ONE session record, PERSONAL, carrying a map from genesis_id to that
  // organization's persona entry. Not N records, one per organization:
  // sign-on is a personal act, and per-organization session records would
  // re-create per-organization sign-on state under another name. The
  // former top-level `org` / `orgSlug` live INSIDE an entry now, because
  // the top level is no longer about one organization.
  //
  //   {key: CryptoKey, personalRootPub, createdAt,
  //    orgs: {<genesisId>: {genesisId, org, orgSlug, personaPub, certWire,
  //                         cert, registryUrl, rootPub, rekeyedAt}}}
  var _state = {
    session: null,
  };
  var _storage = null;
  var _transport = null;
  var _rekey = null;

  function _nowS() { return Math.floor(Date.now() / 1000); }

  function _certLive(cert) {
    // Both window bounds: a FUTURE not_before is as dead as an expired
    // not_after — the registry 403s either, so the chrome must never
    // claim signed-in for a cert the registry would refuse.
    if (!cert) return false;
    var now = _nowS();
    return typeof cert.not_before === 'number' &&
           typeof cert.not_after === 'number' &&
           now >= cert.not_before && now < cert.not_after;
  }

  function _orgEntries(session) {
    if (!session || !session.orgs) return [];
    return Object.keys(session.orgs).map(function (k) { return session.orgs[k]; });
  }

  // A personal session is live while at least ONE organization entry still
  // holds a cert inside its window. An expired entry alone does not end the
  // session — the other organizations' authority is untouched by it.
  function _sessionLive(session) {
    if (!session || !session.key) return false;
    return _orgEntries(session).some(function (e) { return _certLive(e.cert); });
  }

  // Resolve one organization entry. `ref` is an org slug, a genesis_id, or
  // the cert's `org` value; with no ref, a single-organization session
  // resolves implicitly and an N-organization one refuses rather than
  // guessing which organization an action belongs to.
  function _resolveOrgEntry(session, ref) {
    var entries = _orgEntries(session).filter(function (e) {
      return _certLive(e.cert);
    });
    if (!entries.length) return null;
    if (ref === undefined || ref === null || ref === '') {
      if (entries.length === 1) return entries[0];
      throw new Error('this sign-on covers ' + entries.length +
        ' organizations — name the one to act as (org slug or genesis id)');
    }
    for (var i = 0; i < entries.length; i++) {
      var e = entries[i];
      if (e.orgSlug === ref || e.genesisId === ref || e.org === ref) return e;
    }
    return null;
  }

  function _hydrateEntry(entry) {
    if (!entry || typeof entry.certWire !== 'string' ||
        typeof entry.genesisId !== 'string' ||
        typeof entry.personaPub !== 'string') {
      return null;
    }
    var cert;
    try { cert = JSON.parse(entry.certWire); } catch (e) { return null; }
    // The actor is the persona: a stored entry whose cert names anything
    // else is not this organization's persona entry and is dropped.
    if (!cert.subject || cert.subject.id !== entry.personaPub) return null;
    return {
      genesisId: entry.genesisId,
      org: entry.org,
      orgSlug: entry.orgSlug || null,
      personaPub: entry.personaPub,
      certWire: entry.certWire,
      cert: cert,
      registryUrl: entry.registryUrl || null,
      rootPub: entry.rootPub || null,
      rekeyedAt: typeof entry.rekeyedAt === 'number' ? entry.rekeyedAt : null,
    };
  }

  function _hydrateSession(rec) {
    if (!rec || !rec.key || !rec.orgs || typeof rec.orgs !== 'object') return null;
    var orgs = {};
    var keys = Object.keys(rec.orgs);
    for (var i = 0; i < keys.length; i++) {
      var entry = _hydrateEntry(rec.orgs[keys[i]]);
      if (entry) orgs[entry.genesisId] = entry;
    }
    return {
      key: rec.key,
      personalRootPub: rec.personalRootPub || null,
      createdAt: rec.createdAt,
      orgs: orgs,
    };
  }

  async function _loadFromStore() {
    var rec = null;
    try {
      rec = await _storage.getSession();
    } catch (e) {
      return null;
    }
    var session = _hydrateSession(rec);
    if (!session) return null;
    // Tested rejection: a session key that is somehow extractable, or a
    // record whose every cert is outside its validity window (expired OR
    // not yet valid), must not come back to life on load.
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
    var session = _hydrateSession(record);
    if (!session || !_orgEntries(session).length) {
      throw new Error('refusing to install a session that carries no ' +
        'organization persona');
    }
    var stale = _orgEntries(session).filter(function (e) {
      return !_certLive(e.cert);
    });
    if (stale.length) {
      throw new Error('refusing to install a session certificate outside its ' +
        'validity window (expired or not yet valid)');
    }
    await _storage.putSession(record);
    _state.session = session;
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

  // Open the org root from either armor generation, one password either
  // way. Revision-1 legacy armor decrypts directly with the entered
  // passphrase. Revision-2 (B4 Option B, what the founding ceremony
  // writes) holds the root as a seal to the owner's personal-derived
  // X25519 key — so the passphrase opens the PERSONAL armor, the derived
  // key opens the seal, and the personal seed dies before returning.
  async function _openOrgRoot(orgKey, passphrase) {
    if (orgKey.armored_private_key) {
      return decryptArmor(orgKey.armored_private_key, passphrase);
    }
    if (orgKey.sealed_root_key) {
      var personal = await _fetchJson('/api/identity/personal');
      if (!personal || !personal.armored_private_key) {
        throw new Error('this organization\'s key is sealed to your ' +
          'personal identity, but no personal identity is stored on this ' +
          'node — set one up from the getting-started flow first');
      }
      var openedPersonal = await decryptArmorAny(
        personal.armored_private_key, passphrase);
      try {
        var seed = await openSealedArmor(orgKey, openedPersonal.seed);
        return { seed: seed, rootPub: orgKey.root_pub };
      } finally {
        openedPersonal.seed.fill(0);
        openedPersonal.seed = null;
      }
    }
    throw new Error('no identity key is stored for this org yet — create ' +
      'one from the getting-started flow first');
  }

  // D13 — the certificate subject is the ACTOR: the operator's per-org
  // persona, HKDF-derived from the PERSONAL root at the moment of unlock
  // and never stored. The authority ledger's fold authorizes
  // cert.subject.id, so once an org is founded a random browser label
  // authorizes nothing. Resolution is best-effort by design: an unfounded
  // ledger, a missing personal identity, or a passphrase that opens the
  // org armor but not the personal armor all return null and sign-on
  // falls back to the legacy label subject — signing on must never
  // regress for orgs that have no ledger yet.
  // Returns { subject, reason }: subject is null on any fallback, and
  // reason names WHY — the sanctioned degrades get stable names and
  // anything else keeps its message. A swallowed distinction here is how
  // the 2026-07-30 incident started (auto-i3syn); don't reintroduce it.
  async function _resolvePersonaSubject(orgQ, org, passphrase) {
    var heads;
    try {
      heads = await _fetchJson('/api/network/ledger/heads' + orgQ, org);
    } catch (e) {
      if (e && (e.status === 404 || e.status === 409)) {
        return { subject: null, reason: 'ledger-not-founded' };
      }
      return { subject: null,
               reason: 'unexpected:' + String((e && e.message) || e) };
    }
    if (!heads || typeof heads.genesis_id !== 'string') {
      return { subject: null, reason: 'ledger-not-founded' };
    }
    var personal;
    try {
      personal = await _fetchJson('/api/identity/personal');
    } catch (e) {
      if (e && e.status === 404) {
        return { subject: null, reason: 'no-personal-identity' };
      }
      return { subject: null,
               reason: 'unexpected:' + String((e && e.message) || e) };
    }
    if (!personal || !personal.armored_private_key) {
      return { subject: null, reason: 'no-personal-identity' };
    }
    var openedPersonal = null;
    try {
      openedPersonal = await decryptArmorAny(
        personal.armored_private_key, passphrase);
    } catch (e) {
      // The entered passphrase opens the org armor but not the personal
      // armor — sign-on proceeds, publish authority will not.
      return { subject: null, reason: 'personal-armor-locked' };
    }
    try {
      var persona = await derivePersona(
        openedPersonal.seed, heads.genesis_id);
      // Subject KIND stays 'operator' on the rung-1 HTTP transport: the
      // registry's mutation gate 501s kind 'persona' ("rung-2: persona
      // subjects require viewer authn", registry/app.py) and the gwxfb
      // gate tests pin operator-kind certs carrying the persona in
      // subject.id. The kind upgrades to 'persona' when publish moves
      // onto the org tunnel (D19, auto-zudu9).
      return { subject: { kind: 'operator', id: persona.publicHex },
               reason: null };
    } catch (e) {
      return { subject: null,
               reason: 'unexpected:' + String((e && e.message) || e) };
    } finally {
      // I1: the personal seed follows the same lifecycle as the org root
      // seed — zeroed before this function returns, never stored.
      if (openedPersonal && openedPersonal.seed) {
        openedPersonal.seed.fill(0);
        openedPersonal.seed = null;
      }
    }
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

  async function _mintServeCredential(rootKey, orgUuid, personaPub) {
    // One fresh exportable serving key, certified twice for two different
    // disclosure contexts. The registry certificate names the org-scoped
    // persona; the viewer certificate names only its already-public child key.
    var delegate = await crypto.subtle.generateKey(
      { name: 'Ed25519' }, true, ['sign', 'verify']);
    var childPub = bytesToHex(new Uint8Array(
      await crypto.subtle.exportKey('raw', delegate.publicKey)));
    var pkcs8 = new Uint8Array(
      await crypto.subtle.exportKey('pkcs8', delegate.privateKey));
    var privateKeyHex = bytesToHex(pkcs8.slice(16, 48));
    pkcs8.fill(0);

    var now = _nowS();
    var commonCert = {
      v: 1,
      child_pub: childPub,
      scope: ['tunnel:serve'],
      org: orgUuid,
      not_before: now - NOT_BEFORE_SKEW_S,
      not_after: now + MAX_TTL_S,
    };
    var registryPayload = Object.assign({}, commonCert, {
      subject: { kind: 'persona', id: personaPub },
    });
    var viewerPayload = Object.assign({}, commonCert, {
      subject: { kind: 'operator', id: childPub },
    });
    var registrySig = bytesToHex(new Uint8Array(await crypto.subtle.sign(
      'Ed25519', rootKey,
      _domainBytes(CERT_DOMAIN, canonicalJson(registryPayload)))));
    var viewerSig = bytesToHex(new Uint8Array(await crypto.subtle.sign(
      'Ed25519', rootKey,
      _domainBytes(CERT_DOMAIN, canonicalJson(viewerPayload)))));
    return {
      childPub: childPub,
      notAfter: commonCert.not_after,
      body: {
        cert: canonicalJson(Object.assign({}, registryPayload, { sig: registrySig })),
        viewer_cert: canonicalJson(Object.assign({}, viewerPayload, { sig: viewerSig })),
        private_key: privateKeyHex,
      },
    };
  }

  async function _postServeCredential(credential, orgSlug) {
    var headers = orgSlug ? { 'X-Graph-Org': orgSlug } : {};
    var body = Object.assign({ org: orgSlug || null }, credential.body);
    try {
      var resp = await _transport.fetch('/api/network/serve-cert', {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, headers),
        body: JSON.stringify(body),
      });
      var result = await resp.json().catch(function () { return {}; });
      if (!resp.ok || result.ok === false) {
        throw new Error(result.error ||
          ('serve-cert provisioning was refused (' + resp.status + ')'));
      }
      return { childPub: credential.childPub, notAfter: credential.notAfter };
    } finally {
      credential.body.private_key = null; // JS strings cannot be zeroed; drop it.
      body.private_key = null;
    }
  }

  // Cheap, read-only repair decision shared by ordinary organization sign-on
  // and the dashboard password-unlock hook.  It never opens a root key.
  async function _serveCredentialRepairState(orgSlug, binding) {
    if (!(binding && binding.org_uuid && binding.root_pub &&
          binding.registry_url)) {
      return { required: false, status: 'unregistered' };
    }
    var orgQ = orgSlug ? ('?org=' + encodeURIComponent(orgSlug)) : '';
    return await _fetchJson('/api/network/serve-cert' + orgQ, orgSlug);
  }

  // The ONE unlock. Sign-on is personal, so the only armor it opens is the
  // personal root armor — no organization's key is fetched, and none is
  // decrypted. Every persona below comes out of this single seed.
  async function _openPersonalRoot(passphrase) {
    var personal;
    try {
      personal = await _fetchJson('/api/identity/personal');
    } catch (e) {
      if (e && e.status === 404) {
        throw new Error('signing on is a personal act, but no personal ' +
          'identity is stored on this node — set one up from the ' +
          'getting-started flow first');
      }
      throw e;
    }
    if (!personal || !personal.armored_private_key) {
      throw new Error('signing on is a personal act, but no personal ' +
        'identity is stored on this node — set one up from the ' +
        'getting-started flow first');
    }
    var opened = await decryptArmorAny(personal.armored_private_key, passphrase);
    return {
      seed: opened.seed,
      rootPub: opened.rootPub || personal.root_pub || null,
    };
  }

  // Which organizations this unlock covers. An explicit list (opts.orgs, or
  // the single-organization opts.org) restricts it; otherwise every
  // organization this node knows about is considered, and the ones with no
  // founded ledger fall out below.
  async function _signOnOrgSlugs(opts) {
    if (Array.isArray(opts.orgs) && opts.orgs.length) return opts.orgs.slice();
    if (opts.org) return [opts.org];
    var listing = await _fetchJson('/api/orgs');
    var rows = (listing && listing.orgs) || [];
    var slugs = [];
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i] || {};
      var slug = (row.org && row.org.slug) || row.slug;
      if (typeof slug === 'string' && slug && slugs.indexOf(slug) === -1) {
        slugs.push(slug);
      }
    }
    return slugs;
  }

  // §1d trigger 2 — OPPORTUNISTIC REFRESH, evaluated PER ORGANIZATION under
  // the one unlock. Nothing expires on a clock: this asks the organization
  // how long its interval is and how long it has been, and fires only when
  // the interval has elapsed. The interval is an organization setting (a
  // policy dial), so an organization that publishes none is not evaluated.
  //
  // Firing is delegated to the `rekey` adapter, which runs while the
  // personal root is still open — §1d's re-keys all need the root, which is
  // exactly why they belong at a login. `deriveSeed` is the adapter's only
  // access to root-derived material and stops working the moment sign-on
  // returns; the raw personal seed never leaves this module.
  async function _evaluateRekey(entry, deriveSeed) {
    var orgQ = entry.orgSlug ? ('?org=' + encodeURIComponent(entry.orgSlug)) : '';
    var policy;
    try {
      policy = await _fetchJsonOrNull(
        '/api/network/rekey-policy' + orgQ, entry.orgSlug);
    } catch (e) {
      return { evaluated: false, fired: false, reason: 'policy-unreadable' };
    }
    if (!policy || typeof policy.interval_seconds !== 'number' ||
        policy.interval_seconds <= 0) {
      return { evaluated: false, fired: false, reason: 'no-interval-configured' };
    }
    var last = typeof policy.last_rekey_at === 'number'
      ? policy.last_rekey_at : null;
    // Surface the ELAPSED time, never the configured interval (§1d): the
    // interval is agent-writable, the elapsed time is a measured fact.
    var elapsed = last === null ? null : Math.max(0, _nowS() - last);
    var due = elapsed === null || elapsed >= policy.interval_seconds;
    var decision = {
      evaluated: true, fired: false, due: due, elapsedSeconds: elapsed,
      reason: due ? 'interval-elapsed' : 'interval-not-elapsed',
    };
    if (!due) return decision;
    if (typeof _rekey !== 'function') {
      decision.reason = 'no-rekey-adapter';
      return decision;
    }
    try {
      var result = await _rekey({
        orgSlug: entry.orgSlug,
        genesisId: entry.genesisId,
        org: entry.org,
        personaPub: entry.personaPub,
        reason: 'OPPORTUNISTIC',
        deriveSeed: deriveSeed,
      });
      decision.fired = true;
      decision.result = result === undefined ? null : result;
    } catch (e) {
      // One organization's re-key failing does not un-sign-on the others,
      // and does not cost the operator a second passphrase entry.
      decision.reason = 'rekey-failed:' + String((e && e.message) || e);
    }
    return decision;
  }

  // ONE personal unlock → one persona per organization → one session record.
  //
  // The personal root opens exactly once, at the top. Each organization's
  // persona is derived from that seed with its genesis_id, signs a
  // delegation certificate over the shared non-extractable session key, and
  // is dropped. The organization root key is never fetched and never
  // decrypted (the removed `_openOrgRoot(orgKey, passphrase)` sign-on path).
  async function signOn(passphrase, opts) {
    opts = opts || {};
    var ttl = opts.ttlSeconds || DEFAULT_TTL_S;
    if (typeof ttl !== 'number' || !Number.isSafeInteger(ttl) ||
        ttl < MIN_TTL_S || ttl > MAX_TTL_S) {
      throw new Error('session TTL must be between 1 minute and 30 days');
    }

    var slugs = await _signOnOrgSlugs(opts);
    var opened = await _openPersonalRoot(passphrase);
    var sessionKeys = await crypto.subtle.generateKey(
      { name: 'Ed25519' }, false, ['sign', 'verify']);
    var sessionPub = bytesToHex(
      await crypto.subtle.exportKey('raw', sessionKeys.publicKey));

    var orgs = {};
    var reports = [];
    var skipped = [];
    var rekeys = {};
    var seedDropped = false;
    try {
      // Bound to the live seed, and only to it: the closure stops answering
      // as soon as the finally below zeroes the buffer.
      var deriveSeed = async function (info) {
        if (seedDropped || !opened.seed) {
          throw new Error('the personal root is closed — derive during sign-on');
        }
        var persona = await derivePersona(opened.seed, info);
        return persona;
      };

      for (var i = 0; i < slugs.length; i++) {
        var slug = slugs[i];
        var slugQ = '?org=' + encodeURIComponent(slug);
        var heads = null;
        try {
          heads = await _fetchJsonOrNull('/api/network/ledger/heads' + slugQ, slug);
        } catch (e) {
          skipped.push({ orgSlug: slug, reason: 'ledger-unreadable' });
          continue;
        }
        // §2: no genesis ⇒ no persona. An unfounded organization is
        // reported, never signed on under a stand-in browser label.
        if (!heads || typeof heads.genesis_id !== 'string') {
          skipped.push({ orgSlug: slug, reason: 'ledger-not-founded' });
          continue;
        }
        var genesisId = heads.genesis_id;
        if (orgs[genesisId]) continue;

        var persona = await derivePersona(opened.seed, genesisId);
        var binding = await _fetchJsonOrNull('/api/network/binding' + slugQ, slug);
        var bound = !!(binding && binding.org_uuid && binding.root_pub &&
                       binding.registry_url);
        // The registry binding is OPTIONAL — a sovereign organization that
        // never registered with a broker still signs on. Bound, its coordinates
        // pin the entry; unbound, the genesis_id is the organization's name.
        var orgId = bound ? binding.org_uuid : genesisId;

        var now = _nowS();
        var certPayload = {
          v: 1,
          child_pub: sessionPub,
          scope: SESSION_SCOPES.slice(),
          org: orgId,
          // The ACTOR: this organization's persona public key. Subject KIND
          // stays 'operator' — the settled rung-1 representation the fold
          // gate authorizes (registry/app.py 501s kind 'persona'); the
          // persona rides in subject.id. It upgrades to 'persona' when
          // publish moves onto the org tunnel (D19, auto-zudu9).
          subject: { kind: 'operator', id: persona.publicHex },
          not_before: now - NOT_BEFORE_SKEW_S,
          not_after: now + ttl,
        };
        var sigBytes = await crypto.subtle.sign(
          'Ed25519', persona.signingKey,
          _domainBytes(CERT_DOMAIN, canonicalJson(certPayload)));
        var certWire = canonicalJson(
          Object.assign({}, certPayload, { sig: bytesToHex(sigBytes) }));
        persona.signingKey = null;

        var entry = {
          genesisId: genesisId,
          org: orgId,
          orgSlug: slug,
          personaPub: persona.publicHex,
          certWire: certWire,
          registryUrl: bound ? binding.registry_url : null,
          rootPub: bound ? binding.root_pub : null,
          rekeyedAt: null,
        };
        var rekey = await _evaluateRekey(entry, deriveSeed);
        if (rekey.fired) entry.rekeyedAt = _nowS();
        orgs[genesisId] = entry;
        rekeys[genesisId] = rekey;
        reports.push({
          orgSlug: slug, genesisId: genesisId, org: orgId,
          personaPub: persona.publicHex, notAfter: certPayload.not_after,
          certWire: certWire, registryUrl: entry.registryUrl, rekey: rekey,
        });
      }
    } finally {
      // I1: the personal root plaintext dies here, whatever happened above.
      seedDropped = true;
      if (opened.seed) { opened.seed.fill(0); opened.seed = null; }
    }

    if (!reports.length) {
      throw new Error('this personal identity belongs to no organization ' +
        'with a founded ledger — found or join one first');
    }

    var session = await _installSession({
      key: sessionKeys.privateKey,
      personalRootPub: opened.rootPub,
      createdAt: _nowS(),
      orgs: orgs,
    });
    return {
      sessionPub: sessionPub,
      personalRootPub: session.personalRootPub,
      orgs: reports,
      skipped: skipped,
      diagnostics: {
        personalRootDropped: seedDropped,
        extractable: sessionKeys.privateKey.extractable,
        orgRootsOpened: 0,
        personaCount: reports.length,
        rekeyedOrgs: reports.filter(function (r) { return r.rekey.fired; })
          .map(function (r) { return r.orgSlug; }),
      },
    };
  }

  // Explicit serving-credential provisioning seam used by focused tests,
  // recovery tooling, and repairServeCredential(). Normal production repair
  // is reached from both organization sign-on and password-backed dashboard
  // unlock. Each first performs the cheap status check and signs a replacement
  // only when required.
  //
  // The delegate is ROOT-signed: a 30-day serving TTL cannot nest inside the
  // 24h session cert, so a session-key sub-delegate will not do. It signs the
  // SAME properties idkit's issue_cert does — {v, child_pub,
  // scope:['tunnel:serve'], org, subject, not_before, not_after} over
  // CERT_DOMAIN — with the org ROOT, decrypted from the armor with the same
  // approve password and zeroed the instant it is imported (I1).
  //
  // The signed subject is the current organization-scoped persona.  Serving
  // has no legacy browser-label fallback: without the personal armor and the
  // immutable ledger genesis there is no safe routing identity to mint.
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
    if (!orgKey.armored_private_key && !orgKey.sealed_root_key) {
      throw new Error('this org has no signing key to mint a serving delegate');
    }

    var personaResolution = await _resolvePersonaSubject(
      orgQ, orgSlug, passphrase);
    if (!personaResolution.subject) {
      throw new Error('serving persona could not be resolved: ' +
        personaResolution.reason);
    }
    var personaPub = personaResolution.subject.id;
    if (typeof personaPub !== 'string' ||
        !/^[0-9a-f]{64}$/.test(personaPub)) {
      throw new Error('serving persona is not a canonical public key');
    }

    var opened = await _openOrgRoot(orgKey, passphrase);
    try {
      var rootKey = await _importRootKey(opened.seed);
      opened.seed.fill(0); opened.seed = null;   // I1: root seed gone at import

      var credential = await _mintServeCredential(rootKey, orgUuid, personaPub);
      rootKey = null;                        // I1: last root reference dropped
      return await _postServeCredential(credential, orgSlug);
    } finally {
      if (opened && opened.seed) { opened.seed.fill(0); opened.seed = null; }
    }
  }

  // Opportunistic maintenance after an ordinary password-backed dashboard
  // unlock.  The common path performs only two local reads (binding + status)
  // and returns.  Root decryption and signing happen only when the stored
  // serving credential is missing, expired, or has the obsolete schema.
  async function repairServeCredential(passphrase, opts) {
    opts = opts || {};
    var orgSlug = opts.org || null;
    var orgQ = orgSlug ? ('?org=' + encodeURIComponent(orgSlug)) : '';
    var binding = await _fetchJsonOrNull(
      '/api/network/binding' + orgQ, orgSlug);
    var state = await _serveCredentialRepairState(orgSlug, binding);
    if (!state.required) {
      return {
        checked: true,
        repaired: false,
        status: state.status || 'ready',
      };
    }
    await provisionServeCert(passphrase, {
      org: orgSlug,
      orgUuid: binding.org_uuid,
    });
    return { checked: true, repaired: true, status: state.status || 'required' };
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
  // The organization to revoke in is named explicitly (opts.org), because
  // one personal sign-on now carries authority in several organizations and
  // a revocation belongs to exactly one of them.
  //
  // Step-up, and PERSONAL: a persona provisions and expires its own
  // delegates (§7), so revoking this session key re-opens the personal
  // armor and re-derives that organization's persona rather than opening
  // the organization root. The persona is the key that issued the
  // certificate being revoked, so it is the key entitled to revoke it.
  async function revokeCurrentKey(passphrase, reason, opts) {
    opts = opts || {};
    var session = _state.session;
    if (!session) throw new Error('no session key to revoke');
    if (!_sessionLive(session)) {
      // Natural expiry already ended this key's life (I7); local cleanup
      // is all that is left to do.
      await signOut();
      return { revoked: false, expired: true };
    }
    var entry = _resolveOrgEntry(session, opts.org);
    if (!entry) throw new Error('no live session authority for that organization');
    var opened = await _openPersonalRoot(passphrase);
    var recordWire;
    try {
      var persona = await derivePersona(opened.seed, entry.genesisId);
      if (persona.publicHex !== entry.personaPub) {
        throw new Error('that passphrase derives a different persona for ' +
          'this organization — refusing to revoke');
      }
      var payload = {
        v: 1,
        revoked_key_id: entry.cert.child_pub,
        org: entry.org,
        revoked_at: _nowS(),
        expires_at: entry.cert.not_after,   // I7: bounded by natural expiry
        issuer_pub: persona.publicHex,
      };
      if (reason) payload.reason = String(reason).slice(0, 512);
      var sigBytes = await crypto.subtle.sign(
        'Ed25519', persona.signingKey,
        _domainBytes(REVOCATION_DOMAIN, canonicalJson(payload)));
      persona.signingKey = null;
      recordWire = canonicalJson(Object.assign({}, payload, { sig: bytesToHex(sigBytes) }));
    } finally {
      if (opened.seed) { opened.seed.fill(0); opened.seed = null; }
    }

    var revHeaders = { 'Content-Type': 'application/json' };
    if (entry.orgSlug) revHeaders['X-Graph-Org'] = entry.orgSlug;
    var resp = await fetch('/api/network/revocations', {
      method: 'POST', headers: revHeaders,
      body: JSON.stringify({
        org: entry.orgSlug, record: recordWire, revoked_cert: entry.certWire,
      }),
    });
    var body = await resp.json().catch(function () { return {}; });
    if (!resp.ok || body.ok === false) {
      throw new Error(body.error || 'the registry refused the revocation');
    }
    await signOut();
    return { revoked: true, revoked_key_id: body.revoked_key_id };
  }

  // One row per organization the unlock covers: the same session key,
  // acting as a different persona in each.
  function listKeys() {
    var s = _state.session;
    if (!s) return [];
    return _orgEntries(s).map(function (e) {
      return {
        key_id: e.cert.child_pub,
        subject: e.cert.subject,
        scope: e.cert.scope,
        not_after: e.cert.not_after,
        org: e.org,
        org_slug: e.orgSlug,
        genesis_id: e.genesisId,
        persona_pub: e.personaPub,
        this_browser: true,
      };
    });
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
    // Optional: the §1d re-key executor. Sign-on always EVALUATES the
    // opportunistic interval per organization; without an adapter it
    // reports the decision and fires nothing (auto-biqme supplies one).
    var rekey = adapters && adapters.rekey;
    if (rekey !== undefined && rekey !== null && typeof rekey !== 'function') {
      throw new Error('rekey adapter must be a function when provided');
    }

    _storage = storage;
    _transport = transport;
    _rekey = rekey || null;
    _ready = _loadFromStore().then(function (session) {
      _state.session = session;
      return session;
    }).catch(function () { return null; });
    return _ready;
  }

  function available() {
    return _sessionLive(_state.session);
  }

  // `opts.org` names which organization to act in — an org slug, genesis id
  // or the cert's org value. A single-organization sign-on resolves without
  // it; an N-organization one refuses to guess.
  async function signRegistryRequest(method, path, payload, opts) {
    await _ready;
    var session = _state.session;
    if (!_sessionLive(session)) {
      throw new Error('no live operator session key — sign on to auto.network first');
    }
    if (typeof method !== 'string' || typeof path !== 'string') {
      throw new Error('signRegistryRequest needs (method, path, payload)');
    }
    var entry = _resolveOrgEntry(session, opts && opts.org);
    if (!entry) {
      throw new Error('no live session authority for that organization — ' +
        'sign on again');
    }
    var ts = _nowS();
    var signer = entry.cert.child_pub;
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
      cert: entry.certWire, sig: sig,
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
    // Personal state, with one row per organization. There is no top-level
    // `org` any more: the top level of a sign-on is a person, not an
    // organization. Consumers matching an action to authority read `orgs`.
    state: function () {
      var s = _state.session;
      if (!s) return { signedIn: false, orgs: [] };
      var entries = _orgEntries(s);
      return {
        signedIn: available(),
        sessionPub: entries.length ? entries[0].cert.child_pub : null,
        personalRootPub: s.personalRootPub,
        orgs: entries.map(function (e) {
          return {
            org: e.org, orgSlug: e.orgSlug, genesisId: e.genesisId,
            personaPub: e.personaPub, subject: e.cert.subject,
            notAfter: e.cert.not_after, live: _certLive(e.cert),
          };
        }),
      };
    },
    signOn: signOn,
    signOut: signOut,
    provisionServeCert: provisionServeCert,
    repairServeCredential: repairServeCredential,
    revokeCurrentKey: revokeCurrentKey,
    listKeys: listKeys,
    // Internals exposed for the L2.B sweep + cross-language vectors; the
    // enforcement they exercise lives in the real paths above.
    _internals: {
      canonicalJson: canonicalJson,
      decryptArmor: decryptArmor,
      decryptArmorAny: decryptArmorAny,
      encryptArmorV2: encryptArmorV2,
      openOrgRoot: _openOrgRoot,
      derivePersona: derivePersona,
      resolveOrgEntry: function (ref) {
        return _resolveOrgEntry(_state.session, ref);
      },
      provisionServeCert: provisionServeCert,
      repairServeCredential: repairServeCredential,
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
    configure({
      storage: createBrowserStorage(),
      transport: {
        fetch: function (url, options) {
          return window.fetch(url, options);
        },
      },
    });
  }
})();

export {
  configureCore as configure,
  signOnCore as signOn,
  signRegistryRequestCore as signRegistryRequest,
};
