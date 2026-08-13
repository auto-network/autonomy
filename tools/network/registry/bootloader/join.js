/* The org:join bridge page (auto-y7nap).
 *
 * Runs entirely client-side over a FIXED static shell: the server never
 * sees the invitation's ledger bearer (fragment-only) and never
 * interpolates anything into these bytes. This page performs NO ceremony —
 * it either hands the visitor to their LOCAL node (which owns the claim
 * flow) or hands their own coding agent the install primer plus the
 * invite link. All crypto stays local/E2E; the July trust ruling that a
 * relay-served page must not run the join ceremony holds by construction.
 * Per the operator's ingress ruling there is NO auto-detection: this
 * page makes no network calls at all (CSP: no connect targets); both
 * affordances always render and the user picks.
 *
 * Inputs, all read from the current URL:
 *   query     org, root_pub, invite_ref — public context from the bootloader
 *   fragment  channel_token             — the transport credential (the
 *                                         /l/<token> path segment); bearer-
 *                                         class, so it rides the fragment
 *                                         and is never re-sent to a server
 *   fragment  t                         — the LEDGER BEARER; same rule
 */
(function () {
  "use strict";

  var LOCAL_NODE = "https://localhost:8080";

  function $(id) { return document.getElementById(id); }

  function readInputs() {
    var query = new URLSearchParams(location.search);
    var fragment = new URLSearchParams(
      location.hash.replace(/^#/, "")
    );
    return {
      org: query.get("org") || "",
      rootPub: query.get("root_pub") || "",
      inviteRef: query.get("invite_ref") || "",
      channelToken: fragment.get("channel_token") || "",
      bearer: fragment.get("t") || "",
    };
  }

  function looksComplete(inputs) {
    return (
      /^[0-9a-f-]{32,36}$/.test(inputs.org) &&
      /^[0-9a-f]{32}$/.test(inputs.channelToken) &&
      inputs.bearer.length > 0
    );
  }

  function buildBlurb(inputs) {
    // location.origin keeps both URLs correct on every deployment stage
    // (registry host today, bare apex once DNS serves it) — the same
    // host-awareness principle as the /install browser CTA.
    var inviteLink = location.origin + "/l/" + inputs.channelToken +
      "#t=" + encodeURIComponent(inputs.bearer);
    return (
      "I've been invited to join an organization on Autonomy Network! " +
      "Please learn about and install Autonomy Network:\n" +
      location.origin + "/install\n\n" +
      "Once it's set up, my invitation is here:\n" +
      inviteLink
    );
  }

  function localNodeUrl(inputs) {
    var query = new URLSearchParams({
      org: inputs.org,
      root_pub: inputs.rootPub,
      invite_ref: inputs.inviteRef,
    });
    // Same shape the bootloader minted: both credentials in the fragment,
    // so the local hand-off adds no server-visible surface either.
    var fragment = new URLSearchParams({
      channel_token: inputs.channelToken,
      t: inputs.bearer,
    });
    return LOCAL_NODE + "/network/join?" + query.toString() +
      "#" + fragment.toString();
  }

  function render() {
    var inputs = readInputs();
    if (!looksComplete(inputs)) {
      $("invite-line").textContent =
        "This link is missing part of its invitation.";
      $("incomplete").classList.remove("hidden");
      return;
    }
    $("invite-line").textContent =
      "Organization " + inputs.org.slice(0, 8) +
      "… has invited you. Two ways in — pick whichever fits.";
    $("blurb").value = buildBlurb(inputs);
    $("node-link").setAttribute("href", localNodeUrl(inputs));
    $("flows").classList.remove("hidden");

    $("copy").addEventListener("click", function () {
      var blurb = $("blurb");
      blurb.select();
      var done = function () { $("copied").classList.remove("hidden"); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(blurb.value).then(done, done);
      } else {
        document.execCommand("copy");
        done();
      }
    });

  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", render);
  } else {
    render();
  }
})();
