/* Local-origin invite display/consent shell (auto-1ihgz).
 *
 * Display and consent ONLY: reads the invitation from the URL the bridge
 * (or bootloader) minted — public context in the query, both credentials
 * in the fragment — renders it, and gates everything behind an explicit
 * action that currently leads to an honest held state. No password field,
 * no network calls, no ceremony code: acceptance mechanics await the
 * ceremony-workflow ruling (auto-9rw91), and the passphrase must never
 * gain an HTTP ingress (I1).
 */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  function readInputs() {
    var query = new URLSearchParams(location.search);
    var fragment = new URLSearchParams(location.hash.replace(/^#/, ""));
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
      /^[0-9a-f]{64}$/.test(inputs.inviteRef) &&
      inputs.bearer.length > 0
    );
  }

  function render() {
    var inputs = readInputs();
    if (!looksComplete(inputs)) {
      $("status-line").textContent =
        "This link is missing part of its invitation.";
      $("incomplete").classList.remove("hidden");
      return;
    }
    $("status-line").textContent =
      "You've been invited to join an organization.";
    $("org-id").textContent = inputs.org;
    $("invite-ref").textContent = inputs.inviteRef.slice(0, 16) + "…";
    $("invitation").classList.remove("hidden");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", render);
  } else {
    render();
  }
})();
