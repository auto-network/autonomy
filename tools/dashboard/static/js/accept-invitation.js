/* Accept-invitation paste flow (auto-a1xq3, auto-yw5gz) — the operator-ruled
 * ingress.
 *
 * PURE parse: the pasted invitation link is NEVER sent to any server, never
 * persisted, and validated by SHAPE only, locally (MC's constraint:
 * reachability checks are the leak; "validate this link" must not become a
 * POST). Two accepted forms:
 *
 *   1. a share link  …/l/<32hex>#k=<key>&t=<bearer> → CONNECT LOCALLY. A user
 *      with a dashboard resolves the link on their OWN origin instead of
 *      navigating to the relay bridge: the parse hands back the relay host
 *      and channel token (transport credentials) plus the bearer, and the
 *      caller opens the org tunnel over its own dashboard. The bearer
 *      (#t=) is returned for the caller to HOLD client-side — it never
 *      leaves the browser (auto-yw5gz);
 *   2. a handoff URL …/network/join?org=…#…       → navigate LOCALLY
 *      (location.origin + path + the pasted query and fragment), so a
 *      handoff copied from anywhere lands on THIS node's shell.
 *
 * This file deliberately contains no network primitives and no storage —
 * the structural-absence tests pin both; fetching public routing metadata and
 * opening the authenticated channel are the caller's job, keeping this module
 * a pure parser. The
 * identity-panel mount that calls into it is crypto's chrome (their
 * file-level ack).
 */
(function (root, factory) {
  'use strict';

  var api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.AutonomyAcceptInvitation = api;
})(typeof window !== 'undefined' ? window : null, function (root) {
  'use strict';

  var SHARE_PATH = /^\/l\/[0-9a-f]{32}$/;
  var KEY43 = /^[A-Za-z0-9_-]{43}$/;
  var HEX64 = /^[0-9a-f]{64}$/;

  function decodeChannelPub(value) {
    if (typeof value !== 'string' || !KEY43.test(value)) return null;
    try {
      var raw = atob(value.replace(/-/g, '+').replace(/_/g, '/') + '=');
      if (raw.length !== 32) return null;
      var hex = '';
      for (var i = 0; i < raw.length; i++) {
        hex += raw.charCodeAt(i).toString(16).padStart(2, '0');
      }
      return hex;
    } catch (_) {
      return null;
    }
  }

  /** Shape-parse a pasted invitation link.
   *
   * Returns one of:
   *   {kind: "bridge", relayHost: <origin>, channelToken: <32hex>,
   *                    bearer: <the #t= secret, for the caller to HOLD>}
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
      var channelPub = decodeChannelPub(fragment.get('k'));
      var bearer = fragment.get('t');
      if (!channelPub || !HEX64.test(bearer || '')) {
        return {
          kind: 'error',
          reason: 'This link is missing or has an incomplete secret part (after #) — ' +
            'some apps strip it when forwarding. Ask for a fresh link.',
        };
      }
      // Transport credentials only — the relay origin and the channel token
      // (the /l/<token> path segment). The bearer rides back so the caller
      // can HOLD it client-side for the ceremony; it never becomes a request.
      return {
        kind: 'bridge',
        relayHost: url.origin,
        channelToken: url.pathname.slice(3),
        channelPub: channelPub,
        bearer: bearer,
      };
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
      if (!decodeChannelPub(fragment.get('k')) ||
          !HEX64.test(fragment.get('t') || '')) {
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

  /** Parse and dispatch a pasted link. Returns the parse result either way
   * so the caller can render the inline hint on errors.
   *
   * *handlers* is ``{navigate, resolve}`` (a bare function is accepted as a
   * legacy ``navigate``):
   *   - a ``local`` link is handed to ``navigate(destination)`` (default:
   *     ``location.assign``), preserving the fragment across the hop;
   *   - a ``bridge`` link is handed to ``resolve({relayHost, channelToken,
   *     bearer})`` — the caller resolves it on its OWN origin. No network
   *     happens here (this module stays structurally network-free); when no
   *     ``resolve`` handler is supplied a bridge link is parsed but not acted
   *     on, so the bearer never escapes by default.
   */
  function acceptPastedLink(pasted, handlers) {
    if (typeof handlers === 'function') handlers = {navigate: handlers};
    handlers = handlers || {};
    var navigate = handlers.navigate ||
      function (dest) { root.location.assign(dest); };
    var result = parseInvitationLink(pasted);
    if (result.kind === 'local') {
      navigate(result.destination);
    } else if (result.kind === 'bridge' && handlers.resolve) {
      handlers.resolve(result);
    }
    return result;
  }

  return {
    decodeChannelPub: decodeChannelPub,
    parseInvitationLink: parseInvitationLink,
    acceptPastedLink: acceptPastedLink,
  };
});
