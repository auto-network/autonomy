/* Accept-invitation paste flow (auto-a1xq3) — the operator-ruled ingress.
 *
 * PURE parse-and-navigate: the pasted invitation link is NEVER sent to any
 * server, never persisted, and validated by SHAPE only, locally (MC's
 * constraint: reachability checks are the leak; "validate this link" must
 * not become a POST). Two accepted forms, both handled by NAVIGATION so
 * the fragment survives every hop as a fragment:
 *
 *   1. a share link  …/l/<32hex>#t=<bearer>       → navigate to the pasted
 *      URL itself: the bridge's same-origin bootloader resolves the
 *      envelope exactly as reviewed and hands back to the local shell;
 *   2. a handoff URL …/network/join?org=…#…       → navigate LOCALLY
 *      (location.origin + path + the pasted query and fragment), so a
 *      handoff copied from anywhere lands on THIS node's shell.
 *
 * This file deliberately contains no network primitives and no storage —
 * the structural-absence tests pin both. The identity-panel mount that
 * calls into it is crypto's chrome (their file-level ack).
 */
(function (root, factory) {
  'use strict';

  var api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.AutonomyAcceptInvitation = api;
})(typeof window !== 'undefined' ? window : null, function (root) {
  'use strict';

  var SHARE_PATH = /^\/l\/[0-9a-f]{32}$/;

  /** Shape-parse a pasted invitation link.
   *
   * Returns one of:
   *   {kind: "bridge", destination: <the pasted URL, verbatim>}
   *   {kind: "local",  destination: "/network/join?<query>#<fragment>"}
   *   {kind: "error",  reason: <plain sentence for the inline hint>}
   */
  function parseInvitationLink(pasted) {
    var text = (pasted || '').trim();
    if (!text) return {kind: 'error', reason: 'Paste an invitation link.'};
    var url;
    try {
      url = new URL(text);
    } catch (e) {
      return {kind: 'error', reason: 'That does not look like a link.'};
    }
    if (url.protocol !== 'https:' && url.protocol !== 'http:') {
      return {kind: 'error', reason: 'That does not look like a link.'};
    }
    var fragment = new URLSearchParams(url.hash.replace(/^#/, ''));

    if (SHARE_PATH.test(url.pathname)) {
      if (!fragment.get('t')) {
        return {
          kind: 'error',
          reason: 'This link is missing its secret part (after #) — ' +
            'some apps strip it when forwarding. Ask for a fresh link.',
        };
      }
      return {kind: 'bridge', destination: url.href};
    }

    if (url.pathname === '/network/join') {
      var query = new URLSearchParams(url.search);
      if (!query.get('org') || !query.get('invite_ref')) {
        return {
          kind: 'error',
          reason: 'This link is missing its invitation details. ' +
            'Ask for a fresh link.',
        };
      }
      if (!fragment.get('t')) {
        return {
          kind: 'error',
          reason: 'This link is missing its secret part (after #) — ' +
            'some apps strip it when forwarding. Ask for a fresh link.',
        };
      }
      return {kind: 'local', destination: '/network/join' + url.search + url.hash};
    }

    return {
      kind: 'error',
      reason: 'That is not an Autonomy invitation link.',
    };
  }

  /** Parse and, on success, navigate. Returns the parse result either way
   * so the caller can render the inline hint on errors. */
  function acceptPastedLink(pasted, navigate) {
    var go = navigate || function (dest) { root.location.assign(dest); };
    var result = parseInvitationLink(pasted);
    if (result.kind !== 'error') go(result.destination);
    return result;
  }

  return {
    parseInvitationLink: parseInvitationLink,
    acceptPastedLink: acceptPastedLink,
  };
});
