// The one shared invitation-URL serializer for the browser, twin of
// tools/network/invitation.py::build_invitation_join_url (graph://4f9e881c-a9 §3).
//
// A viewer invitation URL carries two INDEPENDENT fragment values:
//   k = the per-link channel-verification PUBLIC key (authenticates the
//       serving endpoint), unpadded base64url of the 32 raw key bytes — the
//       same encoding content-share links use;
//   t = the invitation bearer (buys only the right to ASK to join).
// The canonical (stored/server) URL never carries either value; they live
// only in the fragment, which browsers never transmit.
//
// build() returns { complete, url, reason }. A complete result carries BOTH
// values and a full url. When either is absent the result is an explicit
// incomplete/legacy marker (url === null, reason names the gap): the caller
// must not present it as a usable invitation, and it is NEVER downgraded to a
// bearer-only URL, nor is root_pub ever substituted for k.
(function () {
  'use strict';
  var HEX64 = /^[0-9a-f]{64}$/;

  function hexToBase64Url(hex) {
    var bytes = new Uint8Array(hex.length / 2);
    for (var i = 0; i < bytes.length; i++) {
      bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
    }
    var bin = '';
    for (var j = 0; j < bytes.length; j++) bin += String.fromCharCode(bytes[j]);
    return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }

  function canonicalError(canonicalUrl) {
    // A structurally invalid canonical URL is a bug upstream, not a legacy
    // link; report it so a caller never renders a plausible-but-broken URL.
    if (typeof canonicalUrl !== 'string' || !canonicalUrl) {
      return 'invitation canonical URL is missing';
    }
    var parsed;
    try { parsed = new URL(canonicalUrl); } catch (e) { return 'invitation canonical URL is malformed'; }
    if (parsed.protocol !== 'https:' || !parsed.host || parsed.search
        || parsed.hash || parsed.username || parsed.password) {
      return 'invitation canonical URL is malformed';
    }
    return null;
  }

  function build(canonicalUrl, channelPub, bearer) {
    var badUrl = canonicalError(canonicalUrl);
    if (badUrl) return { complete: false, url: null, reason: badUrl };
    if (typeof channelPub !== 'string' || !HEX64.test(channelPub)) {
      return {
        complete: false, url: null,
        reason: 'channel-verification key is absent (legacy or keyless link)',
      };
    }
    if (bearer === null || bearer === undefined || bearer === '') {
      return {
        complete: false, url: null,
        reason: 'invitation bearer is absent (minted before bearers were retained)',
      };
    }
    if (typeof bearer !== 'string' || !HEX64.test(bearer)) {
      return {
        complete: false, url: null,
        reason: 'invitation bearer must be 64 lowercase hex characters',
      };
    }
    var fragment = 'k=' + hexToBase64Url(channelPub)
      + '&t=' + encodeURIComponent(bearer);
    return { complete: true, url: canonicalUrl + '#' + fragment, reason: null };
  }

  window.AutonomyInvitationUrl = { build: build };
})();
