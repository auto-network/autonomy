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
    ui.view = null;
    // Where this surface was opened FROM. Without it, closing a discussion
    // could only mean "close everything" -- so reading one question and
    // wanting the next one meant reopening the list by hand every time.
    ui.from = null;
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
      payload = {question: body.question, anchor: body.anchor || null};
    } else if (op === "write" && kind === "answer") {
      url = ownerPath(body.entry_id) + "/questions/" + body.entry_id + "/answer";
      payload = {answer: body.answer};
    } else if (op === "write" && kind === "reopen") {
      url = ownerPath(body.entry_id) + "/questions/" + body.entry_id + "/reopen";
      payload = {followup: body.followup};
    } else if (op === "write" && kind === "followup") {
      url = ownerPath(body.entry_id) + "/questions/" + body.entry_id + "/followup";
      payload = {followup: body.followup};
    } else if (op === "write" && kind === "close") {
      url = ownerPath(body.entry_id) + "/questions/" + body.entry_id + "/close";
      payload = {};
    } else if (op === "write" && kind === "asked") {
      url = (currentPillar() ? "/api/pillars/" + currentPillar().pillar_id
                             : "/api/missions/" + mid) + "/asked";
      payload = {anchor: body.anchor, question: body.question, answer: body.answer};
    } else if (op === "read" && kind === "here") {
      // The relay has always recorded this and the dashboard never did, so
      // in the app the list of who is here was empty on every mission,
      // always -- an actively worked mission read as abandoned.
      url = (body.pillar_id ? "/api/pillars/" + body.pillar_id
                            : "/api/missions/" + mid) + "/here";
      payload = {};
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

  // A pillar's own claim about its work. The status line shown on its card is
  // replaced, its age resets because something just happened, and the feed
  // gains an entry -- the feed is what the reader opens to see the sequence.
  function applyActivity(data) {
    var p = (state.pillars || []).filter(function (x) {
      return x.pillar_id === data.pillar_id;
    })[0];
    if (data.event === "status") {
      if (p) { p.last_done = data.text; p.age = "just now"; }
      state.status_posts = state.status_posts || [];
      // THE STREAM REPLAYS. Subscribing replays the cached state of every
      // topic, and EventSource reconnects on its own after any blip, so the
      // same status arrives again on every reconnect -- and once more at
      // first connect, on top of the copy already inlined in the page. Three
      // reconnects showed three identical entries.
      //
      // Applying an event has to be idempotent, which the conversation
      // handler already is: it finds the entry by id and REPLACES it. This
      // one appended, so it was the only handler that could duplicate.
      // Identical text from the same pillar is the same report, not news.
      var already = state.status_posts.some(function (x) {
        return x.pillar_id === data.pillar_id && x.text === data.text;
      });
      if (already) { render(); return; }
      state.status_posts.unshift({
        post_id: "live-" + (data.at || Math.random()),
        pillar_id: data.pillar_id,
        pillar_name: data.pillar_name || (p && p.name) || "",
        color: data.color || (p && p.color) || "",
        text: data.text || "",
        ago: "just now",
      });
    } else if (data.event === "revision") {
      // A new screen is why the age exists: it is the answer to "when did
      // this pillar last do something visible".
      if (p) p.age = "just now";
    } else {
      return;                       // an event kind we do not render
    }
    render();
  }

  // ---- live updates at a real URL -----------------------------------------
  // The framed path is handed events over its channel. At a real URL nothing
  // fed applyEvent at all, so a screen showed whatever was true when it
  // loaded and nothing after -- a question answered while the reader watched
  // stayed unanswered on their screen until they reloaded.
  //
  // The dashboard already streams every topic to open tabs, and Mission
  // Control already publishes conversation events onto it. This subscribes,
  // keeps the frames for THIS mission, and hands them to the same handler the
  // relay path uses, wrapped in the same shape the relay builds.
  //
  // THE SOCKET IS GIVEN BACK WHEN THE PAGE STOPS BEING LOOKED AT. Navigating
  // away does not destroy this page -- it goes into the back/forward cache
  // alive, holding its connections. Served over HTTP/1.1 a browser allows
  // about six sockets to one origin, and an open event stream owns one for as
  // long as it lives. Six visited screens later every socket belongs to a page
  // nobody is looking at, and the next navigation waits for one to come free:
  // seconds of white screen, while everything already inside the document
  // stays instant because it needs no socket at all. Force-quitting the app
  // cured it because that is what finally dropped them.
  var live = null;

  function subscribeLive() {
    if (live) return;                          // one stream, never a stack
    if (window.parent !== window) return;      // framed: the host feeds us
    if (typeof EventSource !== "function") return;
    if (!state.mission_id) return;
    var source;
    try {
      source = new EventSource("/api/events");
    } catch (_e) {
      return;    // no stream is the status quo, not a failure worth showing
    }
    live = source;
    // Work landing, not conversation: a pillar wrote its status line or
    // published a new screen. Without this a live page shows people talking
    // and never shows anything being done, which is most of what there is to
    // see on a mission.
    source.addEventListener("mission_control:activity", function (e) {
      var data;
      try { data = JSON.parse(e.data); } catch (_err) { return; }
      if (!data || data.mission_id !== state.mission_id) return;
      applyActivity(data);
    });
    source.addEventListener("mission_control:conversation", function (e) {
      var data;
      try { data = JSON.parse(e.data); } catch (_err) { return; }
      if (!data || data.mission_id !== state.mission_id) return;
      // A pillar's question belongs on the overview too: the screen carries
      // the whole mission's conversation, and applyEvent matches by entry id.
      applyEvent({kind: "conversation", question: data.question});
    });
    // EventSource reconnects on its own. A stream that never opens leaves the
    // page exactly as it is today, which is why nothing here reports an error.
  }

  function releaseLive() {
    if (!live) return;
    live.close();
    live = null;
  }

  // pagehide, not unload: unload never fires for a page going into the
  // back/forward cache, which is the only case that was leaking. pageshow
  // fires on a restore as well as a fresh load, so the stream comes back for
  // a reader who simply pressed Back -- and comes back ONCE, because
  // subscribeLive refuses to open a second.
  addEventListener("pagehide", releaseLive);
  addEventListener("pageshow", subscribeLive);
  // Backgrounding the app is the same situation: a stream nobody can see,
  // holding a connection the next navigation needs.
  addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") releaseLive(); else subscribeLive();
  });

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
  // OPEN MEANS NOT CLOSED. Every count and filter here read it off the answer
  // field, which was the same thing right up until closing became its own
  // act. After that a question closed because it stopped mattering still
  // counted as open, still sat under Unanswered, and still showed as waiting
  // on somebody -- the old model surviving in every place that was not
  // touched when it changed.
  function isOpen(q) { return !q.closed_at; }
  function openCount() {
    return questionsHere().filter(isOpen).length;
  }
  function answeredCount() {
    return questionsHere().filter(function (q) { return !isOpen(q); }).length;
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

  // Newest first, each line owned by the pillar that wrote it. No grouping
  // and no collapsing: the value is reading them in order, and any grouping
  // hides exactly the interleaving that shows what a week actually looked
  // like.
  function feedView() {
    var posts = state.status_posts || [];
    var body = posts.length
      ? posts.map(function (post) {
          var dot = el("span", {class: "mc-swatch"});
          dot.style.background = post.color || "#475569";
          return el("article", {class: "mc-post"}, [
            el("div", {class: "mc-post-head"}, [
              dot,
              el("span", {class: "mc-post-who", text: post.pillar_name || ""}),
              el("span", {class: "mc-age", text: post.ago || ""}),
            ]),
            el("p", {class: "mc-post-text", text: post.text || ""}),
          ]);
        })
      : [el("p", {class: "mc-empty",
                  text: "No pillar has reported anything yet."})];
    return el("section", {class: "mc-view"}, [
      el("div", {class: "mc-phead"}, [
        el("span", {class: "mc-ptitle", text: "What is happening"}),
        el("span", {class: "mc-grow"}),
        el("button", {class: "mc-x", text: "\u00d7", onclick: function () { show(null); }}),
      ]),
      el("div", {class: "mc-pbody"}, body),
    ]);
  }

  // PROSE KEEPS ITS SHAPE. Answers and questions are written with blank
  // lines between paragraphs and lists on their own lines. Setting all of it
  // as one node's text collapses every break, so a considered answer arrives
  // as one unreadable run-on -- which is exactly how the first real answer on
  // this platform rendered.
  function paras(text, cls) {
    return String(text || "")
      .split(/\n\s*\n/)
      .map(function (block) { return block.trim(); })
      .filter(Boolean)
      .map(function (block) { return el("p", {class: cls, text: block}); });
  }

  function barRow() {
    var p = currentPillar() || {};
    var here = presentHere();
    var swatch = el("span", {class: "mc-swatch"});
    swatch.style.background = p.color || "#475569";
    var kids = [
      el("button", {class: "mc-pill", onclick: function () { show(ui.panel === "pillars" ? null : {panel: "pillars"}); }},
         [swatch, el("span", {class: "mc-name", text: p.name || state.mission || "Mission"}), el("span", {class: "mc-caret", text: "\u25be"})]),
      // The age was the only thing on the bar already answering "when did
      // anything last happen", so it is where "what happened" belongs. A
      // span that now opens a surface has to look like a control, or it is a
      // secret.
      el("button", {class: "mc-age mc-age-btn", text: p.age || "",
                    title: "What every pillar has been reporting",
                    onclick: function () { show(ui.view === "feed" ? null : {view: "feed"}); }}),
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
    // WAITING ON YOU, kept apart from the questions count. That list is what
    // the mission owes you; this is the other direction, and folding them
    // together would bury a decision somebody is blocked on inside a number
    // that mostly means "conversations". These exist only on the page until
    // they are answered, so this is the one place they can be found without
    // scrolling the whole screen looking for a marked paragraph.
    var forYou = unanswered();
    if (forYou.length) {
      kids.push(el("button", {
        class: "mc-foryou", title: forYou.length + " waiting on you",
        onclick: function () { show({anchor: forYou[0].ref}); },
      }, [el("span", {text: forYou.length + " for you"})]));
    }
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

  // An agent in the list IS a session, and its participant id is that
  // session's name -- so the row can go straight to the session viewer.
  // Only where there is a dashboard to navigate to: inside a relay frame the
  // origin is opaque and a dashboard-relative path resolves to nothing, so
  // the row stays plain text rather than becoming a link that fails.
  function sessionHref(h) {
    if (window.parent !== window) return null;
    if (!h || h.kind !== "agent") return null;          // a person is not a session
    var id = h.participant_id;
    return id ? "/session/" + encodeURIComponent(id) : null;
  }

  function whoList() {
    var here = presentHere();
    return el("div", {class: "mc-who"},
      [el("p", {class: "mc-label", text: here.length + " here"})].concat(
        here.map(function (h) {
          var href = sessionHref(h);
          var name = href
            ? el("a", {class: "mc-who-name mc-who-link", href: href,
                       title: "Open this session"}, [el("span", {text: h.label || ""})])
            : el("span", {class: "mc-who-name", text: h.label || ""});
          return el("div", {class: "mc-who-row"}, [face(h), name,
            el("span", {class: "mc-who-seen", text: h.seen || ""})]);
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

  // The opening of a reply, enough to recognise it by. Never the whole
  // thing: the row is an index, and a row that grows to the length of its
  // answer stops being one.
  function firstLine(text) {
    var line = String(text || "").split(/\n/)[0].trim();
    return line.length > 120 ? line.slice(0, 117) + "\u2026" : line;
  }

  function rowChipText(q) {
    if (q.closed_at) return q.answer ? "answered" : "closed";
    if (q.relay_status === "failed") return "not delivered";
    if ((q.updates || []).length) return "working";
    return "open";
  }

  function rowChipClass(q) {
    if (q.closed_at) return "mc-chip mc-chip-done";
    if (q.relay_status === "failed") return "mc-chip mc-chip-bad";
    if ((q.updates || []).length) return "mc-chip mc-chip-working";
    return "mc-chip mc-chip-open";
  }

  function questionRows() {
    var shown = questionsHere().filter(function (q) {
      if (qFilter === "open") return isOpen(q);
      if (qFilter === "done") return !isOpen(q);
      return true;
    });
    // NEWEST FIRST. The list used to lead with everything unanswered and,
    // inside that, with the oldest -- so the question just asked landed at the
    // bottom of the screen, furthest from the reader who had only now sent it.
    // Which state to look at is the filter's job; this is only about when.
    var sorted = shown.slice().sort(function (a, b) {
      return (parseFloat(b.created_at) || 0) - (parseFloat(a.created_at) || 0);
    });
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
      return el("button", {class: "mc-row", onclick: function () {
        show({entry: q.entry_id, from: {panel: "questions"}});
      }}, [
        el("div", {class: "mc-row-top"}, [
          // Three states, not two. "Open" covered a question being actively
          // worked on and one that never reached anybody, which are the two
          // things a reader most needs told apart.
          el("span", {class: rowChipClass(q), text: rowChipText(q)}),
          el("span", {class: "mc-sub", text: pillarName(q.pillar_id)}),
        ]),
        el("p", {class: "mc-qtext", text: q.question || ""}),
        el("p", {class: "mc-sub", text: q.asked_by_label || ""}),
        // An answer you cannot see is one you do not know arrived. The row
        // showed only the question, so the whole visible change on being
        // answered was a colour going away -- which reads as nothing having
        // happened. The first line of the reply is the signal.
      ].concat(q.answer ? [el("p", {class: "mc-answer-peek", text: firstLine(q.answer)})] : []));
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

  // ONE control, not three. The header is a fixed-height row that does not
  // wrap, so three text filters spent the whole width and pushed the close
  // control off the side of a phone -- still in the DOM, still working, and
  // impossible to reach. This states the filter it is currently on and its
  // size, and cycles; the count is kept because a filter whose size you
  // cannot see has to be tried to find out whether it was worth trying.
  var FILTERS = [
    {key: "all", label: "All"},
    {key: "open", label: "Unanswered"},
    {key: "done", label: "Answered"},
  ];

  function filterToggle() {
    var all = questionsHere();
    var open = all.filter(isOpen).length;
    var counts = {all: all.length, open: open, done: all.length - open};
    var i = FILTERS.findIndex(function (f) { return f.key === qFilter; });
    if (i === -1) i = 0;
    var cur = FILTERS[i];
    var next = FILTERS[(i + 1) % FILTERS.length];
    return [el("button", {
      class: qFilter === "all" ? "mc-chip" : "mc-chip mc-chip-on",
      // Says what one more tap does, because a control that cycles gives no
      // hint of its other states from looking at it.
      title: "Showing " + cur.label.toLowerCase() + " — tap for " + next.label.toLowerCase(),
      text: cur.label + " " + counts[cur.key],
      onclick: function () { qFilter = next.key; render(); },
    })];
  }

  function panelNode() {
    if (!ui.panel) return null;
    var body = ui.panel === "pillars" ? pillarRows() : questionRows();
    var kids = [
      el("div", {class: "mc-phead"}, [
        el("span", {class: "mc-ptitle", text: ui.panel === "pillars" ? "Pillars" : "Questions"}),
      ].concat(ui.panel === "questions" ? filterToggle() : []).concat([
        el("span", {class: "mc-grow"}),
        el("button", {class: "mc-x", text: "\u00d7", onclick: function () { show(null); }}),
      ])),
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

  // One step back, not out. Falls through to closing only when there is
  // nowhere to return to -- a question opened directly, with no list behind
  // it, has nothing above it to show.
  function backLabel() {
    if (ui.from && ui.from.panel === "questions") return "\u2039 Questions";
    if (ui.from && ui.from.anchor) return "\u2039 Back";
    return "\u2039 Back to " + ((currentPillar() || {}).name || "mission");
  }

  function goBack() {
    show(ui.from || null);
  }

  // Timestamps cross the wire as epoch SECONDS (floats -- _app_json passes
  // them through precisely because canonical_json refuses floats). Printed as
  // they arrive, a question was stamped "1786651692.626123": the stored number
  // reaching the screen with nothing between it and the reader.
  function when(ts) {
    var n = typeof ts === "number" ? ts : parseFloat(ts);
    if (!isFinite(n)) return typeof ts === "string" ? ts : "";
    var d = new Date(n * 1000);
    var t = d.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
    // The date is noise on something from an hour ago and essential on
    // anything older, so it appears only once it starts carrying information.
    return d.toDateString() === new Date().toDateString()
      ? t
      : d.toLocaleDateString([], {month: "short", day: "numeric"}) + " " + t;
  }

  function entryNode() {
    if (!ui.entry) return null;
    var q = state.questions.filter(function (x) { return x.entry_id === ui.entry; })[0];
    if (!q) return null;
    var body = [el("p", {class: "mc-label", text: "Question"})];
    paras(q.question, "mc-qbig").forEach(function (n) { body.push(n); });
    body.push(el("p", {class: "mc-sub",
                       text: (q.asked_by_label || "") + (q.created_at ? " \u00b7 " + when(q.created_at) : "")}));
    if (q.anchor) body.push(el("p", {class: "mc-sub", text: q.anchor}));
    if (isOpen(q)) {
      // DID IT REACH ANYONE. Whether the question was actually delivered has
      // always been recorded and never shown, so a question that failed to
      // reach its coordinator looked exactly like one being thought about:
      // both were nothing at all on the screen, for as long as you cared to
      // wait.
      var relay = q.relay_status;
      if (relay === "failed") {
        body.push(el("p", {class: "mc-relay mc-relay-bad",
                           text: "Not delivered — nobody has been told about this yet."}));
      } else if (relay === "sent") {
        body.push(el("p", {class: "mc-relay", text: "Delivered"}));
      } else {
        body.push(el("p", {class: "mc-relay", text: "Sending…"}));
      }
      var steps = q.updates || [];
      if (steps.length) {
        body.push(el("p", {class: "mc-label mc-mt", text: "While this is open"}));
        steps.forEach(function (u, i) {
          // Newest is the live one; everything above it became a tick when the
          // next arrived. Nothing marks itself finished -- being superseded is
          // what finishing looks like.
          var last = i === steps.length - 1;
          body.push(el("div", {class: last ? "mc-step mc-step-live" : "mc-step"}, [
            el("span", {class: "mc-step-ic", text: last ? "●" : "✓"}),
            el("div", {}, [
              el("div", {class: "mc-step-tx", text: u.text || ""}),
              el("div", {class: "mc-step-at",
                         text: when(u.created_at) + (last ? " · now" : "")}),
            ]),
          ]));
        });
      }
    }
    if (q.answer) {
      body.push(el("p", {class: "mc-label mc-mt", text: "Answer"}));
      // The answer gets its own block, not another paragraph in the same
      // column of grey. Question and answer reading as one undifferentiated
      // pile is the difference between a record and a wall of text.
      body.push(el("div", {class: "mc-answer-block"}, paras(q.answer, "mc-answer")));
    }
    return el("section", {class: "mc-view"}, [
      el("div", {class: "mc-phead"}, [
        el("button", {class: "mc-back", text: backLabel(),
                      onclick: goBack}),
        el("span", {class: "mc-grow"}),
        // Closing is YOUR act, and only on your own still-open question. The
        // question that stopped needing an answer had nowhere to go but an
        // invented one, which puts words in the record nobody said.
        mine(q) && !q.closed_at
          ? el("button", {class: "mc-close-q", text: "Close",
                          title: "Close this without an answer",
                          onclick: function () { closeEntry(q.entry_id); }})
          : el("span", {}),
        el("span", {class: q.closed_at ? "mc-chip" : "mc-chip mc-chip-open",
                    text: q.closed_at ? (q.answer ? "answered" : "closed") : "open"}),
      ]),
      el("div", {class: "mc-pbody"}, body),
      composer(COMPOSE[replyKind(q)].hint, "", COMPOSE[replyKind(q)].label,
               function (t) { reply(q.entry_id, t, replyKind(q)); }),
    ]);
  }

  //: What each of the three is called where the reader can see it. "Add" and
  //: not "Answer" on your own question is the whole point: the wrong word
  //: there is what made answering yourself look like the thing to do.
  var COMPOSE = {
    answer:   {hint: "Answer\u2026",                   label: "Answer"},
    followup: {hint: "Add to your question\u2026",     label: "Add"},
    reopen:   {hint: "Reopen with a follow-up\u2026",  label: "Reopen"},
  };

  function anchorNode() {
    if (!ui.anchor) return null;
    var here = atAnchor(ui.anchor);
    var screenAsk = asked(ui.anchor);
    var answered = here.some(function (q) { return q.answer; });
    var control = anchorControls.filter(function (x) { return x.ref === ui.anchor; })[0];
    // Never headed by the anchor id again. It is a name for the code, and it
    // was the only thing this panel said about what you were being asked --
    // the question written on the element did not travel with you.
    var body;
    if (screenAsk && !answered) {
      body = [
        el("p", {class: "mc-label mc-label-asks", text: pillarName(
          (currentPillar() || {}).pillar_id) + " asks"}),
      ];
      paras(screenAsk.asks, "mc-qbig").forEach(function (n) { body.push(n); });
      if (screenAsk.about) body.push(el("p", {class: "mc-sub mc-mb", text: screenAsk.about}));
    } else {
      body = [el("p", {class: "mc-label", text: "about"}),
              el("p", {class: "mc-qbig mc-mb",
                       text: (control && control.about) || ui.anchor})];
    }
    // "Nothing discussed here yet" is false on a screen that is asking you
    // something -- the question is right there above it.
    if (!here.length && !(screenAsk && !answered)) {
      body.push(el("p", {class: "mc-empty", text: "Nothing discussed here yet."}));
    }
    here.forEach(function (q) {
      body.push(el("button", {class: "mc-row", onclick: function () {
        show({entry: q.entry_id, from: {anchor: ui.anchor}});
      }}, [
        el("span", {class: isOpen(q) ? "mc-chip mc-chip-open" : "mc-chip",
                    text: rowChipText(q)}),
        el("p", {class: "mc-qtext", text: q.question || ""}),
      ]));
    });
    return el("section", {class: "mc-view"}, [
      el("div", {class: "mc-phead"}, [
        el("button", {class: "mc-back", text: "\u2039 Back to " + ((currentPillar() || {}).name || "mission"),
                      onclick: function () { show(null); }}),
      ]),
      el("div", {class: "mc-pbody"}, body),
      screenAsk && !answered
        ? composer("Your answer\u2026", screenAsk.about || "", "Answer",
                   function (t) { answerAsked(ui.anchor, screenAsk.asks, t); })
        : composer("Ask about this\u2026", "tagged " + ui.anchor, "Ask",
                   function (t) { ask(t, ui.anchor); }),
    ]);
  }

  // Identity is NEVER supplied here. participant_id lives in the grant and is
  // attached by link_serving; there is no field in this request to claim one.
  //
  // FIELD NAMES ARE THE SERVER'S, NOT OURS. Over the relay the body is
  // forwarded completely uninterpreted to handle_relay_write, which reads
  // `question` and `followup` -- a generic `body` key meant every ask over a
  // share link was refused, while the same call worked at a real URL because
  // httpRequest happened to translate it. Both ends were tested; the seam
  // between them was not. Keeping one vocabulary is what removes the seam.
  function ask(text, anchor) {
    var body = {kind: "question", question: text};
    var p = currentPillar();
    if (p) body.pillar_id = p.pillar_id;
    if (anchor) body.anchor = anchor;
    // Where to return to, captured BEFORE show() clears it.
    var from = ui.anchor ? {anchor: ui.anchor}
                         : (ui.panel ? {panel: ui.panel} : ui.from);
    return request("write", body).then(function (r) {
      // LAND ON WHAT WAS JUST SENT. Closing the whole chrome on success threw
      // the reader back to the screen with no sign their question existed --
      // and the next thing they are waiting for, a progress update, renders
      // inside this very view.
      var q = r && r.question;
      if (!q || !q.entry_id) { show(null); return; }
      var i = state.questions.findIndex(function (x) { return x.entry_id === q.entry_id; });
      if (i === -1) state.questions.push(q); else state.questions[i] = q;
      show({entry: q.entry_id, from: from});
    });
  }
  // WHOSE QUESTION IS IT. The composer used to choose by whether an answer
  // existed yet, which cannot see the difference between a question you asked
  // and one asked of you -- so on your own open question it offered the
  // control that files an answer, and using it closed your own question with
  // your own words, recording you as having answered yourself.
  function mine(q) {
    return !!state.me && q.asked_by_participant_id === state.me;
  }

  function replyKind(q) {
    if (q.closed_at) return "reopen";     // finished with; say more and it reopens
    return mine(q) ? "followup" : "answer";
  }

  function closeEntry(entryId) {
    return request("write", {kind: "close", entry_id: entryId}).then(function (r) {
      mergeEntry(r && r.question);
      render();
    });
  }

  function mergeEntry(q) {
    if (!q || !q.entry_id) return;
    var i = state.questions.findIndex(function (x) { return x.entry_id === q.entry_id; });
    if (i === -1) state.questions.push(q); else state.questions[i] = q;
  }

  // Answering a question the SCREEN asked. The question and the answer arrive
  // together because until now there was nothing to answer -- the question
  // lives on the element, and the platform never reads the author's document.
  // The pair lands as one entry with the attributions the other way round.
  function answerAsked(anchorRef, question, text) {
    return request("write", {kind: "asked", anchor: anchorRef,
                             question: question, answer: text})
      .then(function (r) {
        mergeEntry(r && r.question);
        repaintAnchors();
        show({anchor: anchorRef});
      });
  }

  function reply(entryId, text, kind) {
    var body = {kind: kind, entry_id: entryId};
    body[kind === "answer" ? "answer" : "followup"] = text;
    return request("write", body).then(function (r) {
      var q = r && r.question;
      if (q && q.entry_id) {
        var i = state.questions.findIndex(function (x) { return x.entry_id === q.entry_id; });
        if (i === -1) state.questions.push(q); else state.questions[i] = q;
      }
      render();
    });
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
    // WHAT IS BEING TYPED SURVIVES. render() rebuilds the whole chrome, so
    // without this a live event arriving mid-answer throws away what the
    // reader had written -- which is worse than the stale screen the live
    // updates exist to fix. activeElement is read from the shadow root, not
    // the document: the root is closed, so document.activeElement is the
    // host element, not the field inside it.
    var typing = null;
    var active = root.activeElement;
    if (active && active.tagName === "TEXTAREA") {
      typing = {value: active.value,
                start: active.selectionStart, end: active.selectionEnd};
    }
    chrome.textContent = "";
    chrome.appendChild(barRow());
    // Hidden while a panel or view is up. Choosing a pillar is a full-screen
    // act; leaving the current pillar's own section names showing behind the
    // chooser makes it unclear which screen you are even looking at.
    var covered = ui.panel || ui.entry || ui.anchor || ui.view;
    var strip = covered ? null : stripNode();
    if (strip) chrome.appendChild(strip);
    if (ui.who) chrome.appendChild(whoList());
    var p = panelNode(); if (p) chrome.appendChild(p);
    var e = entryNode(); if (e) chrome.appendChild(e);
    var a = anchorNode(); if (a) chrome.appendChild(a);
    if (ui.view === "feed") chrome.appendChild(feedView());
    repaintAnchors();
    if (typing) {
      var ta = root.querySelector("textarea");
      if (ta) {
        ta.value = typing.value;
        ta.focus();
        try { ta.setSelectionRange(typing.start, typing.end); } catch (_e) {}
      }
    }
    measureBar();
    syncStrip();
  }

  // ---- anchored controls --------------------------------------------------
  // Mounted INSIDE the anchored element, never as a sibling: a next sibling
  // breaks the author's adjacent-sibling (+) CSS rules. Each gets its own
  // closed root so author CSS cannot restyle it either.
  // Every mounted control, kept so their counts can be refreshed. The shadow
  // root is CLOSED, so it cannot be reached again from the holder element --
  // if the reference is not kept here it is gone.
  var anchorControls = [];

  //: Which questions the list shows. Kept OUTSIDE ui because ui is cleared
  //: every time a surface opens; a filter that silently reset each time you
  //: opened the list would be worse than not having one.
  var qFilter = "all";

  var BUBBLE_SVG =
    '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
    '<path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 8.5 8.5 0 0 1-3.8-.9L3 21l1.9-5.1A8.4 8.4 0 0 1 12 3a8.4 8.4 0 0 1 9 8.5z"/></svg>';

  // Two counts, not one: unanswered and answered say different things, and a
  // single total cannot tell "three still waiting on me" from "three already
  // settled". Same convention as the bar, so a reader learns it once. Neither
  // is drawn at zero -- an anchor nobody has asked about is a bare bubble, and
  // printing 0 next to it would be noise on every marked element on the page.
  function paintAnchor(control) {
    var here = atAnchor(control.ref);
    var open = 0, done = 0;
    here.forEach(function (q) { isOpen(q) ? open++ : done++; });
    // A screen asking YOU is not a count of a conversation, it is a thing
    // waiting on you -- so it says so in a word, and stops the moment it is
    // answered. A number here would read as "two people are chatting".
    if (control.asks && !done) {
      control.btn.innerHTML = "";
      control.btn.classList.add("mc-anchor-asks");
      control.btn.appendChild(el("span", {class: "mc-asks-label", text: "Answer"}));
      return;
    }
    control.btn.classList.remove("mc-anchor-asks");
    control.btn.innerHTML = BUBBLE_SVG;
    if (open) {
      control.btn.appendChild(el("span", {
        class: "mc-count mc-count-open", title: open + " unanswered",
        text: String(open),
      }));
    }
    if (done) {
      control.btn.appendChild(el("span", {
        class: "mc-count mc-count-done", title: done + " answered",
        text: String(done),
      }));
    }
  }

  // Recount every control. mountAnchors only ever ADDS -- it skips an element
  // that already carries one -- so a control mounted before a question was
  // asked kept showing the count it was born with, while the bar recounted on
  // every render. The anchor was frozen, not miscounted.
  function repaintAnchors() {
    anchorControls.forEach(paintAnchor);
  }

  // What this element is about, in the author's own words: its own heading,
  // or the nearest one above it. The panel headed itself with the raw anchor
  // id -- "decision:dispatch-vs-local" -- which is a name for me, not for the
  // person being asked to answer.
  function headingFor(target) {
    var own = target.querySelector("h1,h2,h3,h4,h5,h6");
    if (own && own.textContent.trim()) return own.textContent.trim();
    var node = target;
    while (node && node !== document.body) {
      var prev = node.previousElementSibling;
      while (prev) {
        if (/^H[1-6]$/.test(prev.tagName) && prev.textContent.trim()) {
          return prev.textContent.trim();
        }
        prev = prev.previousElementSibling;
      }
      node = node.parentElement;
    }
    return "";
  }

  //: The screen's own questions, by anchor -- read from the DOM, never from
  //: the server, because the server never parses the author's document.
  function asked(ref) {
    var c = anchorControls.filter(function (x) { return x.ref === ref; })[0];
    return c && c.asks ? c : null;
  }

  //: Answered ones become ordinary entries; the rest exist only on the page.
  function unanswered() {
    return anchorControls.filter(function (c) {
      return c.asks && !atAnchor(c.ref).some(function (q) { return q.answer; });
    });
  }

  function mountAnchors() {
    var n = 0;
    document.querySelectorAll("[data-mc-anchor]").forEach(function (target) {
      if (target.querySelector("[data-mc-control]")) return;   // idempotent
      var ref = target.getAttribute("data-mc-anchor");
      // THE OTHER DIRECTION. An anchor alone is a place to ask about this.
      // With data-mc-ask the screen is asking YOU, and the question is right
      // there on the element -- nothing has to parse the author's document to
      // find it, and a question edited away simply stops existing.
      var asks = (target.getAttribute("data-mc-ask") || "").trim();
      // The nearest heading, so a panel can name what it is about instead of
      // showing the internal id, which means nothing to the person reading.
      var about = headingFor(target);
      var holder = document.createElement("span");
      holder.setAttribute("data-mc-control", "");
      var r = holder.attachShadow({mode: "closed"});
      r.innerHTML = "<style>" + CSS + "</style>";
      var btn = el("button", {
        class: "mc-anchor-btn", title: asks ? "Answer this" : "Discuss",
        onclick: function () { show({anchor: ref}); },
      });
      var control = {ref: ref, btn: btn, asks: asks, about: about};
      anchorControls.push(control);
      paintAnchor(control);
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
    subscribeLive();
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
