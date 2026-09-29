/* Link publish / revoke signing, shared by the Central link review and the
 * operator's own link requests (org invitations, asset share, fleet invites).
 *
 * Moved from pages/worktrees.js (auto-fkhq0.10a): the same factor-aware unlock
 * only when no retained session carries this organization, and the same
 * signature over TUNNEL + the control path that the executor reconstructs.
 * Inline registration is gone: an unregistered organization is refused when
 * the link is requested, so every request that reaches signing already carries
 * its frozen registry request. */

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
  const needsRoot = !_matchingApprovalAuthority(req);
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

// The operator's one-approval invitation (auto-xvqxz) signs the invitation and
// publishes its link in the same window: it opens the personal root once and
// hands it here, instead of this module opening it a second time. The caller
// owns the opened root and zeroes it.
export async function signLinkWithOpenRoot(req, opened) {
  const session = window.AutonomyNetworkSession;
  const signer = window.AutonomyNetworkSigner;
  if (!session || typeof session.signOnWithRootSeed !== 'function' ||
      !signer || typeof signer.signRegistryRequest !== 'function') {
    throw new Error('Approval is unavailable in this browser. Reload the dashboard and try again.');
  }
  if (typeof session.ready === 'function') await session.ready();
  await _authorizeLinkDecision(req, session, opened);
  return _signAuthorizedLinkDecision(req, session, signer, req.registryRequest);
}

// Establish authority for one link action from an opened personal root: sign
// on as this organization's persona unless a retained session already
// carries it.
async function _authorizeLinkDecision(req, session, opened) {
  let rr = req.registryRequest;
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
  // An org:follow link never expires unless revoked: it takes no duration
  // (the server refuses one; auto-eky23).
  const isOrgFollow = !isRevoke && rr.payload && rr.payload.target_type === 'org:follow';
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
  } else if (isOrgFollow) {
    payload = _linkPayloadWithTtl(rr.payload, null);
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
    return (isOrgJoin || isOrgFollow || isRevoke) ? { envelope } : { envelope, ttl };
  } finally {
    // Unchecked is deliberately one action only. Checked retains the
    // non-extractable authority (never any factor material); Lock clears
    // this same AutonomyNetworkSession store.
    if (!req.allowSessionApprovals) await session.signOut();
  }
}
