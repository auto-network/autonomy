/* The org:join bridge page (auto-y7nap).
 *
 * Runs entirely client-side over a FIXED static shell: the server never
 * sees the invitation's ledger bearer (fragment-only) and never
 * interpolates anything into these bytes. This page performs NO ceremony —
 * it either hands the visitor to their LOCAL node (which owns the claim
 * flow) or hands their own coding agent the install primer plus the
 * invite link. All crypto stays local/E2E; the July trust ruling that a
 * relay-served page must not run the join ceremony holds by construction.
 * Per the operator's ingress ruling there is NO auto-detection of local
 * nodes; both affordances always render and the user picks. The page's
 * ONE network interaction is the root-pinned E2E join channel, over
 * which the ORG self-describes (name/description/icon) — auto-r7kk4.
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

  // ── Org self-description over the E2E join channel (auto-r7kk4) ────
  // The one network interaction this page performs, and it is the same
  // authenticated read the join flow is built on: open the root-pinned
  // channel to the ORG'S OWN node and ask for the invitation context.
  // The reply's org_name/org_description/org_icon are trustworthy because the
  // org said them over a channel pinned to the root key the invitation
  // itself carries — a link can claim anything; this cannot. The BEARER
  // is not involved anywhere in this read (channel token only).

  function safeIcon(value) {
    return (typeof value === "string" &&
            /^data:image\/(png|jpeg|gif|webp|svg\+xml);base64,[A-Za-z0-9+/=]+$/
              .test(value) && value.length <= 300000)
      ? value : null;
  }

  function renderOrgHeader(context, inputs) {
    var name = typeof context.org_name === "string"
      ? context.org_name.slice(0, 120) : "";
    if (!name) return;
    $("org-name").textContent = name;
    var byline = typeof context.org_description === "string"
      ? context.org_description.slice(0, 300) : "";
    if (byline) $("org-byline").textContent = byline;
    var icon = safeIcon(context.org_icon);
    if (icon) {
      $("org-icon").src = icon;
      $("org-icon").classList.remove("hidden");
    }
    $("org-header").classList.remove("hidden");
    $("invite-line").textContent =
      name + " has invited you. Two ways in — pick whichever fits.";
  }

  function enrichFromOrg(inputs) {
    var a = window.autonet;
    if (!a || !a.openSocket || !a.performHandshake) return;
    var scheme = location.protocol === "https:" ? "wss" : "ws";
    var url = scheme + "://" + location.host +
      "/v1/links/" + inputs.channelToken + "/channel";
    a.openSocket(url).then(function (ws) {
      return a.performHandshake(ws, {
        org: inputs.org,
        token: inputs.channelToken,
        rootPub: inputs.rootPub,
      });
    }).then(function (channel) {
      var request = new TextEncoder().encode(
        a.canonicalJson({ v: 1, op: "context" }));
      return channel.sendMessage(request).then(function () {
        return channel.recvMessage();
      });
    }).then(function (bytes) {
      var reply = JSON.parse(new TextDecoder().decode(bytes));
      if (reply && reply.status === "ok") renderOrgHeader(reply, inputs);
    }).catch(function () {
      // Org unreachable or an older node without the fields: the minimal
      // display stands. Enrichment must never break the page.
    });
  }

  var _renderBase = render;
  render = function () {
    _renderBase();
    var inputs = readInputs();
    if (looksComplete(inputs)) enrichFromOrg(inputs);
  };

  if (typeof module === "object" && module.exports) {
    module.exports = { safeIcon: safeIcon };
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", render);
    } else {
      render();
    }
  }
})();
