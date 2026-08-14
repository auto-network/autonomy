/*
 * Verified-org brand rendering — the display half of the org self-describe
 * read (auto-r7kk4 fields), owned by the packaging pillar; the join-page
 * controller (crypto, Model B) imports it and passes a container.
 *
 * Contract: renderVerifiedOrg(containerEl, contextReply) paints the
 * organization's verified identity INSIDE containerEl and returns true, or
 * returns false without touching the DOM when the reply carries no usable
 * name — the caller keeps its minimal display. The module never reaches
 * fixed ids, never touches the network, and never sees credentials; every
 * identity field passes the same guards the public bridge enforces:
 * bounded data:image/*;base64 icons only, bare six-digit hex colors only,
 * clamped text lengths, name-only/initial fallbacks.
 */
(function (root) {
  "use strict";

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

  function clampName(value) {
    return typeof value === "string" ? value.slice(0, 120) : "";
  }

  function clampByline(value) {
    return typeof value === "string" ? value.slice(0, 300) : "";
  }

  function renderVerifiedOrg(containerEl, contextReply) {
    if (!containerEl || !contextReply) return false;
    var name = clampName(contextReply.org_name);
    if (!name) return false;

    var doc = containerEl.ownerDocument;
    containerEl.textContent = "";

    var head = doc.createElement("div");
    head.className = "verified-org";

    var icon = safeIcon(contextReply.org_icon);
    if (icon) {
      var img = doc.createElement("img");
      img.className = "verified-org-icon";
      img.src = icon;
      img.alt = "";
      head.appendChild(img);
    } else {
      var initial = doc.createElement("div");
      initial.className = "verified-org-initial";
      initial.textContent = (name.charAt(0) || "?").toUpperCase();
      head.appendChild(initial);
    }

    var copy = doc.createElement("div");
    copy.className = "verified-org-copy";
    var nameEl = doc.createElement("div");
    nameEl.className = "verified-org-name";
    nameEl.textContent = name;
    copy.appendChild(nameEl);
    var byline = clampByline(contextReply.org_description);
    if (byline) {
      var bylineEl = doc.createElement("div");
      bylineEl.className = "verified-org-byline";
      bylineEl.textContent = byline;
      copy.appendChild(bylineEl);
    }
    head.appendChild(copy);

    var accent = safeColor(contextReply.org_color);
    if (accent) containerEl.style.borderColor = accent;

    containerEl.appendChild(head);
    return true;
  }

  var api = {
    renderVerifiedOrg: renderVerifiedOrg,
    safeIcon: safeIcon,
    safeColor: safeColor,
    clampName: clampName,
    clampByline: clampByline,
  };
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.AutonomyVerifiedOrg = api;
})(typeof window !== "undefined" ? window : null);
