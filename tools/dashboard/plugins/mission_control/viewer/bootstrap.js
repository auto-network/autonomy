/* Mission viewer bootstrap.
 *
 * Prepended to the coordinator's complete HTML by _resolve_mission, so the
 * browser parses ONE document and their page keeps its own lifecycle: their
 * scripts run, their DOMContentLoaded and load fire, their in-page anchors
 * scroll. Nothing here rewrites their markup.
 *
 * Everything is inside this IIFE. Coordinator scripts share the realm, so a
 * public handle would let them issue channel operations -- the sandbox would
 * then mean only "cannot read the parent's DOM".
 */
(function () {
  "use strict";

  var SCRIPTS_AT_BOOT = document.scripts.length;   // 1 == we are first
  var CSS = "/*__CHROME_CSS__*/";                  // inlined by the build

  // ---- state --------------------------------------------------------------
  // Delivered as an inline JSON block by the composer. It is in the same
  // response as the document, so there is no round trip to save by fetching.
  var state = {pillars: [], questions: [], screen: null, presence: []};
  try {
    var stateEl = document.getElementById("mc-state");
    if (stateEl) state = Object.assign(state, JSON.parse(stateEl.textContent));
  } catch (_e) { /* a malformed block must not take the page down */ }

  var ui = {panel: null, entry: null, anchor: null, who: false};

  // ---- transport ----------------------------------------------------------
  var chan = new MessageChannel();
  var port = chan.port1;                 // never leaves this closure
  var nextId = 0;
  var pending = Object.create(null);

  port.onmessage = function (e) {
    var m = e.data;
    if (!m || m.v !== 1) return;
    if (m.type === "response" && pending[m.id]) {
      var p = pending[m.id]; delete pending[m.id];
      m.ok ? p.resolve(m.body) : p.reject(new Error(m.error || "refused"));
    } else if (m.type === "event") {
      applyEvent(m.body);
    }
  };
  parent.postMessage({v: 1, op: "ready"}, "*", [chan.port2]);

  function request(op, body) {
    return new Promise(function (resolve, reject) {
      var id = "r" + (++nextId);
      pending[id] = {resolve: resolve, reject: reject};
      port.postMessage({v: 1, type: "request", id: id, op: op, body: body});
    });
  }

  // ---- live updates -------------------------------------------------------
  function applyEvent(body) {
    if (!body || body.kind !== "conversation") return;
    var q = body.question;
    if (!q || !q.entry_id) return;
    var i = state.questions.findIndex(function (x) { return x.entry_id === q.entry_id; });
    if (i === -1) state.questions.push(q); else state.questions[i] = q;
    render();
  }

  // ---- chrome, in a CLOSED shadow root ------------------------------------
  var host = document.createElement("div");
  host.setAttribute("data-mission-chrome", "");
  var root = host.attachShadow({mode: "closed"});
  root.innerHTML = "<style>" + CSS + "</style><div id='chrome'></div>";
  var chrome = root.getElementById("chrome");

  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    for (var k in (attrs || {})) {
      if (k === "class") n.className = attrs[k];
      else if (k.slice(0, 2) === "on") n.addEventListener(k.slice(2), attrs[k]);
      // Runtime values are DATA. textContent, never innerHTML -- question
      // text, display names and presence fields are untrusted input no matter
      // who authored the page they land in.
      else if (k === "text") n.textContent = attrs[k];
      else n.setAttribute(k, attrs[k]);
    }
    (kids || []).forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }

  function currentPillar() {
    var id = ui.screenId || state.screen;
    return state.pillars.filter(function (p) { return p.pillar_id === id; })[0] || null;
  }
  function openCount() {
    return state.questions.filter(function (q) { return !q.answer; }).length;
  }
  function atAnchor(a) {
    return state.questions.filter(function (q) { return q.anchor === a; });
  }

  function render() { /* chrome markup lands here -- see auto-vson8 */ }

  // ---- anchored controls --------------------------------------------------
  // Mounted INSIDE the anchored element, never as a sibling: a next sibling
  // breaks the author's adjacent-sibling (+) CSS rules. Each gets its own
  // closed root so author CSS cannot restyle it either.
  function mountAnchors() {
    var n = 0;
    document.querySelectorAll("[data-mc-anchor]").forEach(function (target) {
      if (target.querySelector("[data-mc-control]")) return;   // idempotent
      var ref = target.getAttribute("data-mc-anchor");
      var holder = document.createElement("span");
      holder.setAttribute("data-mc-control", "");
      var r = holder.attachShadow({mode: "closed"});
      r.innerHTML = "<style>" + CSS + "</style>";
      var here = atAnchor(ref);
      var count = here.length;
      var open = here.some(function (q) { return !q.answer; });
      var btn = el("button", {
        class: "mc-anchor-btn", title: "Discuss",
        onclick: function () { ui.anchor = ref; render(); },
      });
      btn.innerHTML =
        '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
        '<path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 8.5 8.5 0 0 1-3.8-.9L3 21l1.9-5.1A8.4 8.4 0 0 1 12 3a8.4 8.4 0 0 1 9 8.5z"/></svg>';
      if (count) {
        // icon alone when nothing is there; icon + count when there is;
        // coloured only while something is still open.
        btn.appendChild(el("span", {
          class: open ? "mc-count mc-count-open" : "mc-count",
          text: String(count),
        }));
      }
      r.appendChild(btn);
      target.appendChild(holder);
      n++;
    });
    return n;
  }

  // ---- navigation ---------------------------------------------------------
  function goto(pillarId) {
    return request("read", {kind: "pillar_site", pillar_id: pillarId})
      .then(function (screen) {
        if (!screen || typeof screen.document !== "string") return;
        document.open();
        document.write(screen.document);
        document.close();
        // document.write INHERITS the previous scroll offset (measured: 1214px
        // carried), so without this every screen opens mid-page.
        window.scrollTo(0, 0);
        // The replacement document runs this bootstrap again and opens its own
        // port; autonet.js drops the previous one.
      });
  }

  function start() {
    document.body.appendChild(host);
    mountAnchors();
    render();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else { start(); }

  // Exposed for tests only, via the port -- never on window.
  port.postMessage({v: 1, type: "boot", scriptsAtBoot: SCRIPTS_AT_BOOT});
})();
