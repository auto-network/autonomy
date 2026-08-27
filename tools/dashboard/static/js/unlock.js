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
    // MFA (combined factor): the root opens only with BOTH password AND a
    // passkey PRF together. Detected from require_pair, confirmed against the
    // armor's factors at ceremony time.
    mfa: false,
    rpId: null,
    passkeys: [],
    // Reach: does the current method OPEN THE ROOT (release signing material)
    // or only grant dashboard access? Derived from the armor at init.
    passkeyOpensRoot: false,
    pwOpensRoot: false,
    armorText: null,
    factorPolicy: null,
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
    var target = _nextPath();
    try {
      if (sessionStorage.getItem('autonomy.factor.pending-slot')
          || sessionStorage.getItem('autonomy.factor.slot-enrolled')
          || sessionStorage.getItem('autonomy.factor.open-credentials')) {
        // A first-device enrollment greeting is pending. Its dialog lives in
        // the shell's profile drawer, which immersive surfaces (sessions,
        // Mission Control, …) do not render — so suppress the next-redirect
        // and land on the shell home, where the drawer auto-opens into it.
        target = '/';
      }
    } catch (e) { /* storage unavailable — normal redirect */ }
    location.replace(target);
  }

  function _esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;',
               '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // ── ceremonies ─────────────────────────────────────────────────────

  // Complete a pending Fleet join, else mint the Fleet runtime (+reachability)
  // credential. Shared by BOTH root-releasing unlock paths — password AND
  // passkey — so they can never diverge; that divergence is exactly what left a
  // passkey-only unlock unable to activate fleet sync. Best-effort; the caller
  // owns the seed's outer lifecycle (the ceremonies zero the array passed here).
  async function _fleetCompleteOrMint(seed) {
    var completion = await _fetchJson('/api/fleet/enrollment/local-completion');
    if (completion.pending) {
      var fc = await import('./ceremony/fleet-enrollment.js');
      var proof = await fc.completeFleetEnrollment({
        personalRootSeed: seed,
        requestId: completion.request_id,
        request: completion.request,
        channelBinding: completion.channel_binding,
        approval: completion.approval,
        rosterEntry: completion.roster_entry,
      });
      await _postJson('/api/fleet/enrollment/local-completion', proof);
    } else {
      var rc = await _fetchJson('/api/fleet/runtime');
      if (rc.enabled) {
        // Bring the PERSONAL TUNNEL online before minting the runtime
        // credential. When the personal org is not yet registered (org_uuid is
        // null), register it — and, on the serving machine, provision its
        // serving delegate — using the personal root that is already open. This
        // is what gives a virgin system with zero collaborative orgs a tunnel
        // to serve the fleet on: its own. Idempotent and best-effort — a
        // failure here degrades to a sync-only credential, never a lockout.
        // Run when the personal org isn't registered yet OR this machine is the
        // tunnel server: provisionPersonalNetworkIdentity is idempotent (the
        // registry returns the UUID for a same-root re-register, and the
        // serve-cert is minted only when one isn't already provisioned), so a
        // serving machine re-tries the serve-cert every unlock until it exists —
        // gating on `!org_uuid` alone would permanently skip a serve-cert that
        // failed on the registration unlock (registration lands, serving does
        // not, and org_uuid is now set forever).
        if (rc.personal_org_uuid && (!rc.org_uuid || rc.serves)) {
          try {
            await _signonI().provisionPersonalNetworkIdentity({
              personalRootSeed: new Uint8Array(seed),   // ceremony zeroes its copy
              orgUuid: rc.personal_org_uuid,
              rootPub: rc.personal_root_pub,
              serve: !!rc.serves,
            });
            // The binding now exists; re-read so the runtime credential carries
            // the reachability material minted under the registered org_uuid.
            rc = await _fetchJson('/api/fleet/runtime');
          } catch (e) {
            if (window.console && console.warn) {
              console.warn('personal tunnel provisioning failed:',
                           (e && e.message) || e);
            }
          }
        }
        var frc = await import('./ceremony/fleet-enrollment.js');
        var cred = await frc.mintFleetRuntimeCredential({
          personalRootSeed: new Uint8Array(seed),   // fresh copy; mint zeroes it
          rootPub: rc.personal_root_pub,
          machineId: rc.machine_id,
          machinePub: rc.machine_pub,
          // When the personal org is registered, the credential also carries the
          // machine key + a node-scoped reachability cert for discovery; a null
          // org_uuid keeps it sync-only.
          orgUuid: rc.org_uuid || null,
        });
        await _postJson('/api/fleet/runtime', cred);
      }
      seed.fill(0);   // original consumed only via fresh copies above; drop it
    }
  }

  async function _unlockWithPasskey() {
    if (!window.PublicKeyCredential || !navigator.credentials) {
      throw new Error('this browser does not support passkeys — use your password instead');
    }
    var minted = await _postJson('/api/identity/unlock/passkey/options', {});
    var pk = minted.options;
    var challengeText = pk.challenge;
    pk.challenge = b64uToBytes(pk.challenge);
    (pk.allowCredentials || []).forEach(function (c) { c.id = b64uToBytes(c.id); });
    // Ask for the PRF eval in the SAME gesture: when this passkey is a root
    // factor, its PRF output re-derives the key that opens the armor, so one
    // Face ID both proves access AND releases the root — no second prompt.
    var enroll = null;
    try {
      enroll = await import('./ceremony/enrollment.js');
      pk.extensions = Object.assign({}, pk.extensions, enroll.prfEvalExtension());
    } catch (e) { /* PRF is an optional enhancement; access unlock proceeds */ }
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
    var credentialPayload = {
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
    };

    // A v3 passkey that opens the root signs the SAME WebAuthn ceremony before
    // the server grants access. This keeps dashboard access and root authority
    // genuinely independent: an access-disabled passkey succeeds only when its
    // PRF actually opened the current root policy.
    //
    // This block runs for EVERY v3 passkey login — not only when some passkey
    // already holds root authority — because it is also the DETECTION for the
    // first-device re-enrollment flow: a known credential whose PRF matches no
    // enrolled slot stashes a pending slot, and the factor panel greets it
    // with the enrollment dialog. Gating this on passkeyOpensRoot made the
    // designed flow unreachable in exactly its primary scenario (a migrated
    // or newly synced device whose factor holds no authority yet).
    var openedForWarm = null;
    var rootSignature = null;
    if (enroll && U.armorText
        && U.factorPolicy && U.factorPolicy.armor_version === 3) {
      var rootPrf = null;
      try {
        rootPrf = enroll.prfOutputFromResults(
          (cred.getClientExtensionResults && cred.getClientExtensionResults()) || {});
        if (rootPrf) {
          var rootPrimitives = await import('./ceremony/primitives.js');
          var rootPolicy = await import('./ceremony/root-factor-policy.js');
          var rootEnvelope = await rootPolicy.parseFactorPolicyArmor(U.armorText);
          var rootRecipient = await rootPrimitives.deriveEncapsulationKeypair(
            rootPrf, rootPolicy.FACTOR_RECIPIENT_PURPOSE,
          );
          var credentialId = bytesToB64u(new Uint8Array(cred.rawId));
          var detection = enroll.detectPendingSlot(
            rootEnvelope.factors, credentialId, rootRecipient.publicKeyHex,
          );
          var rootFactor = detection.kind === 'enrolled' ? detection.factor : null;
          // PRF mismatch on a KNOWN credential: this device holds a synced
          // passkey whose slot lives elsewhere. Remember the derived recipient
          // (public data only) — the factor panel greets it with the
          // first-device enrollment dialog, which restores full authority.
          if (detection.kind === 'pending-slot') {
            try {
              sessionStorage.setItem('autonomy.factor.pending-slot',
                JSON.stringify(detection.pending));
            } catch (e) { /* storage unavailable — detection stays best-effort */ }
          }
          if (rootFactor && rootPolicy.policySatisfied(
            rootEnvelope.policy, [rootFactor.factor_id],
          )) {
            var rootSeeds = {}; rootSeeds[rootFactor.factor_id] = rootPrf;
            try {
              openedForWarm = await rootPolicy.openFactorPolicyArmor(
                U.armorText, rootSeeds,
              );
            } finally { rootPrf.fill(0); }
            var rootMessage = new TextEncoder().encode(
              UNLOCK_DOMAIN + _signonI().canonicalJson({
                v: 1, challenge: challengeText, origin: minted.origin,
              }));
            rootSignature = _signonI().bytesToHex(await crypto.subtle.sign(
              'Ed25519', openedForWarm.signingKey, rootMessage,
            ));
          } else {
            rootPrf.fill(0);
          }
        }
      } catch (e) {
        if (openedForWarm && openedForWarm.seed) openedForWarm.seed.fill(0);
        openedForWarm = null;
        if (window.console && console.warn) {
          console.warn('passkey root proof unavailable:', (e && e.message) || e);
        }
      } finally {
        if (rootPrf) rootPrf.fill(0);
      }
    }

    var passkeyBody = { credential: credentialPayload };
    if (rootSignature) passkeyBody.root_signature = rootSignature;
    try {
      await _postJson('/api/identity/unlock/passkey', passkeyBody);
    } catch (e) {
      if (openedForWarm && openedForWarm.seed) openedForWarm.seed.fill(0);
      throw e;
    }

    // Access is granted. If this passkey is a ROOT factor, use the PRF output
    // from the same gesture to open the armor and warm the vault — this is what
    // makes "Face ID opens your keys" true after a promotion. Strictly
    // best-effort: a failure here never turns a successful access unlock into a
    // lockout, and never runs for an access-only passkey or an MFA identity
    // (where the passkey alone cannot reach the root).
    if (U.passkeyOpensRoot && enroll && U.armorText) {
      try {
        var opened = openedForWarm;
        if (U.factorPolicy && U.factorPolicy.armor_version === 3) {
          if (!opened) {
            throw new Error('this passkey grants access but does not open the root alone');
          }
        } else {
          var prf = enroll.prfOutputFromResults(
            (cred.getClientExtensionResults && cred.getClientExtensionResults()) || {});
          if (!prf) throw new Error('this passkey supplied no PRF root material');
          var migRecipient = null;
          try {
            var Pp = await import('./ceremony/primitives.js');
            opened = await Pp.decryptArmorWithPasskey(U.armorText, prf);
            // For the one-shot v2→v3 upgrade below: this device's v3
            // root-recipient key, derived while the PRF is still live.
            try {
              var Rm = await import('./ceremony/root-factor-policy.js');
              migRecipient = {
                credentialId: bytesToB64u(cred.rawId),
                publicKeyHex: (await Pp.deriveEncapsulationKeypair(
                  prf, Rm.FACTOR_RECIPIENT_PURPOSE,
                )).publicKeyHex,
              };
            } catch (e) { migRecipient = null; }
          } finally { prf.fill(0); }
        }
        var rootSeed = new Uint8Array(opened.seed);
        opened.seed.fill(0);
        // best-effort: a root-opening passkey login can also enroll a pending
        // device slot stashed for ANOTHER of this browser's credentials
        if (U.factorPolicy && U.factorPolicy.armor_version === 3) {
          try { await _completePendingSlot(rootSeed); }
          catch (e) {
            if (window.console && console.warn) {
              console.warn('pending device slot not enrolled:', (e && e.message) || e);
            }
          }
        }
        // ONE-SHOT v2→v3 armor upgrade (see _migrateArmorV2Once). Best-effort.
        try {
          await _migrateArmorV2Once({ rootSeed: rootSeed, passkeyRecipient: migRecipient });
        } catch (e) {
          if (window.console && console.warn) {
            console.warn('one-shot armor upgrade failed (v2 stays):', (e && e.message) || e);
          }
        }
        try {
          // Warm the vault, then mint the Fleet runtime + reachability
          // credential — the SAME root release the password path runs (shared
          // via _fleetCompleteOrMint), so a passkey-only unlock activates fleet
          // sync + discovery too. Fresh copies: each ceremony zeroes its own.
          try {
            await _signonI().wakeVault({ personalRootSeed: new Uint8Array(rootSeed) });
          } catch (e) {
            if (window.console && console.warn) console.warn('vault wake failed:', (e && e.message) || e);
          }
          try {
            await _fleetCompleteOrMint(new Uint8Array(rootSeed));
          } catch (e) {
            if (window.console && console.warn) {
              console.warn('fleet runtime after passkey unlock failed:', (e && e.message) || e);
            }
            if (U.fleetRootRequired) throw e;
          }
        } finally {
          rootSeed.fill(0);
        }
      } catch (e) {
        if (window.console && console.warn) {
          console.warn('passkey root release failed:', (e && e.message) || e);
        }
      }
    }
  }

  // A WebAuthn PRF assertion on an enrolled passkey, for the passkey half of a
  // combined (MFA) unlock. Reuses the enrollment ceremony's fixed PRF salt so
  // the derived key matches what promotion published. Returns the 32-byte PRF
  // output, never sent anywhere — it only re-derives the armor-opening key.
  async function _prfAssert() {
    if (!window.PublicKeyCredential || !navigator.credentials) {
      throw new Error('this browser cannot use Face ID — open your dashboard where you enrolled it');
    }
    var enroll = await import('./ceremony/enrollment.js');
    var allow = (U.passkeys || [])
      .filter(function (p) { return p.credential_id && (!U.rpId || p.rp_id === U.rpId); })
      .map(function (p) { return { type: 'public-key', id: b64uToBytes(p.credential_id) }; });
    var asrt;
    try {
      asrt = await navigator.credentials.get({ publicKey: {
        challenge: crypto.getRandomValues(new Uint8Array(32)),
        rpId: U.rpId || undefined,
        allowCredentials: allow,
        userVerification: 'required',
        extensions: enroll.prfEvalExtension(),
      } });
    } catch (e) {
      if (e && e.name === 'NotAllowedError') {
        throw new Error('Face ID was cancelled or timed out — try again');
      }
      throw e;
    }
    if (!asrt) throw new Error('Face ID was cancelled — try again');
    var prf = enroll.prfOutputFromResults(asrt.getClientExtensionResults());
    if (!prf) {
      throw new Error('this passkey cannot unlock your key (no PRF) — use a device enrolled for it');
    }
    var rawId = new Uint8Array(asrt.rawId || allow[0].id);
    return { prf: prf, credentialId: bytesToB64u(rawId) };
  }

  // The plaintext seed exists only inside this function — zeroed the
  // moment the signing key is imported (I1).
  // ═══ ONE-SHOT v2→v3 ARMOR UPGRADE — DELETE THIS FUNCTION (and its two call
  // sites) AFTER THE OPERATOR'S FIRST POST-DEPLOY LOGIN. ═══════════════════
  //
  // Runs only while the stored armor is still v2 (migration_required). It
  // migrates ONLY the factor material live at this login: the typed password
  // re-derives its v3 factor under its legacy factor id (so metadata labels
  // survive); the passkey used to sign in (if any) gets THIS device's v3
  // root-recipient, derived under FACTOR_RECIPIENT_PURPOSE — the legacy
  // vault-purpose kem_pub must never be carried into a v3 recipient (different
  // HKDF purpose ⇒ a key no device could ever re-derive ⇒ permanent lockout).
  // Factors with no live material keep dashboard access but leave the root
  // policy; they re-gain authority per device via "Enroll this device" in
  // Manage credentials. Knowingly not a general migration (operator ruling
  // 2026-08-27: sole pre-deployment identity, delete after use).
  async function _migrateArmorV2Once(material) {
    var fp = U.factorPolicy;
    if (!fp || fp.armor_version === 3 || !fp.migration_required) return;
    var R = await import('./ceremony/root-factor-policy.js');
    var M = await import('./ceremony/armor-migration.js');
    // Armor wraps whose credentials have no dashboard registration row are
    // dead weight (cannot sign in) and the server's binding check refuses a
    // v3 factor list naming them — the builder drops them.
    var registered = [];
    try {
      var st = await _fetchJson('/api/identity/status');
      registered = (st.passkeys || []).map(function (p) { return p.credential_id; })
        .filter(function (c) { return typeof c === 'string'; });
    } catch (e) { registered = null; }
    var operations = await M.buildMigrationOperations(fp, {
      password: material.password,
      passkeyRecipient: material.passkeyRecipient,
      registeredCredentialIds: registered,
    });
    if (!operations) return;   // nothing root-capable in hand — leave v2 intact
    var pv = await _postJson('/api/identity/factor-policy/preview', {
      base_generation: 0, operations: operations,
    });
    var armor = await R.buildFactorPolicyArmor({
      rootSeed: material.rootSeed, rootPub: fp.root_pub, generation: pv.generation,
      factors: pv.factors, access: pv.access, policy: pv.root_policy,
    });
    var seedCopy = new Uint8Array(material.rootSeed);
    var signingKey;
    try { signingKey = await _identityI().importSigningKey(seedCopy); }
    finally { seedCopy.fill(0); }
    var signature = await R.signFactorPolicyTransition({
      signingKey: signingKey, baseGeneration: 0,
      operations: operations, candidateArmor: armor,
    });
    await _postJson('/api/identity/factor-policy/commit', {
      base_generation: 0, operations: operations,
      candidate_armor: armor, root_signature: signature,
    });
    U.factorPolicy = null;
    try { U.factorPolicy = await _fetchJson('/api/identity/factor-policy'); }
    catch (e) { U.factorPolicy = null; }
    if (window.console && console.info) {
      console.info('armor upgraded to v3 (one-shot migration): generation '
        + pv.generation);
    }
  }

  // A synced passkey signed in earlier from a device whose PRF slot is not
  // enrolled (detected + stashed above, public data only). The armor's
  // recipient set is root-sealed, so the slot can only be written under a root
  // proof — do it silently the moment a login opens the root. The factor panel
  // then greets the arrival with the name-this-device dialog.
  async function _completePendingSlot(rootSeed) {
    var raw = null;
    try { raw = sessionStorage.getItem('autonomy.factor.pending-slot'); } catch (e) { return; }
    if (!raw) return;
    var pending = JSON.parse(raw);
    var R = await import('./ceremony/root-factor-policy.js');
    var fp = await _fetchJson('/api/identity/factor-policy');
    var factor = (fp.factors || []).find(function (f) {
      return f.type === 'passkey' && f.credential_id === pending.credential_id;
    });
    if (!factor) { sessionStorage.removeItem('autonomy.factor.pending-slot'); return; }
    var already = (factor.recipients || []).some(function (s) {
      return s.recipient_public_key === pending.recipient_public_key;
    });
    if (!already) {
      var operations = [{
        op: 'add_passkey_recipient',
        factor_id: factor.factor_id,
        recipient: {
          recipient_public_key: pending.recipient_public_key,
          label: pending.label || 'New device',
          created_at: new Date().toISOString().replace(/\.\d+Z$/, 'Z'),
        },
      }];
      // Re-enrollment RESTORES the factor's authority, not just its slot: the
      // policy is still "this factor holds full authority" in intent (it was a
      // root member before its material was lost/migrated), so the same
      // generation grants it back.
      var typeOf = function (fid) {
        var row = (fp.factors || []).find(function (x) { return x.factor_id === fid; });
        return row ? row.type : null;
      };
      var granted = R.policyWithFactorGranted(fp.root_policy, factor.factor_id, typeOf);
      if (JSON.stringify(granted) !== JSON.stringify(R.canonicalExpression(fp.root_policy))) {
        operations.push({ op: 'set_root_policy', policy: granted });
      }
      var pv = await _postJson('/api/identity/factor-policy/preview', {
        base_generation: fp.generation, operations: operations,
      });
      var armor = await R.buildFactorPolicyArmor({
        rootSeed: rootSeed, rootPub: fp.root_pub, generation: pv.generation,
        factors: pv.factors, access: pv.access, policy: pv.root_policy,
      });
      var seedCopy = new Uint8Array(rootSeed);
      var signingKey;
      try { signingKey = await _identityI().importSigningKey(seedCopy); }
      finally { seedCopy.fill(0); }
      var signature = await R.signFactorPolicyTransition({
        signingKey: signingKey, baseGeneration: fp.generation,
        operations: operations, candidateArmor: armor,
      });
      await _postJson('/api/identity/factor-policy/commit', {
        base_generation: fp.generation, operations: operations,
        candidate_armor: armor, root_signature: signature,
      });
    }
    sessionStorage.removeItem('autonomy.factor.pending-slot');
    sessionStorage.setItem('autonomy.factor.slot-enrolled', JSON.stringify({
      factor_id: factor.factor_id,
      recipient_public_key: pending.recipient_public_key,
      label: pending.label || 'New device',
    }));
  }

  async function _unlockWithPassword(password) {
    if (!password) throw new Error('enter your password');
    var S = _signonI();
    var I = _identityI();
    var stored = await _fetchJson('/api/identity/personal');
    // Version 3 normalizes every password to a public recipient plus a
    // password-derived access signing key. Try the named password factors
    // locally. An individually-full factor opens and warms the root; an
    // access-enabled MFA member may sign in without releasing the root.
    if (U.factorPolicy && U.factorPolicy.armor_version === 3) {
      var R = await import('./ceremony/root-factor-policy.js');
      var envelope = await R.parseFactorPolicyArmor(stored.armored_private_key);
      var passwordFactor = null;
      var factorSeed = null;
      for (var candidate of envelope.factors.filter(function (f) { return f.type === 'password'; })) {
        try {
          factorSeed = await R.openPasswordFactor(envelope.root_pub, candidate, password);
          passwordFactor = candidate; break;
        } catch (e) { /* this password may name another enrolled factor */ }
      }
      if (!passwordFactor || !factorSeed) {
        throw new Error('that password does not open any enrolled password factor');
      }
      var individuallyFull = R.policySatisfied(
        envelope.policy, [passwordFactor.factor_id],
      );
      if (!individuallyFull && envelope.access.includes(passwordFactor.factor_id)) {
        try {
          var accessKey = await R.importFactorAccessSigningKey(factorSeed);
          var accessMinted = await _postJson('/api/identity/unlock/password/options', {});
          var accessMessage = new TextEncoder().encode(
            'autonomy.identity.factor-access-unlock.v1\n' + _signonI().canonicalJson({
              v: 1,
              challenge: accessMinted.challenge,
              factor_id: passwordFactor.factor_id,
              origin: accessMinted.origin,
            }));
          var accessSignature = _signonI().bytesToHex(await crypto.subtle.sign(
            'Ed25519', accessKey, accessMessage,
          ));
          await _postJson('/api/identity/unlock/password', {
            challenge: accessMinted.challenge,
            factor_id: passwordFactor.factor_id,
            access_signature: accessSignature,
          });
          return;
        } finally { factorSeed.fill(0); }
      }

      var rootMember = R.policyFactorIds(envelope.policy)
        .includes(passwordFactor.factor_id);
      if (!rootMember) {
        factorSeed.fill(0);
        throw new Error('that password is not authorized for dashboard or root access');
      }

      var factorSeeds = {};
      factorSeeds[passwordFactor.factor_id] = factorSeed;
      var v3Route = '/api/identity/unlock/password';
      try {
        if (!R.policySatisfied(envelope.policy, Object.keys(factorSeeds))) {
          var asserted = await _prfAssert();
          var recipient = await (await import('./ceremony/primitives.js'))
            .deriveEncapsulationKeypair(asserted.prf, R.FACTOR_RECIPIENT_PURPOSE);
          var passkeyFactor = envelope.factors.find(function (factor) {
            return factor.type === 'passkey'
              && factor.credential_id === asserted.credentialId
              && factor.recipients.some(function (slot) {
                return slot.recipient_public_key === recipient.publicKeyHex;
              });
          });
          if (!passkeyFactor) {
            asserted.prf.fill(0);
            throw new Error('this device is not enrolled as a root-authorizing passkey');
          }
          factorSeeds[passkeyFactor.factor_id] = asserted.prf;
          v3Route = '/api/identity/unlock/combined';
        }
        if (!R.policySatisfied(envelope.policy, Object.keys(factorSeeds))) {
          throw new Error('more factors are required by this root policy');
        }
        var v3Opened = await R.openFactorPolicyArmor(stored.armored_private_key, factorSeeds);
        var rootSeed = new Uint8Array(v3Opened.seed);
        v3Opened.seed.fill(0);
        var mintedV3 = await _postJson('/api/identity/unlock/password/options', {});
        var rootMessageV3 = new TextEncoder().encode(
          UNLOCK_DOMAIN + _signonI().canonicalJson({
            v: 1, challenge: mintedV3.challenge, origin: mintedV3.origin,
          }));
        var rootSignatureV3 = _signonI().bytesToHex(await crypto.subtle.sign(
          'Ed25519', v3Opened.signingKey, rootMessageV3,
        ));
        await _postJson(v3Route, {
          challenge: mintedV3.challenge, signature: rootSignatureV3,
        });
        try {
          // best-effort: enroll a detected-but-pending device slot now that
          // the root is open (never blocks the unlock)
          try { await _completePendingSlot(rootSeed); }
          catch (e) { if (window.console && console.warn) console.warn('pending device slot not enrolled:', (e && e.message) || e); }
          try { await _signonI().wakeVault({ personalRootSeed: new Uint8Array(rootSeed) }); }
          catch (e) { if (window.console && console.warn) console.warn('vault wake failed:', e); }
          await _fleetCompleteOrMint(rootSeed);
          rootSeed = null;
        } finally { if (rootSeed) rootSeed.fill(0); }
        return;
      } finally {
        Object.values(factorSeeds).forEach(function (seed) { seed.fill(0); });
      }
    }
    // An MFA identity's armor carries a combined factor and opens only with
    // BOTH the password AND a passkey PRF. Branch on the armor itself (the
    // source of truth), not merely the require_pair hint.
    var P = null;
    var isCombined = false;
    try {
      P = await import('./ceremony/primitives.js');
      isCombined = (P.parseArmor(stored.armored_private_key).factors || [])
        .some(function (f) { return f.type === 'combined'; });
    } catch (e) { /* fall through to the password path, which will error clearly */ }
    var opened;
    var unlockRoute = '/api/identity/unlock/password';
    if (isCombined) {
      var prfResult = await _prfAssert();
      var prf = prfResult.prf;
      unlockRoute = '/api/identity/unlock/combined';
      try {
        opened = await P.decryptArmorWithCombined(stored.armored_private_key, password, prf);
      } catch (e) {
        throw new Error('that password and passkey did not open your identity — check them and try again');
      }
    } else {
      try {
        opened = await S.decryptArmor(stored.armored_private_key, password);
      } catch (e) {
        throw new Error('that password does not open your identity — check it and try again');
      }
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
    await _postJson(unlockRoute, {
      challenge: minted.challenge, signature: sig,
    });

    // ONE-SHOT v2→v3 armor upgrade (see _migrateArmorV2Once). Best-effort: a
    // failed upgrade leaves the v2 armor intact and never turns a successful
    // unlock into a lockout.
    try {
      await _migrateArmorV2Once({ rootSeed: wakeSeed, password: password });
    } catch (e) {
      if (window.console && console.warn) {
        console.warn('one-shot armor upgrade failed (v2 stays):', (e && e.message) || e);
      }
    }

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

    // Complete a pending Fleet join, else mint the Fleet runtime + reachability
    // credential — the SAME block the passkey-root path runs, shared via
    // _fleetCompleteOrMint so the two root-releasing paths can never diverge.
    try {
      await _fleetCompleteOrMint(wakeSeed);
      wakeSeed = null;  // the ceremony zeroed the shared Uint8Array
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

  // Recover access with the printed code: it opens the root ALONE (the
  // emergency floor), signs the same root-authorized unlock the password path
  // signs, and lands the person on their credentials so they can set a new
  // password or add a passkey — authorized by the very code they just used.
  async function _unlockWithRecovery(printable) {
    var R = await import('./ceremony/root-factor-policy.js');
    var recovery = await import('./ceremony/recovery.js');
    var code;
    try { code = await recovery.decodeRecoveryCode(String(printable || '').trim()); }
    catch (e) { throw new Error('That is not a valid recovery code — check the characters.'); }
    var stored = await _fetchJson('/api/identity/personal');
    var opened;
    try { opened = await R.openRootWithRecovery(stored.armored_private_key, code); }
    catch (e) { throw new Error('That recovery code did not open your identity — check it and try again.'); }
    var rootSeed = new Uint8Array(opened.seed);
    opened.seed.fill(0);
    try {
      var minted = await _postJson('/api/identity/unlock/password/options', {});
      var message = new TextEncoder().encode(
        UNLOCK_DOMAIN + _signonI().canonicalJson({
          v: 1, challenge: minted.challenge, origin: minted.origin,
        }));
      var sig = _signonI().bytesToHex(await crypto.subtle.sign('Ed25519', opened.signingKey, message));
      await _postJson('/api/identity/unlock/password', { challenge: minted.challenge, signature: sig });
      // land straight on the credentials screen to re-establish factors
      try { sessionStorage.setItem('autonomy.factor.open-credentials', '1'); } catch (e) { /* best-effort */ }
      try { await _signonI().wakeVault({ personalRootSeed: new Uint8Array(rootSeed) }); }
      catch (e) { if (window.console && console.warn) console.warn('vault wake failed:', (e && e.message) || e); }
      try { await _fleetCompleteOrMint(rootSeed); rootSeed = null; }
      catch (e) { if (window.console && console.warn) console.warn('fleet after recovery unlock failed:', (e && e.message) || e); }
    } finally { if (rootSeed) rootSeed.fill(0); }
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
      '<button id="unlock-use-recovery" data-testid="unlock-use-recovery" class="' + _ROW_CLS + '">' +
      '<svg class="w-4.5 h-4.5 text-gray-400 flex-shrink-0" fill="none" stroke="currentColor" stroke-width="1.7" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M21 2l-2 2m-7.6 7.6a5.5 5.5 0 1 1-7.78 7.78 5.5 5.5 0 0 1 7.78-7.78zm0 0L15.5 7.5m0 0 3 3L22 7l-3-3"/></svg>' +
      'Use a recovery code</button>';
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

    if (U.mode === 'recovery') {
      card.innerHTML =
        '<div class="flex flex-col items-center flex-1 md:flex-none justify-center md:justify-start">' +
        '<h1 class="text-2xl md:text-xl font-semibold">Use a recovery code</h1>' +
        '<p class="text-gray-400 mt-2">Enter your recovery code to get back in. You’ll be taken ' +
        'straight to your credentials so you can set a new password or add a passkey.</p>' +
        '<input id="unlock-recovery-input" data-testid="unlock-recovery-input" type="text" spellcheck="false" ' +
        'autocapitalize="off" placeholder="Recovery code" class="mt-6 w-full bg-gray-900 border border-gray-700 ' +
        'rounded-xl px-4 py-3 text-white">' +
        '</div>' +
        '<div class="md:mt-7 pt-7 md:pt-0">' +
        '<button id="unlock-recovery-submit" data-testid="unlock-recovery-submit" class="w-full bg-amber-500 ' +
        'hover:bg-amber-400 text-black font-semibold rounded-xl py-4 md:py-3">Recover access</button>' +
        '<div class="flex justify-center mt-4 text-sm">' +
        '<button id="unlock-recovery-back" class="text-indigo-400 hover:underline">Back</button></div>' +
        '<div id="unlock-error" data-testid="unlock-error" class="' + (U.error ? '' : 'hidden ') +
        'text-sm text-red-400 mt-4">' + _esc(U.error || '') + '</div>' +
        '<div id="unlock-busy" class="' + (U.busy ? '' : 'hidden ') + 'text-xs text-gray-500 mt-3">working&hellip;</div>' +
        '</div>';
      var recInput = card.querySelector('#unlock-recovery-input');
      var recSubmit = card.querySelector('#unlock-recovery-submit');
      var recBack = card.querySelector('#unlock-recovery-back');
      var runRecovery = function () {
        var code = recInput ? recInput.value : '';
        if (!code) return;
        _run(function () { return _unlockWithRecovery(code); });
      };
      if (recSubmit) recSubmit.addEventListener('click', runRecovery);
      if (recInput) {
        recInput.addEventListener('keydown', function (e) { if (e.key === 'Enter') runRecovery(); });
        recInput.focus();
      }
      if (recBack) recBack.addEventListener('click', function () { _switchMode('password'); });
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
      : (U.mfa
          ? 'Enter your password, then confirm with Face ID — both together open your dashboard.'
          : 'Enter your password to open your dashboard.');
    var primary = passkey ? 'Unlock with Face ID'
      : (U.mfa ? 'Unlock with password + Face ID' : 'Unlock');

    // Reach pill: amber when this method releases the root, sky when it only
    // opens the dashboard. Two colours, one meaning — from the design.
    var opensRoot = passkey ? U.passkeyOpensRoot : (U.mfa || U.pwOpensRoot);
    var reachPill = '<div class="flex justify-center mt-3"><span data-testid="unlock-reach" '
      + 'class="text-[11px] rounded-full px-2.5 py-0.5 border '
      + (opensRoot
          ? 'text-amber-300 border-amber-400/30 bg-amber-400/10">opens the dashboard and your keys'
          : 'text-sky-300 border-sky-400/30 bg-sky-400/10">opens the dashboard')
      + '</span></div>';
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
      reachPill +
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
    on('unlock-use-recovery', function () { _switchMode('recovery'); });
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
    U.rpId = status.rp_id || null;
    U.passkeys = status.passkeys || [];
    U.mfa = !!(status.personal_identity && status.personal_identity.require_pair);
    // Reach: which method OPENS THE ROOT vs only grants access. Derived from the
    // armor's own factors via the tested policy model, best-effort — if the
    // armor can't be read, reach stays unknown and unlock proceeds unchanged.
    U.passkeyOpensRoot = false;
    U.passkeyCanStart = U.passkeysForHost > 0;
    U.pwOpensRoot = !U.mfa && U.hasIdentity;   // a standalone password opens the root
    U.armorText = null;
    U.factorPolicy = null;
    if (U.hasIdentity) {
      try {
        var personal = await _fetchJson('/api/identity/personal');
        U.armorText = personal.armored_private_key;
        try { U.factorPolicy = await _fetchJson('/api/identity/factor-policy'); }
        catch (e) { U.factorPolicy = null; }
        if (U.factorPolicy && U.factorPolicy.armor_version === 3) {
          var passwordRows = U.factorPolicy.factors.filter(function (f) { return f.type === 'password'; });
          U.pwOpensRoot = passwordRows.some(function (f) { return f.root_role === 'individual'; });
          U.passkeyOpensRoot = U.factorPolicy.factors.some(function (f) {
            return f.type === 'passkey' && f.root_role === 'individual';
          });
          U.passkeyCanStart = U.factorPolicy.factors.some(function (f) {
            return f.type === 'passkey'
              && (f.access === 'enabled' || f.root_role === 'individual');
          });
          var passwordHasAccess = passwordRows.some(function (f) { return f.access === 'enabled'; });
          U.mfa = !passwordHasAccess && passwordRows.some(function (f) {
            return f.root_role === 'mfa-member';
          });
        } else {
          var Pm = await import('./ceremony/primitives.js');
          var Pol = await import('./ceremony/factor-policy.js');
          var m = Pol.buildModel(status, Pm.parseArmor(U.armorText));
          U.mfa = m.mfa;
          U.pwOpensRoot = Pol.level(m, 'pass') === 'b';
          U.passkeyOpensRoot = Pol.level(m, 'face') === 'b';   // a full-authority passkey
        }
      } catch (e) { /* reach unknown; the ceremonies still work */ }
    }
    if (!U.fleetRootRequired && U.passkeyCanStart && U.webauthnOk) {
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
