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
  // THE HOST OWNS THE PORT. We announce ready and it hands one back; we do
  // not create a channel and offer it. Transferring our own port to the host
  // looks symmetric and is not: the host ignores it, keeps its half of a
  // channel we never hear about, and every request becomes a promise that
  // resolves for nobody. The document still renders, because its state is
  // inlined -- so the failure is invisible until someone taps something.
  var port = null;
  var nextId = 0;
  var pending = Object.create(null);
  var resolvePort;
  var havePort = new Promise(function (r) { resolvePort = r; });

  // What "ok" means at each layer, and why this check has to exist HERE.
  // The host's broker carries opaque application messages by design -- it
  // delivers whatever came back and marks it ok:true because the exchange
  // itself succeeded. A refusal IS a successful exchange at that layer. So a
  // server saying {"status":"unavailable"} arrived as a resolved promise,
  // the ask closed the panel and cleared the box, and nothing was written:
  // a tap that looked like it worked. Only the application knows that
  // status is the answer.
  function settle(p, body) {
    var status = body && body.status;
    if (status && status !== "ok") {
      p.reject(new Error(status === "unavailable"
        ? "this link cannot post to the mission"
        : String(status)));
      return;
    }
    p.resolve(body);
  }

  function onPortMessage(e) {
    var m = e.data;
    if (!m || m.v !== 1) return;
    if (m.type === "response" && pending[m.id]) {
      var p = pending[m.id]; delete pending[m.id];
      m.ok ? settle(p, m.body) : p.reject(new Error(m.error || "refused"));
    } else if (m.type === "event") {
      applyEvent(m.body);
    }
  }

  addEventListener("message", function (e) {
    if (e.source !== parent || !e.data || e.data.v !== 1) return;
    if (e.data.op !== "port" || !e.ports || !e.ports.length) return;
    port = e.ports[0];                   // never leaves this closure
    port.onmessage = onPortMessage;
    if (typeof port.start === "function") port.start();
    resolvePort(port);
  });

  parent.postMessage({v: 1, op: "ready"}, "*");
  // If the host never hands a port over, every control in this chrome is a
  // promise that resolves for nobody. Say so on the bar rather than looking
  // fine and doing nothing -- a silent dead control costs more to diagnose
  // than any amount of visible degradation.
  // ONLY IN A FRAME. At a real URL no port is ever transferred and none is
  // wanted -- the transport is HTTP. Arming this timer everywhere meant the
  // dashboard told the operator "Not connected. Posting is disabled." six
  // seconds after every load, hid the composer, and flagged the bar "no link",
  // while the HTTP path underneath worked perfectly. The transport was fixed
  // and the question "is there a transport?" went on asking about the old one.
  if (window.parent !== window) {
    setTimeout(function () {
      if (!port) { ui.noChannel = true; render(); }
    }, 6000);
  }
  // This document renders its own top bar, so it asks the shell for the
  // viewport instead of sitting under a second one. The shell decides
  // whether to honour it; nothing here depends on the answer.
  parent.postMessage({v: 1, op: "chrome", own: true}, "*");

  // Waits for the host's port rather than assuming one is already here: a tap
  // can land before the handover completes, and dropping that request would
  // look exactly like a dead control.
  // Tell the mission we are reading it, and take back the current list of who
  // else is. Presence otherwise only ever recorded sessions that PUSH, so the
  // people a mission is written FOR never appeared on it at all.
  function announceHere() {
    var p = currentPillar();
    var body = {kind: "here"};
    if (p) body.pillar_id = p.pillar_id;
    return request("read", body).then(function (r) {
      if (!r || !r.presence) return;
      if (p) { p.here = r.presence; } else { state.here = r.presence; }
      render();
    }, function () { /* a refused touch costs nothing and shows nothing */ });
  }

  // TWO SURFACES, TWO TRANSPORTS -- the same split goto() makes, for the same
  // reason. In a frame the host hands us a MessagePort. Served at a real URL
  // NOBODY EVER DOES, so `havePort` never resolves and every ask, answer and
  // reopen became a promise waiting on a channel that does not exist. The
  // document still rendered, because its state is inlined -- so the dashboard
  // looked completely healthy and the compose button did nothing at all,
  // forever, with no error anywhere. goto() was taught this split and request()
  // was not, which is the whole of that bug.
  function request(op, body) {
    if (window.parent === window) return httpRequest(op, body);
    return havePort.then(function (p) {
      return new Promise(function (resolve, reject) {
        var id = "r" + (++nextId);
        pending[id] = {resolve: resolve, reject: reject};
        p.postMessage({v: 1, type: "request", id: id, op: op, body: body});
      });
    });
  }

  // The same operations against the real routes. Identity is still NEVER in
  // the payload: at a real URL the dashboard session cookie rides the request
  // and the server resolves who that is, exactly as the grant carries it over
  // the relay. There is no field here to claim one.
  function httpRequest(op, body) {
    var mid = state.mission_id || "";
    var kind = body && body.kind;

    // Which collection an entry belongs to is a property OF THE ENTRY, not of
    // whatever screen happens to be open -- answering from the overview must
    // still hit the pillar's route.
    function ownerPath(entryId) {
      var q = (state.questions || []).filter(function (x) {
        return x.entry_id === entryId;
      })[0];
      return q && q.pillar_id
        ? "/api/pillars/" + q.pillar_id
        : "/api/missions/" + mid;
    }

    var url, payload;
    if (op === "write" && kind === "question") {
      url = (body.pillar_id ? "/api/pillars/" + body.pillar_id
                            : "/api/missions/" + mid) + "/questions";
      payload = {question: body.body, anchor: body.anchor || null};
    } else if (op === "write" && kind === "answer") {
      url = ownerPath(body.entry_id) + "/questions/" + body.entry_id + "/answer";
      payload = {answer: body.body};
    } else if (op === "write" && kind === "reopen") {
      url = ownerPath(body.entry_id) + "/questions/" + body.entry_id + "/reopen";
      payload = {followup: body.body};
    } else if (op === "read" && kind === "here") {
      // Presence has no HTTP route yet; it is decoration, and announceHere
      // already treats an empty answer as "show nothing". Resolving quietly
      // beats inventing a route from the client.
      return Promise.resolve({});
    } else {
      // Screens are reached by navigating, not by fetching, when there is a
      // URL to navigate to -- goto() branches before ever getting here.
      return Promise.reject(new Error("no top-level transport for " + op + "/" + kind));
    }

    return fetch(url, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      // The dashboard session cookie is the whole identity story here.
      credentials: "same-origin",
      body: JSON.stringify(payload),
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        // A REFUSAL IS NOT A SUCCESS. The relay broker marks a completed
        // exchange ok:true even when the application refused, which once made
        // a rejected ask close the panel and clear the box as though it had
        // worked. HTTP hands us a real status; throwing on it is what keeps
        // the two transports behaving identically at the surface.
        if (!res.ok) {
          throw new Error(data && data.error ? data.error : "HTTP " + res.status);
        }
        return data;
      });
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
      // Guard against the whole class of bug this belongs to: a missing
      // value concatenated into a string renders as the WORD "undefined",
      // which looks like content and reads like a defect.
      else if (k === "text") {
        var v = attrs[k];
        n.textContent = (v === undefined || v === null) ? "" : String(v);
      }
      else n.setAttribute(k, attrs[k]);
    }
    (kids || []).forEach(function (c) { if (c) n.appendChild(c); });
    return n;
  }

  function currentPillar() {
    var id = ui.screenId || state.screen;
    return state.pillars.filter(function (p) { return p.pillar_id === id; })[0] || null;
  }
  // The bar describes the screen you are on. On a pillar that means that
  // pillar's questions; on the overview it means all of them. A count that
  // never changes as you navigate is not telling you anything about where
  // you are.
  function questionsHere() {
    var p = currentPillar();
    if (!p) return state.questions;
    return state.questions.filter(function (q) { return q.pillar_id === p.pillar_id; });
  }
  function openCount() {
    return questionsHere().filter(function (q) { return !q.answer; }).length;
  }
  function answeredCount() {
    return questionsHere().filter(function (q) { return !!q.answer; }).length;
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
         [swatch, el("span", {class: "mc-name", text: p.name || state.mission || "Mission"}), el("span", {class: "mc-caret", text: "\u25be"})]),
      el("span", {class: "mc-age", text: p.age || ""}),
      el("span", {class: "mc-grow"}),
    ];
    var n = openCount(), done = answeredCount();
    // The bubble says what this is; the numbers say how much of it there is.
    // Open is the count that wants a human, so it is the only coloured one.
    var qKids = [svg(CHAT)];
    if (n || done) {
      qKids.push(el("span", {class: n ? "mc-count mc-count-open" : "mc-count",
                             title: n + " open", text: String(n)}));
      qKids.push(el("span", {class: "mc-count mc-count-done",
                             title: done + " answered", text: String(done)}));
    }
    var q = el("button", {class: n ? "mc-q mc-q-hot" : "mc-q", title: "Questions",
                          onclick: function () { show(ui.panel === "questions" ? null : {panel: "questions"}); }},
               qKids);
    kids.push(q);
    if (ui.noChannel) {
      kids.push(el("span", {class: "mc-nobody", title:
        "This page cannot reach the mission. Navigation and questions are unavailable.",
        text: "no link"}));
    }
    if (here.length) {
      var cluster = el("button", {class: "mc-faces", onclick: function () { show(ui.who ? null : {who: true}); }},
                       here.map(function (h) { return face(h); }));
      kids.push(cluster);
    } else {
      kids.push(el("span", {class: "mc-nobody", text: "nobody here"}));
    }
    return el("header", {class: "mc-bar"}, kids);
  }

  // ---- section strip ------------------------------------------------------
  // Built from [data-mc-section] in the author's own markup. One optional
  // attribute: they decide what a section is and what it is called, the
  // platform decides how it behaves on a phone. No sections declared, no
  // strip -- nothing is imposed on a page that does not want one.
  var sections = [];
  var jumpedTo = -1, jumpedAt = 0;
  function collectSections() {
    sections = [];
    document.querySelectorAll("[data-mc-section]").forEach(function (el) {
      var label = (el.getAttribute("data-mc-section") || "").trim();
      if (label) sections.push({el: el, label: label});
    });
    return sections.length;
  }

  function stripNode() {
    if (!sections.length) return null;
    var strip = el("nav", {class: "mc-strip"}, sections.map(function (s, i) {
      return el("button", {
        class: "mc-chip", "data-i": String(i), text: s.label,
        onclick: function () {
          // Mark it now. Scroll-spy cannot: a smooth scroll has not moved
          // anywhere yet at the moment of the tap, so the previous section is
          // still the one under the line and the wrong pill lights up until
          // the reader nudges the page.
          jumpedTo = i; jumpedAt = Date.now();
          s.el.scrollIntoView({behavior: "smooth", block: "start"});
          syncStrip();
        },
      });
    }));
    return strip;
  }

  // The strip follows the reader: the chip for the section they are in is
  // marked, and the strip scrolls itself so that chip stays visible. Without
  // that second half a strip wider than the screen hides exactly the part
  // you need once you are past the third section.
  function syncStrip() {
    if (!sections.length) return;
    var strip = root.querySelector(".mc-strip");
    if (!strip) return;
    var top = barHeight() + 8;
    var active = 0;
    for (var i = 0; i < sections.length; i++) {
      if (sections[i].el.getBoundingClientRect().top <= top) active = i;
    }
    // A tap wins until the scroll it started has had time to land.
    if (jumpedTo >= 0 && Date.now() - jumpedAt < 1200) active = jumpedTo;
    else jumpedTo = -1;
    var chips = strip.querySelectorAll(".mc-chip");
    for (var j = 0; j < chips.length; j++) {
      var on = j === active;
      chips[j].className = on ? "mc-chip mc-chip-on" : "mc-chip";
      if (on) {
        var c = chips[j], want = c.offsetLeft - 12;
        if (Math.abs(strip.scrollLeft - want) > 4) {
          strip.scrollTo({left: Math.max(0, want), behavior: "smooth"});
        }
      }
    }
  }

  function barHeight() {
    var bar = root.querySelector(".mc-bar");
    return bar ? Math.round(bar.getBoundingClientRect().height) : 48;
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
    var onOverview = !(ui.screenId || state.screen);
    // The overview needs a colour tab like every pillar row has. Without one
    // it reads as a heading above the list rather than as a destination in it.
    var overviewSwatch = el("span", {class: "mc-swatch"});
    overviewSwatch.style.background = "#e2e8f0";
    var rows = [el("button", {
      class: onOverview ? "mc-row mc-row-on" : "mc-row",
      onclick: function () { show(null); if (!onOverview) goto(null); },
    }, [
      el("div", {class: "mc-row-top"}, [
        overviewSwatch,
        el("span", {class: "mc-name", text: state.mission || "Mission overview"}),
      ]),
      el("p", {class: "mc-last", text: "Mission overview and current status."}),
    ])];
    return rows.concat(state.pillars.map(function (p) {
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
        el("p", {class: "mc-last", text: p.last_done || ""}),
        el("div", {class: "mc-row-meta"}, meta),
      ]);
    }));
  }

  function questionRows() {
    var sorted = questionsHere().slice().sort(function (a, b) { return (!!a.answer) - (!!b.answer); });
    if (!sorted.length) {
      // Do not invite an action this reader cannot take.
      var canAsk = state.may_write !== false && !ui.noChannel;
      return [el("p", {class: "mc-empty", text: canAsk
        ? (currentPillar()
           ? "No questions on this screen yet. Ask the first one below."
           : "No questions yet. Ask the first one below.")
        : "No questions yet."})];
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
    // Focusing a textarea is what opens the keyboard, so that is the moment
    // the panel has to be re-measured -- the resize event alone can land
    // before the browser has settled on a height.
    ta.addEventListener("focus", function () { setTimeout(measureKeyboard, 250); });
    // Grow with what is typed. A fixed two rows meant a long question
    // scrolled inside a box the size of two lines, with the beginning of
    // your own sentence hidden above the fold while you wrote the end.
    function grow() {
      ta.style.height = "auto";
      ta.style.height = Math.min(ta.scrollHeight, window.innerHeight * 0.4) + "px";
    }
    ta.addEventListener("input", grow);
    setTimeout(grow, 0);
    // With no channel there is nothing to send to, and a button that looks
    // live and does nothing is worse than one that says so.
    // Three reasons a control cannot work, known BEFORE it is offered: no
    // channel at all, or a link carrying no identity to attribute a question
    // to. Both were previously discovered by tapping.
    // Say what is true, not how it works. The earlier wording explained
    // grant metadata to someone who just wanted to ask a question.
    var why = ui.noChannel
      ? "Not connected. Posting is disabled."
      : (state.may_write === false
         ? "Posting is disabled on this anonymous read-only link."
         : null);
    // A control you cannot use should not be there. Disabling the button
    // while leaving an inviting text box is worse than either: it opens the
    // keyboard, asks you to compose something, and then refuses to send it.
    if (why) return el("div", {class: "mc-foot"}, [el("p", {class: "mc-readonly", text: why})]);
    var status = el("span", {class: "mc-sub", text: why || note});
    var send = el("button", {
      class: "mc-send", text: label,
      onclick: function () {
        var text = ta.value.trim();
        if (!text) return;
        status.textContent = "Sending\u2026";
        Promise.resolve(onSend(text)).then(function () {
          ta.value = ""; grow(); status.textContent = note;
        }, function (err) {
          // Never silent. A refused write used to reject a promise nobody
          // was listening to, so the tap did nothing and said nothing.
          status.textContent = "Not sent: " + ((err && err.message) || "refused");
        });
      },
    });
    if (why) send.disabled = true;
    return el("div", {class: "mc-foot"}, [
      ta,
      el("div", {class: "mc-foot-row"}, [
        status, send,
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
        el("span", {class: "mc-ptitle", text: ui.panel === "pillars" ? "Pillars" : "Questions"}),
        el("span", {class: "mc-grow"}),
        el("button", {class: "mc-x", text: "\u00d7", onclick: function () { show(null); }}),
      ]),
      el("div", {class: "mc-pbody"}, body),
    ];
    if (ui.panel === "questions") {
      // On the overview there is no pillar to name, and `(x || {}).name` is
      // undefined, not absent -- string concatenation renders that as the
      // word. Say where the question actually goes instead.
      var target = (currentPillar() || {}).name || state.mission || "this mission";
      kids.push(composer("Ask about " + target + "\u2026",
                         "goes to " + target, "Ask", ask));
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

  // The bar sticks to the top of the VIEWPORT once scrolled, but sits below
  // the author's body padding before that. Panels dock to its real bottom
  // edge rather than to a constant, or they cover its own controls.
  // Everything pinned is fixed to the viewport, so the host in normal flow
  // has to reserve exactly their combined height -- otherwise the first
  // thing the author wrote sits underneath them.
  function measureBar() {
    var strip = root.querySelector(".mc-strip");
    var total = barHeight() + (strip ? Math.round(strip.getBoundingClientRect().height) : 0);
    chrome.style.setProperty("--mc-below", total + "px");
    chrome.style.setProperty("--mc-bar-only", barHeight() + "px");
    host.style.height = total + "px";
    document.documentElement.style.scrollPaddingTop = (total + 8) + "px";
    document.documentElement.style.setProperty("--mc-bar-height", total + "px");
  }
  // The on-screen keyboard covers the bottom of the screen without changing
  // the layout viewport, so anything pinned to bottom:0 -- including the
  // composer's send button -- ends up underneath it. visualViewport is the
  // only thing that reports the covered height; publish it and the panels
  // sit above the keyboard instead of behind it.
  function measureKeyboard() {
    var vv = window.visualViewport;
    if (!vv) return;
    var covered = Math.max(0, Math.round(
      window.innerHeight - vv.height - vv.offsetTop));
    chrome.style.setProperty("--mc-kb", covered + "px");
  }
  if (window.visualViewport) {
    visualViewport.addEventListener("resize", measureKeyboard);
    visualViewport.addEventListener("scroll", measureKeyboard);
  }

  addEventListener("scroll", syncStrip, {passive: true});
  addEventListener("scroll", measureBar, {passive: true});
  addEventListener("resize", measureBar, {passive: true});

  function render() {
    chrome.textContent = "";
    chrome.appendChild(barRow());
    // Hidden while a panel or view is up. Choosing a pillar is a full-screen
    // act; leaving the current pillar's own section names showing behind the
    // chooser makes it unclear which screen you are even looking at.
    var covered = ui.panel || ui.entry || ui.anchor;
    var strip = covered ? null : stripNode();
    if (strip) chrome.appendChild(strip);
    if (ui.who) chrome.appendChild(whoList());
    var p = panelNode(); if (p) chrome.appendChild(p);
    var e = entryNode(); if (e) chrome.appendChild(e);
    var a = anchorNode(); if (a) chrome.appendChild(a);
    measureBar();
    syncStrip();
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
  // pillarId null means the mission overview -- the screen a reader lands on
  // and, until now, the one place navigation could not take them back to.
  function goto(pillarId) {
    // TWO SURFACES, TWO KINDS OF NAVIGATION. In a frame there is no URL to go
    // to, so a screen is fetched over the channel and written in place. Served
    // at a real URL there IS one, and asking a channel that does not exist
    // leaves every control silently dead -- which is what the dashboard did.
    if (window.parent === window) {
      var base = "/missions/" + (state.mission_id || "");
      location.assign(pillarId ? base + "/pillars/" + pillarId : base);
      return;
    }
    var body = pillarId
      ? {kind: "pillar_site", pillar_id: pillarId}
      : {kind: "mission_site"};
    return request("read", body)
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
    // Sticky HERE, on the host: this element is a child of body, so body's
    // full height is the range it can stick across. Setting it on the bar
    // inside the shadow root pins the bar to its own height, which is the
    // same as not pinning it at all.
    // A plain block that occupies the bar's height. The bar itself is fixed
    // to the viewport; this is what stops it covering the top of the page.
    host.style.cssText = "display:block;height:3rem";
    document.body.insertBefore(host, document.body.firstChild);
    // The one property we set on the author's document. A sticky bar still
    // overlays whatever a #fragment jump lands on, and working fragment
    // links are the whole reason this document carries a <base>. This
    // affects scroll landing only -- nothing about how their page paints.
    document.documentElement.style.scrollPaddingTop = "3rem";
    // Published so a page's own sticky header can sit UNDER this bar instead
    // of behind it: `top: var(--mc-bar-height)` stacks them. Without it both
    // stick at 0 and ours, which is above in paint order, hides theirs.
    document.documentElement.style.setProperty("--mc-bar-height", "3rem");
    mountAnchors();
    collectSections();
    render();
    announceHere();
    // Presence is a claim with a shelf life, so it is re-stated rather than
    // set once. Paused while the tab is hidden: nobody is reading a screen
    // they cannot see, and saying otherwise is the lie this is meant to end.
    setInterval(function () {
      if (!document.hidden) announceHere();
    }, 45000);
    addEventListener("visibilitychange", function () {
      if (!document.hidden) announceHere();
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else { start(); }

  // Exposed for tests only, via the port -- never on window.
  port.postMessage({v: 1, type: "boot", scriptsAtBoot: SCRIPTS_AT_BOOT});
})();
