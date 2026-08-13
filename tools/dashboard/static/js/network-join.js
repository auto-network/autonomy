/* The accept-invitation flow (auto-1ihgz).
 *
 * Stepped, as a user walks it: opened bare, the page IS the paste step —
 * a full screen with a visible field and a Next button (parsing via the
 * network-free accept-invitation.js module; the pasted value never leaves
 * this page). Opened with an invitation in the URL, the page is the
 * organization step. Controls appear only when their function exists: the
 * accept action arrives WITH the acceptance ceremony (auto-9rw91), and
 * the organization's verified name/description/icon light up when the
 * org-context read lands on this origin (auto-r7kk4). No password
 * anything, ever (I1).
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

  function hasInvitationParams() {
    return location.search.length > 1 || location.hash.length > 1;
  }

  function looksComplete(inputs) {
    return (
      /^[0-9a-f-]{32,36}$/.test(inputs.org) &&
      /^[0-9a-f]{64}$/.test(inputs.inviteRef) &&
      inputs.bearer.length > 0
    );
  }

  function showPasteStep() {
    $("step-paste").classList.remove("hidden");
    var input = $("invite-input");
    var hint = $("paste-hint");
    input.focus();
    function go() {
      var api = window.AutonomyAcceptInvitation;
      if (!api) { hint.textContent = "Something went wrong loading this page — reload and try again."; return; }
      var result = api.acceptPastedLink(input.value);
      if (result.kind === "error") hint.textContent = result.reason;
    }
    $("next").addEventListener("click", go);
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") go();
    });
  }

  function showOrgStep(inputs) {
    $("step-org").classList.remove("hidden");
    $("org-id").textContent = inputs.org;
    $("invite-ref").textContent = inputs.inviteRef.slice(0, 16) + "…";
    $("org-fallback").textContent =
      inputs.org.slice(0, 1).toUpperCase() || "?";
  }

  function render() {
    if (!hasInvitationParams()) {
      showPasteStep();
      return;
    }
    var inputs = readInputs();
    if (!looksComplete(inputs)) {
      $("step-broken").classList.remove("hidden");
      return;
    }
    showOrgStep(inputs);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", render);
  } else {
    render();
  }
})();
