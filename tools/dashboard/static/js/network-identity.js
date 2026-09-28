/* auto.network identity key primitives, shared by the identity ceremonies:
 * the Get-started onboarding (network-onboarding.js), sign-on repair
 * (unlock.js), setup-org-key.html and the Worktrees inline registration.
 * Browser-only crypto: Ed25519 generation, password armor (delegated to
 * ceremony/root-factor-policy.js), non-extractable signing-key import, and
 * the root-direct registration envelope (registry §4.1).
 *
 * This file used to also hold the C1 create-org-identity wizard modal
 * (bead auto-40fob). Nothing opened it once the create-org screen
 * (create-org.js) replaced it, so it was removed (auto-5ahr1); these
 * primitives are what the other ceremonies still use.
 *
 * Invariant: a root seed exists only inside the calling ceremony; callers
 * zero it after armoring and importing the non-extractable signing key.
 *
 * Depends on network-signon.js (loaded first in base.html) for the
 * byte-exact canonical JSON, hex helpers and domain constants.
 */
(function () {
  'use strict';

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

  // ── exports ────────────────────────────────────────────────────────

  window.AutonomyNetworkIdentity = {
    // The identity ceremonies (network-onboarding.js, unlock.js,
    // setup-org-key.html, the Worktrees inline registration) use these.
    _internals: {
      generateEd25519: generateEd25519,
      armorSeed: armorSeed,
      importSigningKey: importSigningKey,
      signRegistration: signRegistration,
      overrideIterations: function (n) { _iterations = Math.max(10000, n | 0); },
      defaultIterations: DEFAULT_ITERATIONS,
    },
  };
})();
