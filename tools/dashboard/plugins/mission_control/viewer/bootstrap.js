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

  // The chrome shows at most one surface at a time. Every open goes through
  // here so a bar control tapped while a discussion is up switches to it
  // rather than opening a panel underneath the view that covers it.
  function show(next) {
    ui.panel = null; ui.entry = null; ui.anchor = null; ui.who = false;
    if (next) Object.assign(ui, next);
    render();
  }

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

  function svg(path) {
    var n = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    n.setAttribute("viewBox", "0 0 24 24"); n.setAttribute("width", "13");
    n.setAttribute("height", "13"); n.setAttribute("fill", "none");
    n.setAttribute("stroke", "currentColor"); n.setAttribute("stroke-width", "2");
    var d = document.createElementNS("http://www.w3.org/2000/svg", "path");
    d.setAttribute("d", path); n.appendChild(d); return n;
  }
  var CHAT = "M21 11.5a8.4 8.4 0 0 1-9 8.4 8.5 8.5 0 0 1-3.8-.9L3 21l1.9-5.1A8.4 8.4 0 0 1 12 3a8.4 8.4 0 0 1 9 8.5z";

  function face(p, small) {
    var f = el("span", {class: small ? "mc-face mc-face-sm" : "mc-face", text: p.initial || "?"});
    f.style.background = (p.color || "#64748b") + "33";
    f.style.color = p.color || "#94a3b8";
    return f;
  }

  // On a pillar screen, who is on THAT pillar; on the mission overview, who
  // is on the mission. Both are real presence surfaces (pillar:<id> and
  // mission:<id>), so neither is a stand-in for the other.
  function presentHere() {
    var p = currentPillar();
    return (p ? p.here : state.here) || [];
  }

  function barRow() {
    var p = currentPillar() || {};
    var here = presentHere();
    var swatch = el("span", {class: "mc-swatch"});
    swatch.style.background = p.color || "#475569";
    var kids = [
      el("button", {class: "mc-pill", onclick: function () { show(ui.panel === "pillars" ? null : {panel: "pillars"}); }},
         [swatch, el("span", {class: "mc-name", text: p.name || "Mission"}), el("span", {class: "mc-caret", text: "\u25be"})]),
      el("span", {class: "mc-age", text: p.age || ""}),
      el("span", {class: "mc-grow"}),
    ];
    var n = openCount();
    var q = el("button", {class: n ? "mc-q mc-q-hot" : "mc-q",
                          onclick: function () { show(ui.panel === "questions" ? null : {panel: "questions"}); }},
               [svg("M12 8v5M12 16.5v.5"), el("span", {text: n ? n + " open" : "Q&A"})]);
    kids.push(q);
    if (here.length) {
      var cluster = el("button", {class: "mc-faces", onclick: function () { show(ui.who ? null : {who: true}); }},
                       here.map(function (h) { return face(h); }));
      kids.push(cluster);
    } else {
      kids.push(el("span", {class: "mc-nobody", text: "nobody here"}));
    }
    return el("header", {class: "mc-bar"}, kids);
  }

  function whoList() {
    var here = presentHere();
    return el("div", {class: "mc-who"},
      [el("p", {class: "mc-label", text: here.length + " here"})].concat(
        here.map(function (h) {
          return el("div", {class: "mc-who-row"}, [
            face(h),
            el("span", {class: "mc-who-name", text: h.label || ""}),
            el("span", {class: "mc-who-seen", text: h.seen || ""}),
          ]);
        })));
  }

  function pillarRows() {
    return state.pillars.map(function (p) {
      var sw = el("span", {class: "mc-swatch"}); sw.style.background = p.color || "#475569";
      var faces = el("span", {class: "mc-faces"}, (p.here || []).map(function (h) { return face(h, true); }));
      var meta = [el("span", {class: "mc-age", text: p.age || ""})];
      if (p.open) meta.push(el("span", {class: "mc-open", text: p.open + " open"}));
      if (!(p.here || []).length) meta.push(el("span", {class: "mc-nobody", text: "nobody here"}));
      return el("button", {
        class: p.pillar_id === (ui.screenId || state.screen) ? "mc-row mc-row-on" : "mc-row",
        onclick: function () { goto(p.pillar_id); },
      }, [
        el("div", {class: "mc-row-top"}, [sw, el("span", {class: "mc-name", text: p.name || ""}), faces]),
        // at most two sentences, no jargon: the last productive thing done
        el("p", {class: "mc-last", text: p.last || ""}),
        el("div", {class: "mc-row-meta"}, meta),
      ]);
    });
  }

  function questionRows() {
    var sorted = state.questions.slice().sort(function (a, b) { return (!!a.answer) - (!!b.answer); });
    if (!sorted.length) {
      return [el("p", {class: "mc-empty", text: "No questions yet. Ask the first one below."})];
    }
    return sorted.map(function (q) {
      return el("button", {class: "mc-row", onclick: function () { show({entry: q.entry_id}); }}, [
        el("div", {class: "mc-row-top"}, [
          el("span", {class: q.answer ? "mc-chip" : "mc-chip mc-chip-open", text: q.answer ? "answered" : "open"}),
          el("span", {class: "mc-sub", text: pillarName(q.pillar_id)}),
        ]),
        el("p", {class: "mc-qtext", text: q.question || ""}),
        el("p", {class: "mc-sub", text: q.asked_by_label || ""}),
      ]);
    });
  }

  function composer(placeholder, note, label, onSend) {
    var ta = el("textarea", {class: "mc-ta", rows: "2", placeholder: placeholder});
    return el("div", {class: "mc-foot"}, [
      ta,
      el("div", {class: "mc-foot-row"}, [
        el("span", {class: "mc-sub", text: note}),
        el("button", {class: "mc-send", text: label,
                      onclick: function () { if (ta.value.trim()) onSend(ta.value.trim()); }}),
      ]),
    ]);
  }

  function pillarName(id) {
    var p = state.pillars.filter(function (x) { return x.pillar_id === id; })[0];
    return p ? p.name : "Mission";
  }

  function panelNode() {
    if (!ui.panel) return null;
    var body = ui.panel === "pillars" ? pillarRows() : questionRows();
    var kids = [
      el("div", {class: "mc-phead"}, [
        el("span", {class: "mc-sub", text: ui.panel === "pillars" ? "Pillars" : "Questions"}),
        el("span", {class: "mc-grow"}),
        el("button", {class: "mc-x", text: "\u00d7", onclick: function () { show(null); }}),
      ]),
      el("div", {class: "mc-pbody"}, body),
    ];
    if (ui.panel === "questions") {
      kids.push(composer("Ask about " + (currentPillar() || {}).name + "\u2026",
                         "goes to " + (currentPillar() || {}).name, "Ask", ask));
    }
    return el("aside", {class: "mc-panel"}, kids);
  }

  function entryNode() {
    if (!ui.entry) return null;
    var q = state.questions.filter(function (x) { return x.entry_id === ui.entry; })[0];
    if (!q) return null;
    var body = [
      el("p", {class: "mc-label", text: "Question"}),
      el("p", {class: "mc-qbig", text: q.question || ""}),
      el("p", {class: "mc-sub", text: (q.asked_by_label || "") + (q.created_at ? " \u00b7 " + q.created_at : "")}),
    ];
    if (q.anchor) body.push(el("p", {class: "mc-sub", text: q.anchor}));
    if (!q.answer && (q.updates || []).length) {
      body.push(el("p", {class: "mc-label mc-mt", text: "While this is open"}));
      (q.updates || []).forEach(function (u) { body.push(el("p", {class: "mc-update", text: u.text || ""})); });
    }
    if (q.answer) {
      body.push(el("p", {class: "mc-label mc-mt", text: "Answer"}));
      body.push(el("p", {class: "mc-answer", text: q.answer}));
    }
    return el("section", {class: "mc-view"}, [
      el("div", {class: "mc-phead"}, [
        el("button", {class: "mc-back", text: "\u2039 Back to " + ((currentPillar() || {}).name || "mission"),
                      onclick: function () { show(null); }}),
        el("span", {class: "mc-grow"}),
        el("span", {class: q.answer ? "mc-chip" : "mc-chip mc-chip-open", text: q.answer ? "answered" : "open"}),
      ]),
      el("div", {class: "mc-pbody"}, body),
      composer(q.answer ? "Reopen with a follow-up\u2026" : "Answer\u2026", "", q.answer ? "Reopen" : "Answer",
               function (t) { reply(q.entry_id, t, !!q.answer); }),
    ]);
  }

  function anchorNode() {
    if (!ui.anchor) return null;
    var here = atAnchor(ui.anchor);
    var body = [el("p", {class: "mc-label", text: "about"}), el("p", {class: "mc-sub mc-mb", text: ui.anchor})];
    if (!here.length) body.push(el("p", {class: "mc-empty", text: "Nothing discussed here yet."}));
    here.forEach(function (q) {
      body.push(el("button", {class: "mc-row", onclick: function () { show({entry: q.entry_id}); }}, [
        el("span", {class: q.answer ? "mc-chip" : "mc-chip mc-chip-open", text: q.answer ? "answered" : "open"}),
        el("p", {class: "mc-qtext", text: q.question || ""}),
      ]));
    });
    return el("section", {class: "mc-view"}, [
      el("div", {class: "mc-phead"}, [
        el("button", {class: "mc-back", text: "\u2039 Back to " + ((currentPillar() || {}).name || "mission"),
                      onclick: function () { show(null); }}),
      ]),
      el("div", {class: "mc-pbody"}, body),
      composer("Ask about this\u2026", "tagged " + ui.anchor, "Ask",
               function (t) { ask(t, ui.anchor); }),
    ]);
  }

  // Identity is NEVER supplied here. participant_id lives in the grant and is
  // attached by link_serving; there is no field in this request to claim one.
  function ask(text, anchor) {
    var body = {kind: "question", body: text};
    var p = currentPillar();
    if (p) body.pillar_id = p.pillar_id;
    if (anchor) body.anchor = anchor;
    return request("write", body).then(function () { show(null); });
  }
  function reply(entryId, text, reopen) {
    return request("write", {kind: reopen ? "reopen" : "answer", entry_id: entryId, body: text})
      .then(function () { render(); });
  }

  function render() {
    chrome.textContent = "";
    chrome.appendChild(barRow());
    if (ui.who) chrome.appendChild(whoList());
    var p = panelNode(); if (p) chrome.appendChild(p);
    var e = entryNode(); if (e) chrome.appendChild(e);
    var a = anchorNode(); if (a) chrome.appendChild(a);
  }

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
        onclick: function () { show({anchor: ref}); },
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
    // FIRST child, and the bar is sticky rather than fixed, so it reserves
    // its own 3rem in normal flow instead of covering the top of whatever
    // the coordinator wrote. Measured on the real 1.87MB pillar page: a
    // fixed bar ate its opening heading.
    document.body.insertBefore(host, document.body.firstChild);
    // The one property we set on the author's document. A sticky bar still
    // overlays whatever a #fragment jump lands on, and working fragment
    // links are the whole reason this document carries a <base>. This
    // affects scroll landing only -- nothing about how their page paints.
    document.documentElement.style.scrollPaddingTop = "3rem";
    mountAnchors();
    render();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else { start(); }

  // Exposed for tests only, via the port -- never on window.
  port.postMessage({v: 1, type: "boot", scriptsAtBoot: SCRIPTS_AT_BOOT});
})();
