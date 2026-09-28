/* Link publish / revoke signing, shared by the Central link review.
 *
 * Moved unchanged from pages/worktrees.js (auto-fkhq0.10a): the same factor-
 * aware unlock only when no retained session carries this organization, the
 * same inline register-before-freeze, and the same signature over TUNNEL + the
 * control path that the executor reconstructs. The only change is that the
 * caller supplies req.refreshRegistryRequest(), which re-reads the frozen
 * registry request after an inline registration. */

export function _linkTtlText(ttl) {
  if (!ttl) return 'No expiration';
  // Decompose ANY positive integer of seconds into readable components:
  // 3600 -> "1 Hour", 604800 -> "1 Week", 10420 -> "2 Hours 53 Minutes 40 Seconds".
  const units = [[604800, 'Week'], [86400, 'Day'], [3600, 'Hour'], [60, 'Minute'], [1, 'Second']];
  const parts = [];
  let rem = ttl;
  for (const [secs, name] of units) {
    const n = Math.floor(rem / secs);
    if (n > 0) {
      parts.push(n + ' ' + name + (n === 1 ? '' : 's'));
      rem -= n * secs;
    }
  }
  return parts.join(' ');
}

export const _LINK_DURATION_VALUES = new Set([
  '604800', '2592000', '31536000',
]);


export function _linkPayloadWithTtl(payload, ttl) {
  const next = JSON.parse(JSON.stringify(payload || {}));
  const meta = Object.assign({}, next.meta || {});
  if (ttl === null) delete meta.ttl;
  else meta.ttl = ttl;
  if (Object.keys(meta).length) next.meta = meta;
  else delete next.meta;
  return next;
}

export function _matchingApprovalAuthority(req) {
  const session = window.AutonomyNetworkSession;
  if (!session || typeof session.state !== 'function') return false;
  const state = session.state();
  // Publish carries the org uuid inside the staged payload; revoke's
  // payload is empty by contract, so its rows expose the frozen
  // binding's uuid as req.orgUuid instead.
  const expectedOrg = req.orgUuid || (
    req.registryRequest && req.registryRequest.payload &&
    req.registryRequest.payload.org);
  // One personal sign-on carries a persona per organization, so authority
  // for THIS action is one row of the session, not the whole session.
  return !!(state && state.signedIn && (state.orgs || []).some(
    (entry) => entry.live && entry.org === expectedOrg));
}

// Gate 2 unlocks org authority only for the concrete action being reviewed.
// The passphrase and root plaintext stay inside network-signon.js; this
// function receives only a signed envelope back.
// recovery_policy is 'none' (operator decision: recovery is a sovereign,
// root-mutable policy — the root holder can ADD recovery later via a
// root-signed policy update). NB the policy-update path that makes 'none'
// reversible is a SEPARATE registry companion (registry.auto.network +
// redeploy); until it deploys, 'none' is not yet reversible in the LIVE
// registry, so this flow must not promise "add recovery later" in the UI.
const INLINE_RECOVERY_POLICY = 'none';

// Inline first-publish registration (register-before-freeze). Reuses the
// EXISTING root-signed registration ceremony via exposed internals — no new
// crypto: open the sealed org root with the personal root the shared
// factor-aware unlock just produced, sign a root-direct registration
// envelope, POST it to the server route (which verifies signer==root_pub,
// matches the stored key, and forwards to the registry that verifies the
// signature and returns the binding). The org root plaintext is zeroed
// before this returns (I1); the personal seed belongs to the caller.
async function _registerOrgInline(req, personalRoot) {
  if (!INLINE_RECOVERY_POLICY) {
    throw new Error(
      'First-publish registration is not enabled yet — the organization ' +
      'recovery policy is still being decided. Register from the ' +
      'getting-started flow for now.');
  }
  const session = window.AutonomyNetworkSession;
  const identity = window.AutonomyNetworkIdentity;
  if (!session || !session._internals || !identity || !identity._internals) {
    throw new Error('Registration is unavailable in this browser. Reload and try again.');
  }
  const S = session._internals, I = identity._internals;
  const orgHeaders = req.orgSlug ? { 'X-Graph-Org': req.orgSlug } : {};
  const orgQ = req.orgSlug ? ('?org=' + encodeURIComponent(req.orgSlug)) : '';
  const keyResp = await fetch('/api/network/org-key' + orgQ, { headers: orgHeaders });
  if (!keyResp.ok) {
    throw new Error('Could not load this organization\'s signing key (' + keyResp.status + ').');
  }
  const orgKey = await keyResp.json();
  if (!orgKey.armored_private_key && !orgKey.sealed_root_key) {
    throw new Error('This organization has no signing key to register.');
  }
  // Unseal the org root with the already-open personal root. A retired
  // password-only org armor cannot be opened this way and says so.
  const opened = await S.openOrgRootWithSeed(orgKey, personalRoot.seed);
  let rootKey = null;
  try {
    rootKey = await I.importSigningKey(opened.seed);
    const payload = {
      org_uuid: crypto.randomUUID(),
      root_pub: opened.rootPub,
      recovery_policy: INLINE_RECOVERY_POLICY,
    };
    const envelope = await I.signRegistration(rootKey, opened.rootPub, payload);
    const resp = await fetch('/api/network/register', {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, orgHeaders),
      body: JSON.stringify({ org: req.orgSlug, envelope: envelope }),
    });
    const body = await resp.json().catch(function () { return {}; });
    if (!resp.ok || body.ok === false) {
      throw new Error(body.error || ('Registration was refused (' + resp.status + ').'));
    }
  } finally {
    if (opened && opened.seed) { opened.seed.fill(0); opened.seed = null; }
    rootKey = null;   // I1: drop the root; the armor is the survivor
  }
}

export async function _signLinkDecision(self, req, { mount, signal, view, onAuthenticated } = {}) {
  let rr = req.registryRequest;
  const session = window.AutonomyNetworkSession;
  const signer = window.AutonomyNetworkSigner;
  if (!session || typeof session.signOnWithRootSeed !== 'function' ||
      !signer || typeof signer.signRegistryRequest !== 'function') {
    throw new Error('Approval is unavailable in this browser. Reload the dashboard and try again.');
  }
  if (typeof session.ready === 'function') await session.ready();

  // ONE common factor-aware unlock — password, passkey, or both, chosen by
  // the personal armor's own factor policy — opened only when this action
  // needs authority the browser does not already retain. The sheet owns no
  // password field: a publish is authorized by the operator's PERSONA,
  // which derives from the personal root, so the personal root's factors
  // are the only credential that was ever really being asked for.
  const needsRoot = (req.registrationRequired && !rr) || !_matchingApprovalAuthority(req);
  let opened = null;
  if (needsRoot) {
    const { openRoot } = await import('../ceremony/open-root.js');
    const actingName = (req.actingIdentity && req.actingIdentity.name) ||
      req.orgSlug || 'this organization';
    opened = await openRoot({
      title: req.op === 'revoke' ? 'Revoke this share link?' : 'Publish this share link?',
      detail: 'Unlock your personal identity to act as ' + actingName + '.',
      mount, signal, view,
    });
    if (!opened) throw new Error('Approval cancelled.');
  }
  try {
    if (signal?.aborted) throw new DOMException('Approval cancelled.', 'AbortError');
    onAuthenticated?.();
    await _authorizeLinkDecision(req, session, opened);
  } finally {
    // I1: the personal root plaintext dies here whatever happened above.
    if (opened && opened.seed) { opened.seed.fill(0); opened.seed = null; }
  }
  rr = req.registryRequest;
  return _signAuthorizedLinkDecision(req, session, signer, rr);
}

// Establish authority for one link action from an opened personal root:
// register the organization inline when it has never been registered,
// then sign on as this organization's persona unless a retained session
// already carries it.
async function _authorizeLinkDecision(req, session, opened) {
  let rr = req.registryRequest;
  // Register-before-freeze: a keyed-but-unregistered org registers its
  // EXISTING key inline (the root just opened unseals it), then a
  // re-enrich freezes the publish against the now-live binding. Nothing is
  // frozen or executed before the binding exists, so the confused-deputy
  // execute path is untouched.
  if (req.registrationRequired && !rr) {
    // On-the-fly registration: register this org's EXISTING key inline in
    // the SAME Approve, invisibly. auto.network routes scope by the
    // X-Graph-Org header (a bare ?org= is refused cross-org without it), so
    // every call below carries it. Only SKIP registration if the org is
    // DEFINITIVELY already bound (a valid 200 binding from a prior half-
    // completed Approve); ANY other response (404, a scope 403, an error)
    // means "not confirmed bound" -> register. Never guess "bound" from a
    // non-200 and silently skip.
    const orgHeaders = req.orgSlug ? { 'X-Graph-Org': req.orgSlug } : {};
    const bq = req.orgSlug ? ('?org=' + encodeURIComponent(req.orgSlug)) : '';
    let alreadyBound = false;
    try {
      const b = await fetch('/api/network/binding' + bq, { headers: orgHeaders });
      if (b.ok) {
        const bj = await b.json().catch(function () { return {}; });
        alreadyBound = !!(bj && bj.org_uuid && bj.root_pub && bj.registry_url);
      }
    } catch (e) { /* unreachable -> treat as not bound, register */ }
    if (!alreadyBound) await _registerOrgInline(req, opened);
    // Wait for the publish request to freeze against the now-live binding
    // (covers read-after-write timing). Invisible -- it just completes; no
    // "try again" is ever surfaced to the operator.
    for (let attempt = 0; attempt < 15 && !rr; attempt++) {
      // The caller says where the frozen request is re-read (legacy approval
      // row, or the Central bootstrap); both return registry_request.
      rr = await req.refreshRegistryRequest();
      if (!rr) await new Promise(function (resolve) { setTimeout(resolve, 200); });
    }
    if (!rr) {
      throw new Error('Registered your organization, but the publish request '
        + 'did not prepare against the new binding.');
    }
    req.registryRequest = rr;
  }
  if (!rr) {
    throw new Error('This request is missing its auto.network details. Close it and try again.');
  }

  let retained = _matchingApprovalAuthority(req);
  if (!retained) {
    try {
      await session.signOnWithRootSeed(opened.seed, opened.rootPub, {
        org: req.orgSlug, requireServingRuntime: true,
      });
    } catch (error) {
      const message = String((error && error.message) || error || '');
      if (error && error.status === 404 && /org|identity|key/i.test(message)) {
        const missing = new Error(
          'This organization is not ready for approvals yet. Set up its signing authority and try again.');
        missing.code = 'ORG_KEY_NOT_CONFIGURED';
        throw missing;
      }
      throw error;
    }
    retained = _matchingApprovalAuthority(req);
    if (!retained) {
      await session.signOut();
      throw new Error('The unlocked authority does not match this organization.');
    }
  }
}

async function _signAuthorizedLinkDecision(req, session, signer, rr) {
  const isRevoke = req.op === 'revoke';
  const isOrgJoin = !isRevoke && rr.payload && rr.payload.target_type === 'org:join';
  let ttl = null;
  let payload;
  if (isRevoke) {
    // Registry contract: revoke envelopes carry an EMPTY payload — the
    // server refuses to forward anything else.
    payload = {};
  } else if (isOrgJoin) {
    const invitation = await import('../ceremony/invitation.js');
    payload = invitation.buildOrgJoinGrantPayload({
      orgUuid: rr.payload.org,
      inviteId: rr.payload.invite_ref,
      inviteExpiry: rr.payload.expires_at,
    });
    if (rr.payload.meta && Object.keys(rr.payload.meta).length) {
      payload.meta = JSON.parse(JSON.stringify(rr.payload.meta));
    }
  } else {
    ttl = req.duration === 'none' ? null
      : (req.duration === 'custom'
        ? req.customDurationSeconds
        : Number(req.duration));
    payload = _linkPayloadWithTtl(rr.payload, ttl);
  }
  // D19 routing: EVERY link publish/revoke rides the org tunnel now
  // (auto-qol1v retired the org:join HTTP path with the rest), so the
  // dashboard authenticates them LOCALLY and verifies this signature over
  // fixed proof-of-possession bytes (TUNNEL + a control path), NOT the
  // registry's method/path. The executor's _verify_local_publish_authority
  // reconstructs the SAME bytes, so the two sides must agree here
  // (Codex D19 finding #3).
  const signMethod = 'TUNNEL';
  const signPath = isRevoke ? '/control/revoke-link' : '/control/create-link';
  try {
    let envelope;
    try {
      envelope = await signer.signRegistryRequest(
        signMethod, signPath, payload, { org: req.orgSlug });
    } catch (error) {
      throw new Error('This approval could not be signed. Unlock it again and retry.');
    }
    return (isOrgJoin || isRevoke) ? { envelope } : { envelope, ttl };
  } finally {
    // Unchecked is deliberately one action only. Checked retains the
    // non-extractable authority (never any factor material); Lock clears
    // this same AutonomyNetworkSession store.
    if (!req.allowSessionApprovals) await session.signOut();
  }
}
