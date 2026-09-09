/* The accept-invitation flow (auto-1ihgz, auto-yw5gz).
 *
 * Stepped, as a user walks it: opened bare, the page IS the paste step —
 * a full screen with a visible field and a Next button (parsing via the
 * network-free accept-invitation.js module; the pasted value never leaves
 * this page). Paste a relay share link (/l/<token>) and this dashboard
 * resolves it on its OWN origin instead of bouncing to the relay bridge:
 * it hands the resolve endpoint the transport credentials only
 * ({relay_host, channel_token}) and the endpoint opens the org tunnel and
 * returns the verified org context. Opened with a handoff link's context
 * in the URL, the page is the organization step directly.
 *
 * The bearer (#t=) is HELD in this page and never sent to any server,
 * including our own dashboard — it is the ceremony's secret and stays in
 * the browser (I1-adjacent). Controls appear only when their function
 * exists: the accept action appears once the organization has answered
 * over its own tunnel and said what you would be joining as. Accepting
 * opens the SHARED root control, which presents whichever factors this
 * identity has. No password anything, ever, on this page (I1).
 */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  // The ledger bearer, held ONLY in this closure for the future ceremony.
  // It is never written to storage and never placed in a request body —
  // the resolve call below carries transport credentials and nothing else.
  var heldBearer = "";

  function readInputs() {
    var query = new URLSearchParams(location.search);
    var fragment = new URLSearchParams(location.hash.replace(/^#/, ""));
    return {
      org: query.get("org") || "",
      rootPub: query.get("root_pub") || "",
      inviteRef: query.get("invite_ref") || "",
      relayHost: query.get("relay_host") || "",
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
    if (name) $("org-name").textContent = name;
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
    if (accent) $("org-header").style.borderColor = accent;
  }

  // Fill the minimal verified-org step from whatever public context we hold —
  // org id + invite ref. This stands on its own when the org is unreachable.
  function fillOrgStep(context) {
    $("step-org").classList.remove("hidden");
    if (context.org) {
      $("org-id").textContent = context.org;
      $("org-fallback").textContent =
        context.org.slice(0, 1).toUpperCase() || "?";
    }
    var ref = context.invite_ref || context.inviteRef || "";
    if (ref) $("invite-ref").textContent = ref.slice(0, 16) + "…";
  }

  // The one network hop, to THIS dashboard's own origin. The body is the
  // transport credentials only; when the handoff link already carried the
  // public org/root_pub/invite_ref, those ride along so the endpoint can pin
  // without a second registry hop. The bearer is deliberately absent.
  function resolveOnOrigin(relayHost, channelToken, known) {
    var body = { relay_host: relayHost, channel_token: channelToken };
    if (known && known.org) {
      body.org = known.org;
      body.root_pub = known.rootPub;
      body.invite_ref = known.inviteRef;
    }
    return fetch("/api/network/invite/resolve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) { return r.json(); });
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
        // A relay share link resolves in place on this origin — no bounce to
        // the relay bridge. The bearer is held here and never sent.
        resolve: function (parsed) {
          heldBearer = parsed.bearer;
          $("step-paste").classList.add("hidden");
          fillOrgStep({});
          resolveOnOrigin(parsed.relayHost, parsed.channelToken)
            .then(function (reply) {
              if (reply && reply.ok) {
                fillOrgStep(reply);
                renderVerifiedHeader(reply);
              }
            })
            .catch(function () {
              // Resolve unreachable: the minimal step stands. Never an error.
            });
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
  function showOrgStepFromUrl(inputs) {
    heldBearer = inputs.bearer;
    fillOrgStep({ org: inputs.org, inviteRef: inputs.inviteRef });
    if (inputs.relayHost && /^[0-9a-f]{32}$/.test(inputs.channelToken)) {
      resolveOnOrigin(inputs.relayHost, inputs.channelToken, inputs)
        .then(function (reply) {
          if (reply && reply.ok) renderVerifiedHeader(reply);
        })
        .catch(function () {});
    }
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
              org_icon: session.brand.orgIcon,
            });
          }
          session.grantedRole = state.grantedRole;
          offerAccept(state);
          wireAccept(inputs);
          return;
        }
        reportTerminal(state);
      })
      .catch(function () { /* unreachable org: the minimal step stands */ });
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
    showOrgStepFromUrl(inputs);
  }

  if (typeof module === "object" && module.exports) {
    module.exports = { safeIcon: safeIcon, safeColor: safeColor };
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", render);
    } else {
      render();
    }
  }
})();
