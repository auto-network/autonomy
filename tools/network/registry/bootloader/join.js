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
 * ONE network interaction is the per-link authenticated E2E join channel, over
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

  function decodeFragmentKey(fragment) {
    if (typeof fragment !== "string" ||
        !/^[A-Za-z0-9_-]{43}$/.test(fragment)) {
      throw new Error("fragment is not an unpadded base64url key");
    }
    var raw = atob(fragment.replace(/-/g, "+").replace(/_/g, "/") + "=");
    if (raw.length !== 32) throw new Error("fragment is not a 32-byte key");
    var hex = "";
    for (var i = 0; i < raw.length; i++) {
      hex += raw.charCodeAt(i).toString(16).padStart(2, "0");
    }
    return hex;
  }

  function readInputs() {
    var query = new URLSearchParams(location.search);
    var fragment = new URLSearchParams(
      location.hash.replace(/^#/, "")
    );
    return {
      org: query.get("org") || "",
      inviteRef: query.get("invite_ref") || "",
      channelToken: fragment.get("channel_token") || "",
      linkKey: fragment.get("k") || "",
      bearer: fragment.get("t") || "",
    };
  }

  function looksComplete(inputs) {
    return (
      /^[0-9a-f-]{32,36}$/.test(inputs.org) &&
      /^[0-9a-f]{32}$/.test(inputs.channelToken) &&
      /^[A-Za-z0-9_-]{43}$/.test(inputs.linkKey) &&
      inputs.bearer.length > 0
    );
  }

  // The invitation link the coding agent hands to the install flow. It is
  // the minted org:join link exactly — the registry grant in the path and
  // BOTH fragment values (graph://4f9e881c-a9 §3): k, the per-link channel
  // key the joining node verifies the org's serving endpoint against, and
  // t, the ledger bearer. tools.network.invitation.invitation_from_join_url
  // refuses a link missing either, so a bearer-only link is not an
  // invitation the agent can use. origin keeps the URL correct on every
  // deployment stage (registry host today, bare apex once DNS serves it).
  function inviteLink(inputs, origin) {
    return origin + "/l/" + inputs.channelToken +
      "#k=" + inputs.linkKey + "&t=" + encodeURIComponent(inputs.bearer);
  }

  function buildBlurb(inputs, origin) {
    return (
      "I've been invited to join an organization on Autonomy Network! " +
      "Please learn about and install Autonomy Network:\n" +
      origin + "/install\n\n" +
      "Once it's set up, my invitation is here:\n" +
      inviteLink(inputs, origin)
    );
  }

  function localNodeUrl(inputs, origin) {
    // The node's own /network/join page reads the same query and fragment
    // the bootloader minted, and it requires relay_host: that is the relay
    // it opens the join channel through. This page's origin IS that relay.
    var query = new URLSearchParams({
      org: inputs.org,
      invite_ref: inputs.inviteRef,
      relay_host: origin,
    });
    // Same shape the bootloader minted: both credentials in the fragment,
    // so the local hand-off adds no server-visible surface either.
    var fragment = new URLSearchParams({
      channel_token: inputs.channelToken,
      k: inputs.linkKey,
      t: inputs.bearer,
    });
    return LOCAL_NODE + "/network/join?" + query.toString() +
      "#" + fragment.toString();
  }

  function render() {
    var inputs = readInputs();
    if (!looksComplete(inputs)) {
      $("incomplete").classList.remove("hidden");
      return;
    }
    var origin = location.origin;
    $("blurb").value = buildBlurb(inputs, origin);
    $("node-link").setAttribute("href", localNodeUrl(inputs, origin));
    $("invitation").classList.remove("hidden");
    $("copy").addEventListener("click", copyInstructions);
  }

  // Copy the blurb. The clipboard API needs a secure context and a user
  // gesture (both true here); when it still refuses, open the instructions
  // and fall back to the textarea selection so the visitor can copy by hand.
  function copyInstructions() {
    var blurb = $("blurb");
    var done = function () {
      $("copy-label").textContent = "Instructions copied";
      $("copy-note").textContent = "Ready to paste into your coding agent.";
    };
    var fallback = function () {
      $("instructions").open = true;
      blurb.focus();
      blurb.select();
      var copied = false;
      try { copied = document.execCommand("copy"); } catch (e) { copied = false; }
      if (copied) { done(); return; }
      $("copy-note").textContent =
        "Copy didn’t work here. Select the instructions below and copy them.";
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(blurb.value).then(done, fallback);
    } else {
      fallback();
    }
  }

  // ── Org self-description over the E2E join channel (auto-r7kk4) ────
  // The one network interaction this page performs, and it is the same
  // authenticated read the join flow is built on: open the link-key-pinned
  // channel to the ORG'S OWN node and ask for the invitation context.
  // The reply's org_name/org_description/org_icon are trustworthy because the
  // org said them over a channel pinned to the key in the invitation URL.
  // is not involved anywhere in this read (channel token only).

  function safeIcon(value) {
    return (typeof value === "string" &&
            /^data:image\/(png|jpeg|gif|webp|svg\+xml);base64,[A-Za-z0-9+/=]+$/
              .test(value) && value.length <= 300000)
      ? value : null;
  }

  // Brand accent: a bare hex color and nothing else — never arbitrary CSS.
  function safeColor(value) {
    return (typeof value === "string" && /^#[0-9a-fA-F]{6}$/.test(value))
      ? value : null;
  }

  function text(value, max) {
    return typeof value === "string" ? value.trim().slice(0, max) : "";
  }

  function initials(name) {
    return name.split(/\s+/).slice(0, 2).map(function (part) {
      return part.charAt(0).toUpperCase();
    }).join("");
  }

  // "member" → "Member", "org_admin" → "Org Admin": presentation only.
  function roleLabel(role) {
    return text(role, 40).replace(/(^|[\s_-])([a-z])/g, function (_m, sep, ch) {
      return (sep ? " " : "") + ch.toUpperCase();
    });
  }

  // invite_expiry is the ledger's millisecond timestamp. Formatted here, in
  // the visitor's own locale and timezone, with the zone named.
  function renderExpiry(expiry) {
    if (typeof expiry !== "number" || !isFinite(expiry) || expiry <= 0) return;
    var when = new Date(expiry);
    if (isNaN(when.getTime())) return;
    $("expiry-date").textContent = when.toLocaleDateString(undefined,
      { year: "numeric", month: "long", day: "numeric" });
    $("expiry-time").textContent = when.toLocaleTimeString(undefined,
      { hour: "numeric", minute: "2-digit", timeZoneName: "short" });
    $("expiry-time").setAttribute("datetime", when.toISOString());
    $("expiry-fact").classList.remove("hidden");
  }

  function renderOrgHeader(context, inputs) {
    var name = text(context.org_name, 120);
    if (!name) return;
    $("org-name").textContent = name;
    $("eyebrow").textContent = "You’re invited to join";
    var byline = text(context.org_description, 300);
    $("org-byline").textContent = byline;
    $("org-byline").classList.toggle("hidden", !byline);
    var icon = safeIcon(context.org_icon);
    if (icon) {
      $("org-icon").src = icon;
      $("org-icon").classList.remove("hidden");
      $("org-initial").classList.add("hidden");
    } else {
      $("org-initial").textContent = name.charAt(0).toUpperCase();
    }
    var accent = safeColor(context.org_color);
    if (accent) $("org-mark").style.setProperty("--org", accent);

    // The inviter: the org's own member directory row for the sponsor, or
    // the honest fallback when the sponsor has no presentation.
    var sponsor = text(context.sponsor_name, 120);
    $("sponsor-name").textContent = sponsor || "an authorized member";
    $("sponsor-initials").textContent = sponsor ? initials(sponsor) : "↗";
    var avatar = safeIcon(context.sponsor_avatar);
    if (avatar) {
      $("sponsor-avatar").src = avatar;
      $("sponsor-avatar").classList.remove("hidden");
      $("sponsor-initials").classList.add("hidden");
    }
    var sponsorByline = text(context.sponsor_byline, 120);
    if (sponsorByline) {
      $("sponsor-byline").textContent = sponsorByline;
      $("sponsor-byline").classList.remove("hidden");
    }
    $("inviter").classList.remove("hidden");

    var role = roleLabel(context.granted_role);
    if (role) {
      $("role").textContent = role;
      renderExpiry(context.invite_expiry);
      $("facts").classList.remove("hidden");
    }
  }

  function enrichFromOrg(inputs) {
    var a = window.autonet;
    if (!a || !a.openSocket || !a.performHandshake || !a.sendOp) return;
    var scheme = location.protocol === "https:" ? "wss" : "ws";
    var url = scheme + "://" + location.host +
      "/v1/links/" + inputs.channelToken + "/channel";
    a.openSocket(url).then(function (ws) {
      return a.performHandshake(ws, {
        org: inputs.org,
        token: inputs.channelToken,
        linkPub: decodeFragmentKey(inputs.linkKey),
      });
    }).then(function (channel) {
      return a.sendOp(channel, { v: 1, op: "context" });
    }).then(function (reply) {
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
    module.exports = {
      safeIcon: safeIcon, safeColor: safeColor,
      decodeFragmentKey: decodeFragmentKey,
      inviteLink: inviteLink, buildBlurb: buildBlurb,
      localNodeUrl: localNodeUrl, looksComplete: looksComplete,
      roleLabel: roleLabel, initials: initials,
    };
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", render);
    } else {
      render();
    }
  }
})();
