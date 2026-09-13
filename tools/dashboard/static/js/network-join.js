/* The accept-invitation flow (auto-1ihgz, auto-yw5gz).
 *
 * Stepped, as a user walks it: opened bare, the page IS the paste step —
 * a full screen with a visible field and a Next button (parsing via the
 * network-free accept-invitation.js module). Public relay metadata is used
 * only for routing. The browser then authenticates the selected connector
 * with fragment k and obtains all human and ledger context over that channel.
 *
 * The bearer (#t=) is HELD in this page and reaches the organization only
 * inside the encrypted claim — it never enters an HTTP request, URL query,
 * or local dashboard endpoint. Controls appear only when their function
 * exists: the accept action appears once the organization has answered
 * over its own tunnel and said what you would be joining as. Accepting
 * opens the SHARED root control, which presents whichever factors this
 * identity has. No password anything, ever, on this page (I1).
 */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  // The ledger bearer, held ONLY in this closure for the claim ceremony.
  // It is never written to storage or placed in an unencrypted request.
  var heldBearer = "";

  function decodeChannelPub(value) {
    var api = window.AutonomyAcceptInvitation;
    return api && api.decodeChannelPub ? api.decodeChannelPub(value) : null;
  }

  function readInputs() {
    var query = new URLSearchParams(location.search);
    var fragment = new URLSearchParams(location.hash.replace(/^#/, ""));
    return {
      org: query.get("org") || "",
      inviteRef: query.get("invite_ref") || "",
      relayHost: query.get("relay_host") || "",
      channelToken: fragment.get("channel_token") || "",
      channelPub: decodeChannelPub(fragment.get("k")),
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
      /^[0-9a-f]{32}$/.test(inputs.channelToken) &&
      /^[0-9a-f]{64}$/.test(inputs.channelPub || "") &&
      /^[0-9a-f]{64}$/.test(inputs.bearer) &&
      /^https?:\/\/[^/]+$/.test(inputs.relayHost)
    );
  }

  // -- verified-org header (auto-r7kk4 fields), same guards as the bridge --
  // The org self-describes over the E2E tunnel; the values are trustworthy
  // because the org said them over a channel pinned to the invitation's own
  // root key. Re-validated here client-side even though the endpoint already
  // bounded them — a bare hex color and a bounded data: icon, never more.

  function safeIcon(value) {
    return (typeof value === "string" &&
            /^data:image\/(png|jpeg|gif|webp|svg\+xml);base64,[A-Za-z0-9+/=]+$/
              .test(value) && value.length <= 300000)
      ? value : null;
  }

  function safeColor(value) {
    return (typeof value === "string" && /^#[0-9a-fA-F]{6}$/.test(value))
      ? value : null;
  }

  function renderVerifiedHeader(reply) {
    var name = typeof reply.org_name === "string"
      ? reply.org_name.slice(0, 120) : "";
    if (name) {
      $("org-name").textContent = name;
      $("org-fallback").textContent = name.trim().charAt(0).toUpperCase();
    }
    var byline = typeof reply.org_description === "string"
      ? reply.org_description.slice(0, 300) : "";
    if (byline) $("org-byline").textContent = byline;
    var icon = safeIcon(reply.org_icon);
    if (icon) {
      $("org-icon").src = icon;
      $("org-icon").classList.remove("hidden");
      $("org-fallback").classList.add("hidden");
    }
    var accent = safeColor(reply.org_color);
    if (accent) {
      $("org-header").style.borderColor = accent;
      $("org-fallback").style.backgroundColor = accent;
    }
  }

  function renderProfile(prefix, profile) {
    var name = profile && typeof profile.display_name === "string"
      ? profile.display_name.trim().slice(0, 200) : "";
    if (!name) throw new Error("profile presentation unavailable");
    var tile = $(prefix + "-profile");
    var avatar = $(prefix + "-avatar");
    $(prefix + "-name").textContent = name;
    $(prefix + "-byline").textContent = typeof profile.byline === "string"
      ? profile.byline.slice(0, 300) : (profile.biography || "");
    avatar.replaceChildren();
    var image = typeof profile.avatar === "string" &&
      /^data:image\/(jpeg|png|webp);base64,[A-Za-z0-9+/=]+$/.test(profile.avatar)
      ? profile.avatar : null;
    if (image) {
      var img = document.createElement("img");
      img.src = image;
      img.alt = "Profile photo of " + name;
      avatar.appendChild(img);
    } else {
      avatar.textContent = profile.initials || name.split(/\s+/).slice(0, 2)
        .map(function (part) { return part.charAt(0); }).join("").toUpperCase();
    }
    tile.classList.remove("hidden");
  }

  function renderPresentation(presentation, joiningProfile) {
    renderProfile("joining", {
      display_name: joiningProfile.display_name,
      byline: joiningProfile.biography,
      avatar: joiningProfile.avatar_icon_data_uri,
      initials: joiningProfile.initials,
    });
    if (!presentation || !presentation.sponsorName) {
      throw new Error("inviter profile presentation unavailable");
    }
    renderProfile("inviter", {
      display_name: presentation.sponsorName,
      byline: presentation.sponsorByline,
      avatar: presentation.sponsorAvatar,
    });
  }

  function loadJoiningProfile() {
    return fetch("/api/graph/settings/autonomy.user/default").then(function (response) {
      if (!response.ok) throw new Error("joining profile unavailable");
      return response.json();
    }).then(function (body) {
      if (!body || !body.payload) throw new Error("joining profile unavailable");
      return body.payload;
    });
  }

  // Routing identifiers are shown only as secondary technical details. No
  // organization identity is inferred from the untrusted public envelope.
  function fillOrgStep(context) {
    $("step-org").classList.remove("hidden");
  }

  // Public metadata supplies routing only. k and t are closure-held values and
  // are absent from this URL, request body, and the returned registry envelope.
  function fetchEnvelope(inputs) {
    return fetch("/api/network/invite/resolve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ relay_host: inputs.relayHost, channel_token: inputs.channelToken }),
    }).then(function (r) {
      if (!r.ok) throw new Error("organization unreachable");
      return r.json();
    }).then(function (envelope) {
      if (!envelope || !envelope.ok ||
          !/^[0-9a-f-]{32,36}$/.test(envelope.org || "") ||
          !/^[0-9a-f]{64}$/.test(envelope.invite_ref || "")) {
        throw new Error("organization unreachable");
      }
      inputs.org = envelope.org;
      inputs.inviteRef = envelope.invite_ref;
      return inputs;
    });
  }

  function showPasteStep() {
    $("step-paste").classList.remove("hidden");
    var input = $("invite-input");
    var hint = $("paste-hint");
    input.focus();
    function go() {
      var api = window.AutonomyAcceptInvitation;
      if (!api) {
        hint.textContent =
          "Something went wrong loading this page — reload and try again.";
        return;
      }
      var result = api.acceptPastedLink(input.value, {
        // A handoff link still navigates locally (its fragment survives the
        // hop); the org step then renders from the URL on reload.
        navigate: function (dest) { location.assign(dest); },
        // A relay share link resolves routing metadata through this dashboard. The
        // fragment values are held here and never sent with that request.
        resolve: function (parsed) {
          heldBearer = parsed.bearer;
          $("step-paste").classList.add("hidden");
          fetchEnvelope(parsed).then(showOrgStep)
            .catch(function () { reportTerminal({
              state: "link-lost", reason: "org-unreachable",
            }); });
        },
      });
      if (result.kind === "error") hint.textContent = result.reason;
    }
    $("next").addEventListener("click", go);
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") go();
    });
  }

  // The org step reached with a handoff link's context already in the URL.
  // The minimal id + invite ref render immediately; if the link also named
  // its relay host, the verified header lights up over this origin too.
  function showOrgStep(inputs) {
    heldBearer = inputs.bearer;
    fillOrgStep({ org: inputs.org, inviteRef: inputs.inviteRef });
    // Open the org's own channel to learn what this invitation grants. The
    // accept control appears only if that answer arrives; an organization we
    // cannot reach offers no action, which is the honest state.
    connectSession(inputs)
      .then(function (state) {
        if (!state) return;
        if (state.state === "org") {
          if (session.brand && session.brand.orgName) {
            renderVerifiedHeader({
              org_name: session.brand.orgName,
              org_description: session.brand.orgDescription,
              org_color: session.brand.orgColor,
              org_icon: session.brand.orgIcon,
            });
          }
          session.grantedRole = state.grantedRole;
          loadJoiningProfile().then(function (profile) {
            session.joiningProfile = profile;
            renderPresentation(session.context.presentation, profile);
            offerAccept(state);
            wireAccept(inputs);
          }).catch(function (error) { reportTerminal({
            state: "closed",
            reason: /inviter/.test(String(error && error.message))
              ? "inviter-profile-unavailable" : "joining-profile-unavailable",
          }); });
          return;
        }
        reportTerminal(state);
      })
      .catch(function () { reportTerminal({
        state: "link-lost", reason: "org-unreachable",
      }); });
  }

  // ---- accepting --------------------------------------------------------
  // The join session, once connected. Held so the page can be closed and
  // reopened on the same link without re-deriving anything it already knows.
  var session = null;

  function show(id, on) {
    var node = $(id);
    if (node) node.classList[on ? "remove" : "add"]("hidden");
  }

  function article(word) {
    return /^[aeiou]/i.test(String(word || "")) ? "an" : "a";
  }

  function say(id, text) {
    var node = $(id);
    if (node) node.textContent = text;
  }

  // The org answered: we now know the role, so the action can appear.
  function offerAccept(context) {
    var role = context && context.grantedRole;
    if (!role) return;
    say("joins-line", "You would join as " + article(role) + " " + role + ".");
    show("accept-block", true);
  }

  function showWaiting(orgName) {
    show("accept-block", false);
    show("waiting-block", true);
    say("waiting-line", "Waiting for " + (orgName || "the organization")
      + " to approve you");
  }

  function showAdmitted(orgName, role) {
    show("accept-block", false);
    show("waiting-block", false);
    show("done-block", true);
    say("done-line", "You joined " + (orgName || "the organization")
      + (role ? " as " + article(role) + " " + role : "") + ".");
  }

  // One live join session over the org's own channel. Every module is
  // imported here rather than at load: this page is a classic script, and
  // nothing below is needed until someone actually accepts.
  function connectSession(inputs) {
    return Promise.all([
      import("/static/js/join/accept-controller.js"),
      import("/static/js/join/channel-factory.js"),
      import("/static/js/join/ceremony.js"),
      import("/static/js/ceremony/open-root.js"),
    ]).then(function (mods) {
      var JoinSession = mods[0].JoinSession;
      var openChannel = mods[1].openChannel;
      var makeRootCeremony = mods[2].makeRootCeremony;
      var openRoot = mods[3].openRoot;
      session = new JoinSession({
        inputs: inputs,
        openChannel: openChannel,
        runCeremony: makeRootCeremony({ openRoot: openRoot }),
      });
      return session.connect();
    });
  }

  function reportTerminal(state) {
    show("accept-block", false);
    show("waiting-block", false);
    say("accept-hint", state && state.reason
      ? "This invitation cannot be used: " + state.reason
      : "This invitation cannot be used.");
    show("accept-block", true);
    var button = $("accept");
    if (button) button.classList.add("hidden");
  }

  function wireAccept(inputs) {
    var button = $("accept");
    if (!button) return;
    button.addEventListener("click", function () {
      button.disabled = true;
      say("accept-hint", "");
      var brandName = ($("org-name") && $("org-name").textContent) || "";
      var role = (session && session.brand && session.grantedRole) || null;
      session.accept()
        .then(function (result) {
          // Cancelled ceremony: nothing was signed and nothing was sent.
          if (result === null) { button.disabled = false; return null; }
          if (result.state === "pending") { showWaiting(brandName); return poll(brandName, role); }
          if (result.state === "admitted") { showAdmitted(brandName, role); return null; }
          if (result.state === "already-approved") { return finalize(brandName, role); }
          reportTerminal(result);
          return null;
        })
        .catch(function (error) {
          button.disabled = false;
          say("accept-hint", (error && error.message) || String(error));
        });
    });
  }

  // Approval is somebody else's action, so this waits rather than asking the
  // user to. Admission needs a second submit carrying the countersignature,
  // which finalize() does; accept() would discard it.
  function poll(brandName, role) {
    return session.pollUntilTerminal({
      onProgress: function () {},
    }).then(function (result) {
      if (!result) return null;
      if (result.state === "already-approved") return finalize(brandName, role);
      if (result.state === "admitted") { showAdmitted(brandName, role); return null; }
      if (result.state === "pending" || result.state === "pending-timeout") return null;
      reportTerminal(result);
      return null;
    });
  }

  function finalize(brandName, role) {
    return session.finalize().then(function (result) {
      if (result && result.state === "admitted") { showAdmitted(brandName, role); return null; }
      showWaiting(brandName);
      return poll(brandName, role);
    });
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

  if (typeof module === "object" && module.exports) {
    module.exports = {
      safeIcon: safeIcon, safeColor: safeColor, looksComplete: looksComplete,
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
