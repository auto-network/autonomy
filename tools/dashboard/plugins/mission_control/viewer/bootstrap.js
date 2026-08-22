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
    ui.view = null; ui.post = null;
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
  // THE TRANSPORT DISCRIMINATOR. True only in the relay's srcdoc frame —
  // the one context with no real URL (compose injects base=about:srcdoc
  // only on that path) and therefore no HTTP; everything it needs arrives
  // over a MessagePort. The SPA also frames this document, but at its real
  // URL with full credentials, where HTTP and EventSource work exactly as
  // at top level. Testing window.parent for transport choices conflated
  // those two and left every control dead in the SPA frame.
  var RELAY_FRAME = window.parent !== window
    && /^about:/.test(document.baseURI || location.href || "");

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
  // ONLY IN A RELAY FRAME. At a real URL no port is ever transferred and
  // none is wanted -- the transport is HTTP. Arming this timer everywhere
  // meant the dashboard told the operator "Not connected. Posting is
  // disabled." six seconds after every load, hid the composer, and flagged
  // the bar "no link", while the HTTP path underneath worked perfectly. The
  // transport was fixed and the question "is there a transport?" went on
  // asking about the old one. Being framed is NOT the discriminator either:
  // the SPA hosts this document in a same-origin frame at its real URL,
  // where HTTP works exactly as at top level. What actually needs a port is
  // the relay's srcdoc frame -- the only context whose base is about:srcdoc,
  // because compose injects that base only on the framed path.
  if (RELAY_FRAME) {
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
    if (!RELAY_FRAME) return httpRequest(op, body);
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
      payload = {question: body.question, anchor: body.anchor || null,
                 anchor_title: body.anchor_title || null,
                 anchor_excerpt: body.anchor_excerpt || null};
    } else if (op === "write" && kind === "answer") {
      url = ownerPath(body.entry_id) + "/questions/" + body.entry_id + "/answer";
      payload = {answer: body.answer};
    } else if (op === "write" && kind === "reopen") {
      url = ownerPath(body.entry_id) + "/questions/" + body.entry_id + "/reopen";
      payload = {followup: body.followup};
    } else if (op === "write" && kind === "reply") {
      url = ownerPath(body.entry_id) + "/questions/" + body.entry_id + "/update";
      payload = {text: body.text, kind: "message"};
    } else if (op === "write" && kind === "followup") {
      url = ownerPath(body.entry_id) + "/questions/" + body.entry_id + "/followup";
      payload = {followup: body.followup};
    } else if (op === "write" && kind === "close") {
      url = ownerPath(body.entry_id) + "/questions/" + body.entry_id + "/close";
      payload = {};
    } else if (op === "write" && kind === "asked") {
      url = (currentPillar() ? "/api/pillars/" + currentPillar().pillar_id
                             : "/api/missions/" + mid) + "/asked";
      payload = {anchor: body.anchor, anchor_title: body.anchor_title,
                 question: body.question, answer: body.answer};
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
    if (RELAY_FRAME) return;                   // relay: the host feeds us
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

  // The bar renders before the author's script declares its views; when
  // the declaration lands, draw the view menu.
  document.addEventListener("mc:views-changed", function () { render(); });

  // The page can summon the pillar-status panel (its News tab) without
  // owning it — same surface the bar's Pillars button opens.
  document.addEventListener("mc:open-status", function () {
    show({panel: "pillars"});
  });

  // The page's inline News view hands replies back to the chrome, which
  // owns the post composer and the reply flow.
  // The page's Questions tab hands conversations back to the chrome: a
  // row opens the entry view, Ask opens the questions panel (composer).
  document.addEventListener("mc:open-entry", function (e) {
    var id = e.detail && e.detail.entry_id;
    if (id) show({entry: id});
  });
  document.addEventListener("mc:open-ask", function () {
    qaMode = "mission";
    show({panel: "questions"});
  });

  document.addEventListener("mc:open-post", function (e) {
    var pid = e.detail && e.detail.post_id;
    var post = (state.status_posts || []).filter(function (p) {
      return p.post_id === pid; })[0];
    if (post) show({post: post});
  });
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
  function isOpen(q) { return !q.closed_at && !q.answer; }
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
  // A terminal: the session behind this screen. One path, same stroke
  // grammar as CHAT so the two bar icons render as siblings.
  var TERM = "M3 5h18v14H3zM7 9.5l3 2.5-3 2.5M13 15h4";
  // A hamburger: the way back out of the mission frame into the app.
  var MENU = "M4 6h16M4 12h16M4 18h16";
  // A pulse line: "latest" — what has been happening, not a clock reading.
  var PULSE = "M3 12h4l3-8 4 16 3-8h4";

  function face(p, small) {
    var cls = small ? "mc-face mc-face-sm" : "mc-face";
    // A guest who sent a photo is shown as one. Everyone else keeps the
    // initial: an agent has no face, and inventing one for them would make
    // the list harder to read rather than friendlier.
    if (p.avatar_url) {
      var img = el("img", {class: cls + " mc-face-photo", src: p.avatar_url,
                           alt: p.label || ""});
      // A photo that fails to load must not leave a blank circle where a
      // person was: fall back to the initial in place.
      img.onerror = function () {
        var f = face({initial: p.initial, color: p.color}, small);
        if (img.parentNode) img.parentNode.replaceChild(f, img);
      };
      return img;
    }
    var f = el("span", {class: cls, text: p.initial || "?"});
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
  //: A reply belongs to the thing it is about. The anchor is free text the
  //: author of a screen normally chooses; a status post has no screen, so the
  //: feed names its own.
  function postAnchor(post) { return "status:" + post.post_id; }

  function feedView() {
    var posts = state.status_posts || [];
    var body = posts.length
      ? posts.map(function (post) {
          var dot = el("span", {class: "mc-swatch"});
          dot.style.background = post.color || "#475569";
          // WHAT WAS SAID BACK, UNDER WHAT IT WAS SAID ABOUT. The feed could
          // be read and nothing else -- replying to any of it meant working
          // out which pillar it came from and going there yourself, which is
          // most of the reason a feed gets ignored.
          var replies = atAnchor(postAnchor(post)).map(function (q) {
            return el("button", {class: "mc-post-reply", onclick: function () {
              show({entry: q.entry_id, from: {view: "feed"}});
            }}, [
              el("p", {class: "mc-post-q", text: q.question || ""}),
              q.answer
                ? el("p", {class: "mc-post-a", text: firstLine(q.answer)})
                : el("span", {class: "mc-chip mc-chip-open", text: rowChipText(q)}),
            ]);
          });
          return el("article", {class: "mc-post"}, [
            el("div", {class: "mc-post-head"}, [
              dot,
              el("span", {class: "mc-post-who", text: post.pillar_name || ""}),
              el("span", {class: "mc-age", text: post.ago || ""}),
            ]),
            el("p", {class: "mc-post-text", text: post.text || ""}),
            el("div", {class: "mc-post-foot"}, [
              el("button", {class: "mc-post-act", text: "Reply",
                            onclick: function () { show({post: post}); }}),
            ]),
          ].concat(replies));
        })
      : [el("p", {class: "mc-empty",
                  text: "No pillar has reported anything yet."})];
    return el("section", {class: "mc-view"}, [
      el("div", {class: "mc-phead"}, [
        el("span", {class: "mc-ptitle", text: "What's been happening"}),
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
    var kids = [];
    // THE WAY OUT. Inside the dashboard SPA the mission frame covers the
    // whole app, so without a door the only exit is the browser back-swipe
    // \u2014 once per internal navigation, counted out loud by the operator. A
    // guest share frame has no app behind it, so it gets no door.
    if (!RELAY_FRAME) {
      kids.push(el("button", {class: "mc-q mc-menu", title: "Back to Mission Control",
                              onclick: function () {
        // The REAL nav, not a page: the parent app opens its own drawer
        // over this frame when it can (the phone). Where it cannot -- the
        // desktop's sidebar is static and cannot rise -- fall back to the
        // Mission Control home, where the full app chrome is visible.
        try {
          if (window.parent !== window) {
            var A = window.parent.Autonomy;
            if (A && typeof A.openNav === "function" && A.openNav()) return;
            if (typeof window.parent.navigateTo === "function") {
              window.parent.navigateTo("/mission-control");
              return;
            }
          }
        } catch (_e) {}
        location.assign("/mission-control");
      }}, [svg(MENU)]));
    }
    // TITLELESS BAR. The screen carries its own title; the bar is a
    // toolbar. The swatch keeps the pillar's colour as the remaining
    // "where am I" cue and opens the pillar status panel.
    kids.push(
      (function () {
        var band = el("span", {class: "mc-swatch"});
        band.style.background = "#e2e8f0";
        return el("button", {class: "mc-pill mc-menu-word",
                    title: "Pillar status \u2014 latest by pillar or by time",
                    onclick: function () { show(ui.panel === "pillars" ? null : {panel: "pillars"}); }},
           [band, el("span", {text: "Pillars"})]);
      })());
    var viewsDecl = null;
    try { viewsDecl = JSON.parse(document.body.dataset.mcViews || "null"); }
    catch (_e) { viewsDecl = null; }
    if (viewsDecl && viewsDecl.length) {
      kids.push(el("button", {class: "mc-pill mc-menu-word", title: "Switch view",
                    onclick: function () { show(ui.panel === "views" ? null : {panel: "views"}); }},
         [el("span", {text: document.body.dataset.mcViewLabel || viewsDecl[0].label || "View"}),
          el("span", {class: "mc-caret", text: "\u25be"})]));
    }
    kids.push(el("span", {class: "mc-grow"}));
    // THE BUBBLE IS GONE. Asks-for-you render better on the page itself,
    // and the Q&A conversations moved into the page's Questions tab — the
    // panel and entry views stay, summoned by the page over events.
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
    if (RELAY_FRAME) return null;
    if (!h || h.kind !== "agent") return null;          // a person is not a session
    var id = h.participant_id;
    return id ? "/session/" + encodeURIComponent(id) : null;
  }

  // Links out of the mission frame ride the PARENT app's router when one
  // is there — a push, not a document load, so the PWA never reboots. A
  // sandboxed guest frame throws on the parent touch and falls through to
  // the plain href (which its state never carries anyway).
  function spaNav(href) {
    return function (e) {
      try {
        if (window.parent !== window &&
            typeof window.parent.navigateTo === "function") {
          e.preventDefault();
          window.parent.navigateTo(href);
        }
      } catch (_e) {}
    };
  }

  function whoList() {
    var here = presentHere();
    var rows = [el("p", {class: "mc-label", text: here.length + " here"})];
    // THE COORDINATING SESSION LIVES HERE NOW. It wore a bar icon of its
    // own, but bar room is spent one icon at a time and presence was
    // already the "who is behind this screen" surface — the session is
    // exactly that. Dashboard only: compose omits session hrefs over the
    // relay, so a guest's panel never grows a door to an internal session.
    var p = currentPillar() || {};
    var sessHref = (p && p.session_href) || state.session_href || "";
    var coordName = (p && p.coordinator_session)
                 || state.coordinator_session || "";
    var coordPresent = here.some(function (h) {
      return h && h.kind === "agent" && h.participant_id === coordName;
    });
    if (sessHref && !coordPresent) {
      rows.push(el("div", {class: "mc-who-row"}, [
        el("span", {class: "mc-who-term"}, [svg(TERM)]),
        el("a", {class: "mc-who-name mc-who-link", href: sessHref,
                 title: "Open the coordinating session",
                 onclick: spaNav(sessHref)},
           [el("span", {text: coordName || "coordinating session"})]),
        el("span", {class: "mc-who-seen", text: "coordinating"}),
      ]));
    }
    return el("div", {class: "mc-who"}, rows.concat(
        here.map(function (h) {
          var isCoord = coordName && h.kind === "agent"
                     && h.participant_id === coordName;
          var href = isCoord ? sessHref : sessionHref(h);
          var name = href
            ? el("a", {class: "mc-who-name mc-who-link", href: href,
                       title: isCoord ? "Open the coordinating session"
                                      : "Open this session",
                       onclick: spaNav(href)}, [el("span", {text: h.label || ""})])
            : el("span", {class: "mc-who-name", text: h.label || ""});
          return el("div", {class: "mc-who-row"}, [
            isCoord ? el("span", {class: "mc-who-term"}, [svg(TERM)]) : face(h),
            name,
            el("span", {class: "mc-who-seen",
                        text: isCoord ? "coordinating" : (h.seen || "")}),
          ]);
        })));
  }

  function pillarRows() {
    // STATUS, NOT NAVIGATION, on a structured mission. A structured screen
    // is one dynamic app showing cross-pillar content — routing the reader
    // to a per-pillar URL from here would reload the world to show them a
    // subset of what they already have. The panel is a toggle: the latest
    // status of every pillar, opened and closed from the bar. Freeform
    // missions keep the rows as destinations, because there each pillar
    // really is a separate document.
    var structured = state.style === "structured";
    var onOverview = !(ui.screenId || state.screen);
    // The overview needs a colour tab like every pillar row has. Without one
    // it reads as a heading above the list rather than as a destination in it.
    var overviewSwatch = el("span", {class: "mc-swatch"});
    overviewSwatch.style.background = "#e2e8f0";
    var rows = structured ? [] : [el("button", {
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
      var content = [
        el("div", {class: "mc-row-top"}, [sw, el("span", {class: "mc-name", text: p.name || ""}), faces]),
        // at most two sentences, no jargon: the last productive thing done.
        // An empty field renders as a stated absence, never a blank card.
        p.last_done
          ? el("p", {class: "mc-last", text: p.last_done})
          : el("p", {class: "mc-last mc-last-none",
                     text: "No completed result reported yet."}),
        el("div", {class: "mc-row-meta"}, meta),
      ];
      if (structured) return el("div", {class: "mc-row mc-row-static"}, content);
      return el("button", {
        class: p.pillar_id === (ui.screenId || state.screen) ? "mc-row mc-row-on" : "mc-row",
        onclick: function () { goto(p.pillar_id); },
      }, content);
    }));
  }

  // The opening of a reply, enough to recognise it by. Never the whole
  // thing: the row is an index, and a row that grows to the length of its
  // answer stops being one.
  function firstLine(text) {
    var line = String(text || "").split(/\n/)[0].trim();
    return line.length > 120 ? line.slice(0, 117) + "\u2026" : line;
  }

  //: ANSWERED BEATS BUSY. A conversation stays open after it is answered, and
  //: it has rounds in it by then -- so reading "working" off the presence of
  //: rounds labelled an answered exchange as though nobody had replied yet.
  function rowChipText(q) {
    if (q.closed_at) return q.answer ? "answered" : "closed";
    if (q.relay_status === "failed") return "not delivered";
    if (q.answer) return "answered";
    if ((q.updates || []).length) return "working";
    return "open";
  }

  function rowChipClass(q) {
    if (q.closed_at || q.answer) return "mc-chip mc-chip-done";
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
      var stripe = el("span", {class: "mc-swatch"});
      stripe.style.background = pillarColor(q.pillar_id);
      return el("button", {class: "mc-row", onclick: function () {
        show({entry: q.entry_id, from: {panel: "questions"}});
      }}, [
        el("div", {class: "mc-row-top"}, [
          stripe,
          el("span", {class: "mc-qpillar", text: pillarName(q.pillar_id)}),
          el("span", {class: "mc-grow"}),
          // Three states, not two. "Open" covered a question being actively
          // worked on and one that never reached anybody, which are the two
          // things a reader most needs told apart.
          el("span", {class: rowChipClass(q) + " mc-chip-sm", text: rowChipText(q)}),
        ]),
        el("p", {class: "mc-qtext", text: q.question || ""}),
        el("p", {class: "mc-sub", text: (q.asked_by_label || "")
          + (q.created_at ? " \u00b7 " + ago(q.created_at) : "")}),
        // An answer you cannot see is one you do not know arrived. The row
        // showed only the question, so the whole visible change on being
        // answered was a colour going away -- which reads as nothing having
        // happened. The first line of the reply is the signal.
      ].concat(q.answer ? [el("p", {class: "mc-answer-peek", text: firstLine(q.answer)})] : []));
    });
  }

  //: Where a file for this screen goes: the coordinator's own session. Absent
  //: over a share link, which is what disables attaching there.
  function uploadTarget() {
    var p = currentPillar();
    return (p && p.coordinator_session) || state.coordinator_session || "";
  }

  //: ATTACHMENTS SURVIVE A REDRAW. render() rebuilds the whole chrome, and it
  //: runs on every live event and on the presence heartbeat -- so a strip of
  //: thumbnails held inside the composer was destroyed seconds after being
  //: drawn, by something the reader did nothing to cause. Typing was already
  //: carried across a redraw for exactly this reason; picking a file is the
  //: same act and was not. Keyed by surface so switching screens does not
  //: carry someone's files somewhere they did not attach them.
  var pendingFiles = Object.create(null);

  //: WHAT WAS TYPED OUTLIVES EVERYTHING THE PAGE DOES ON ITS OWN. A redraw
  //: preserved the textarea only when it still had focus -- and tapping Send
  //: moves focus to the button, so a live event arriving while a message was
  //: in flight rebuilt an empty box and took the unsent text with it. What
  //: the reader saw was a long answer vanishing on click with no error, which
  //: is indistinguishable from having been sent.
  //:
  //: Mirrored to localStorage for the same reason the session viewer does it:
  //: on a phone a backgrounded page can be evicted outright, and a draft that
  //: only exists in memory dies with it.
  var _DRAFTS = "mc-draft:";

  function draftGet(slot) {
    try { return window.localStorage.getItem(_DRAFTS + slot) || ""; }
    catch (_e) { return ""; }
  }

  function draftSet(slot, text) {
    try {
      if (text) window.localStorage.setItem(_DRAFTS + slot, text);
      else window.localStorage.removeItem(_DRAFTS + slot);
    } catch (_e) { /* private mode, quota -- the in-page value still stands */ }
  }

  //: A refusal has to survive the next redraw too, or the reader is told
  //: nothing at all about why their message did not go.
  var sendErrors = Object.create(null);

  //: AND SO DOES A SEND THAT IS STILL GOING. render() empties the chrome and
  //: rebuilds it on every live event, so a send that resolved against
  //: CAPTURED NODES wrote its outcome into elements that had already been
  //: thrown away: the reader was left on "Sending..." for a message that had
  //: in fact arrived, and the text was never cleared from a box that no
  //: longer existed. Observed live during an event storm -- 34 questions
  //: relaying at once -- where redraws land continuously and an in-flight
  //: send almost never survives to its own callback.
  //:
  //: In flight is state, keyed like every other per-slot fact here, so a
  //: rebuild renders it faithfully instead of losing it.
  var sendPending = Object.create(null);

  function pendingKey() {
    return (ui.entry ? "e:" + ui.entry
            : ui.anchor ? "a:" + ui.anchor
            : ui.post ? "p:" + ui.post.post_id
            : ui.panel ? "panel:" + ui.panel
            : "screen") + "|" + (state.screen || "");
  }

  function composer(placeholder, note, label, onSend) {
    var ta = el("textarea", {class: "mc-ta", rows: "2", placeholder: placeholder});
    // ---- attachments ------------------------------------------------------
    // Picked files are shown from memory before anything is sent, and upload
    // in the background while the message is still being written. The send
    // waits on them, so a large picture delays the picture and never the
    // message.
    var slot = pendingKey();
    if (!pendingFiles[slot]) pendingFiles[slot] = [];
    var picked = pendingFiles[slot];
    // NOT .mc-strip: that class is the SECTION NAVIGATOR, position:fixed
    // under the bar — reusing it pinned this (usually empty) thumbnail row
    // as an 11px dead band below every composer view's header, and sent
    // picked thumbnails to the top of the screen instead of the composer.
    var strip = el("div", {class: "mc-files"});
    var target = uploadTarget();

    function drawStrip() {
      strip.textContent = "";
      strip.style.display = picked.length ? "flex" : "none";
      picked.forEach(function (item, i) {
        var tile = el("div", {class: item.path ? "mc-thumb" : "mc-thumb mc-thumb-busy"});
        if (item.dataUrl) {
          tile.style.backgroundImage = "url(" + item.dataUrl + ")";
        } else {
          tile.appendChild(el("span", {class: "mc-thumb-ext", text: item.ext}));
        }
        tile.appendChild(el("button", {
          class: "mc-thumb-x", text: "\u00d7", title: "Remove",
          onclick: function () { picked.splice(i, 1); drawStrip(); },
        }));
        strip.appendChild(tile);
      });
    }

    function addFiles(files) {
      if (!target || !files || !files.length) return;
      Array.prototype.forEach.call(files, function (file) {
        var item = {name: file.name || "file", path: null,
                    ext: (file.name || "").split(".").pop().slice(0, 4).toUpperCase()};
        picked.push(item);
        // Straight from memory -- no server involved, so this looks the same
        // however the page was opened.
        if (/^image\//.test(file.type)) {
          var reader = new FileReader();
          reader.onload = function () { item.dataUrl = reader.result; drawStrip(); };
          reader.readAsDataURL(file);
        }
        drawStrip();
        var body = new FormData();
        body.append("file", file);
        body.append("tmux_session", target);
        fetch("/api/upload", {method: "POST", body: body, credentials: "same-origin"})
          .then(function (r) { return r.json(); })
          .then(function (d) {
            var meta = (d && d.files && d.files[0]) || d || {};
            item.path = meta.path || meta.host_path || null;
            if (!item.path) throw new Error("no path");
            drawStrip();
          })
          .catch(function () {
            item.failed = true;
            var at = picked.indexOf(item);
            if (at !== -1) picked.splice(at, 1);
            drawStrip();
            status.textContent = "Could not attach " + item.name;
          });
      });
    }

    drawStrip();

    ta.addEventListener("paste", function (e) {
      var files = [];
      Array.prototype.forEach.call((e.clipboardData || {}).items || [], function (it) {
        if (it.kind === "file") { var f = it.getAsFile(); if (f) files.push(f); }
      });
      if (files.length) { e.preventDefault(); addFiles(files); }
    });
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
    ta.value = draftGet(pendingKey());
    ta.addEventListener("input", function () { draftSet(pendingKey(), ta.value); });
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
    // Drawn FROM state every time, so a rebuild mid-send shows the send is
    // still going instead of quietly reverting to the idle hint.
    var status = el("span", {class: sendErrors[pendingKey()] ? "mc-sub mc-sub-bad" : "mc-sub",
                             text: sendPending[slot] ? "Sending\u2026"
                                   : (sendErrors[pendingKey()] || why || note)});
    var send = el("button", {
      class: "mc-send", text: label,
      onclick: function () {
        var text = ta.value.trim();
        if (!text) return;
        // A picture still going up is the one thing worth waiting for. Sending
        // now would deliver a message referring to a file that is not there.
        if (picked.some(function (x) { return !x.path; })) {
          status.textContent = "Still attaching\u2026";
          return;
        }
        // The paths go above the message, which is how a session receives an
        // attachment everywhere else on this dashboard.
        var body = picked.length
          ? picked.map(function (x) { return x.path; }).join("\n") + "\n\n" + text
          : text;
        // One send at a time per slot. A second tap while the first is
        // still going would post the same message twice.
        if (sendPending[slot]) return;
        sendPending[slot] = true;
        delete sendErrors[slot];
        status.textContent = "Sending\u2026";
        send.disabled = true;
        // EVERY OUTCOME IS WRITTEN TO STATE FIRST, THEN DRAWN. The nodes
        // captured by this closure may already be detached by the time the
        // request settles -- that is the defect this shape exists to remove
        // -- so nothing here depends on them still being on screen.
        Promise.resolve(onSend(body)).then(function () {
          // Only a confirmed success discards what was written.
          //
          // BOTH the box and the stored draft, and the box is not optional
          // even though the redraw below rebuilds it: render() preserves
          // whatever is in the FOCUSED textarea across a rebuild, so a box
          // left populated here has its contents faithfully restored and the
          // message the reader just sent successfully reappears unsent.
          ta.value = "";
          draftSet(slot, "");
          picked.length = 0; delete pendingFiles[slot];
        }, function (err) {
          sendErrors[slot] = "Not sent: " + ((err && err.message) || "refused")
            + " \u2014 your message is still here.";
          // Never silent, and never lost: the text stays in the draft, and
          // the reason survives the next redraw.
        }).then(function () {
          delete sendPending[slot];
          // Redraw from state. This is what puts the outcome on the screen
          // the reader is actually looking at, whether or not it is the one
          // this closure was built against.
          render();
        });
      },
    });
    // Disabled from STATE, not just from the click handler: a rebuild during
    // an in-flight send would otherwise draw a fresh, inviting button for a
    // message that is already on its way.
    if (why || sendPending[slot]) send.disabled = true;
    var picker = el("input", {class: "mc-file", type: "file", multiple: "multiple"});
    picker.addEventListener("change", function (e) {
      addFiles(e.target.files); e.target.value = "";
    });
    var clip = el("button", {
      class: "mc-clip", text: "+",
      // Absent rather than disabled when there is nowhere to send a file:
      // over a shared link there is no session to deliver it to.
      title: target ? "Attach" : "",
      onclick: function () { picker.click(); },
    });
    var foot = el("div", {class: "mc-foot"}, [
      strip, ta,
      el("div", {class: "mc-foot-row"},
         target ? [clip, status, send] : [status, send]),
    ]);
    if (target) {
      foot.appendChild(picker);
      ["dragenter", "dragover"].forEach(function (ev) {
        foot.addEventListener(ev, function (e) {
          e.preventDefault(); foot.classList.add("mc-foot-drop");
        });
      });
      ["dragleave", "drop"].forEach(function (ev) {
        foot.addEventListener(ev, function (e) {
          e.preventDefault(); foot.classList.remove("mc-foot-drop");
          if (ev === "drop") addFiles((e.dataTransfer || {}).files);
        });
      });
    }
    return foot;
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
    {key: "open", label: "Open"},
    {key: "done", label: "Answered"},
  ];

  // Three labeled tabs with counts. This was ONE chip that cycled on tap,
  // and nobody knew it was tappable; there is room now, so every state is
  // its own visible control.
  function filterToggle() {
    var all = questionsHere();
    var open = all.filter(isOpen).length;
    var counts = {all: all.length, open: open, done: all.length - open};
    return [el("div", {class: "mc-seg"}, FILTERS.map(function (f) {
      return el("button", {
        class: qFilter === f.key ? "mc-seg-on" : "",
        text: f.label + " " + counts[f.key],
        onclick: function () { qFilter = f.key; render(); },
      });
    }))];
  }

  //: Everything on this screen waiting on the reader, in the order it appears
  //: on the page -- which is the order the author put it in, and the only
  //: order that means anything to someone about to read the screen itself.
  function forYouRows() {
    var waiting = unanswered();
    if (!waiting.length) {
      return [el("p", {class: "mc-empty", text: "Nothing is waiting on you here."})];
    }
    return waiting.map(function (c) {
      return el("button", {class: "mc-row", onclick: function () {
        show({anchor: c.ref, from: {panel: "foryou"}});
      }}, [
        el("div", {class: "mc-row-top"}, [
          el("span", {class: "mc-chip mc-chip-open", text: "answer"}),
          el("span", {class: "mc-sub", text: c.about || ""}),
        ]),
        el("p", {class: "mc-qtext", text: c.asks}),
      ]);
    });
  }

  var PANEL_TITLE = {pillars: "", views: "View",
                     questions: "", foryou: "Asks for you"};

  function viewRows() {
    var decl = [];
    try { decl = JSON.parse(document.body.dataset.mcViews || "[]"); }
    catch (_e) { decl = []; }
    var cur = document.body.dataset.mcView || "";
    return decl.map(function (vw) {
      return el("button", {
        class: vw.id === cur ? "mc-row mc-row-on" : "mc-row",
        onclick: function () {
          document.dispatchEvent(new CustomEvent("mc:goto-view", {detail: {id: vw.id}}));
          show(null);
        },
      }, [el("div", {class: "mc-row-top"}, [
        el("span", {class: "mc-name", text: vw.label || vw.id}),
      ])]);
    });
  }

  // TWO DIRECTIONS, ONE PANEL. Questions FOR THE MISSION are conversations
  // people opened that a coordinator owes or gave an answer; questions FOR
  // YOU are what the screen's content asks the reader to decide. Push and
  // pull of the same thing, so they share one panel behind a direction
  // toggle. Each direction keeps its own filters beneath it.
  var qaMode = "mission";

  function qaToggle() {
    var forYou = unanswered().length;
    var open = openCount();
    return [el("div", {class: "mc-seg"}, [
      el("button", {class: qaMode === "mission" ? "mc-seg-on" : "",
                    text: "Q&A" + (open ? " " + open : ""),
                    onclick: function () { qaMode = "mission"; render(); }}),
      el("button", {class: qaMode === "you" ? "mc-seg-on" : "",
                    text: "For you" + (forYou ? " " + forYou : ""),
                    onclick: function () { qaMode = "you"; render(); }}),
    ])];
  }

  // ONE LIST, TWO ORDERINGS. "By pillar" is the snapshot: one tile per
  // pillar, its latest report. "By time" is the history: every report,
  // newest first. Same tiles, same panel, one toggle -- they were two bar
  // buttons opening two differently-shaped surfaces of the same posts,
  // and nobody could say how they differed.
  var statusMode = "pillar";

  function statusToggle() {
    return [el("div", {class: "mc-seg"}, ["pillar", "time"].map(function (m) {
      return el("button", {
        class: statusMode === m ? "mc-seg-on" : "",
        text: m === "pillar" ? "By Pillar" : "Mission",
        onclick: function () { statusMode = m; render(); },
      });
    }))];
  }

  function byTimeRows() {
    var posts = state.status_posts || [];
    if (!posts.length)
      return [el("p", {class: "mc-empty",
                       text: "No pillar has reported anything yet."})];
    return posts.map(function (post) {
      var dot = el("span", {class: "mc-swatch"});
      dot.style.background = post.color || "#475569";
      var replies = atAnchor(postAnchor(post)).map(function (q) {
        return el("button", {class: "mc-post-reply", onclick: function () {
          show({entry: q.entry_id, from: {panel: "pillars"}});
        }}, [
          el("p", {class: "mc-post-q", text: q.question || ""}),
          q.answer
            ? el("p", {class: "mc-post-a", text: firstLine(q.answer)})
            : el("span", {class: "mc-chip mc-chip-open", text: rowChipText(q)}),
        ]);
      });
      return el("div", {class: "mc-row mc-row-static"}, [
        el("div", {class: "mc-row-top"}, [
          dot,
          el("span", {class: "mc-name", text: post.pillar_name || ""}),
          el("span", {class: "mc-grow"}),
          el("span", {class: "mc-age", text: post.ago || ""}),
        ]),
        el("p", {class: "mc-last", text: post.text || ""}),
        el("div", {class: "mc-post-foot"}, [
          el("button", {class: "mc-post-act", text: "Reply",
                        onclick: function () { show({post: post}); }}),
        ]),
      ].concat(replies));
    });
  }

  function panelNode() {
    if (!ui.panel) return null;
    var body = ui.panel === "pillars"
                 ? (statusMode === "time" ? byTimeRows() : pillarRows())
             : ui.panel === "views" ? viewRows()
             : ui.panel === "foryou" ? forYouRows()
             : qaMode === "you" ? forYouRows()
             : [el("div", {class: "mc-qfilter"}, filterToggle())]
                 .concat(questionRows());
    var kids = [
      el("div", {class: "mc-phead"}, ((PANEL_TITLE[ui.panel] || "") === "" ? [] : [
        el("span", {class: "mc-ptitle", text: PANEL_TITLE[ui.panel]}),
      ]).concat(ui.panel === "questions" ? qaToggle() : [])
       .concat(ui.panel === "pillars" ? statusToggle() : []).concat([
        el("span", {class: "mc-grow"}),
        el("button", {class: "mc-x", text: "\u00d7", onclick: function () { show(null); }}),
      ])),
      el("div", {class: "mc-pbody"}, body),
    ];
    if (ui.panel === "questions" && qaMode !== "you") {
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
    if (ui.from && ui.from.panel === "foryou") return "\u2039 For you";
    // Opened from the feed, so back is the feed -- not "back to the mission",
    // which would throw away the place in the list you had scrolled to.
    if (ui.from && (ui.from.view === "feed" || ui.from.post)) {
      return "\u2039 What is happening";
    }
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
  function pillarColor(pid) {
    var p = (state.pillars || []).filter(function (x) { return x.pillar_id === pid; })[0];
    return (p && p.color) || "#64748b";
  }

  function ago(ts) {
    var n = typeof ts === "number" ? ts : parseFloat(ts);
    if (!isFinite(n)) return "";
    var sec = Math.max(0, Date.now() / 1000 - n);
    if (sec < 60) return "just now";
    if (sec < 3600) return Math.round(sec / 60) + "m ago";
    if (sec < 86400) return Math.round(sec / 3600) + "h ago";
    return Math.round(sec / 86400) + "d ago";
  }

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

  //: What each kind of round looks like in the margin. A receipt is the
  //: platform speaking, progress is someone working, a message is someone
  //: talking -- three different things that all belong in one column.
  var ROUND_MARK = {message: "\u201C", status: "\u25CF", echo: "\u2713"};

  function entryNode() {
    if (!ui.entry) return null;
    var q = state.questions.filter(function (x) { return x.entry_id === ui.entry; })[0];
    if (!q) return null;
    // WHERE THIS LIVES. The list shows the pillar; losing it on tap-through
    // strands the reader mid-conversation with no context.
    var ctxStripe = el("span", {class: "mc-swatch"});
    ctxStripe.style.background = pillarColor(q.pillar_id);
    var body = [
      el("div", {class: "mc-qctx"}, [ctxStripe,
        el("span", {class: "mc-qpillar",
                    text: pillarName(q.pillar_id) || state.mission || ""})]),
      el("p", {class: "mc-label", text: "Question"}),
    ];
    paras(q.question, "mc-qbig").forEach(function (n) { body.push(n); });
    body.push(el("p", {class: "mc-sub",
                       text: (q.asked_by_label || "") + (q.created_at ? " \u00b7 " + ago(q.created_at) : "")}));
    // Only what the screen's author CALLED the anchor. The raw slug is a
    // name for code, and rendered here it read as a stray UUID.
    if (q.anchor_title) {
      body.push(el("p", {class: "mc-sub", text: q.anchor_title}));
    }
    // AND WHAT IT SAID. A question read back without the thing it replies to
    // is half a conversation -- the reader has to go and find the screen, and
    // the screen may have been rewritten since.
    if (q.anchor_excerpt) {
      body.push(el("p", {class: "mc-label mc-mt", text: "In reply to"}));
      body.push(el("blockquote", {class: "mc-quote", text: q.anchor_excerpt}));
    }
    // THE ROUNDS, IN ORDER. This showed a question, then a bare delivery
    // line, then a separate block of progress, then an answer -- four
    // sections that happened to be about the same exchange. What a reader
    // could not get from it was the SEQUENCE: asked, delivered, worked on,
    // replied, worked on again. That is the whole of what an open
    // conversation tells you, and it was the one thing not on the screen.
    var rounds = q.updates || [];
    if (rounds.length) {
      body.push(el("p", {class: "mc-label mc-mt", text: "Discussion"}));
      rounds.forEach(function (u, i) {
        var kind = u.kind || "status";
        var live = isOpen(q) && !q.answer && i === rounds.length - 1
                   && kind === "status";
        body.push(el("div", {class: live ? "mc-step mc-step-live" : "mc-step"}, [
          el("span", {class: "mc-step-ic mc-ic-" + kind, text: ROUND_MARK[kind] || "•"}),
          el("div", {}, [
            el("div", {class: kind === "message" ? "mc-step-tx mc-round-said"
                                                 : "mc-step-tx",
                       text: u.text || ""}),
            el("div", {class: "mc-step-at",
                       text: (u.author_label ? u.author_label + " · " : "")
                             + ago(u.created_at) + (live ? " · now" : "")}),
          ]),
        ]));
      });
    }
    if (isOpen(q) && !rounds.length && q.relay_status !== "sent") {
      // Nothing has happened yet, so say only the one thing that is true.
      body.push(el("p", {class: q.relay_status === "failed"
                                ? "mc-relay mc-relay-bad" : "mc-relay",
                         text: q.relay_status === "failed"
                               ? "Not delivered — nobody has been told about this yet."
                               : "Sending…"}));
    }
    if (q.answer) {
      // The byline sits ON the answer, not floating between blocks where
      // it reads as the question's.
      body.push(el("p", {class: "mc-label mc-mt",
        text: "Answer" + (q.answered_at ? " \u00b7 " + ago(q.answered_at) : "")}));
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
        el("span", {class: rowChipClass(q) + " mc-chip-sm", text: rowChipText(q)}),
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
    reply:    {hint: "Reply\u2026",                    label: "Reply"},
    answer:   {hint: "Answer\u2026",                   label: "Answer"},
    followup: {hint: "Add to your question\u2026",     label: "Add"},
    reopen:   {hint: "Reopen with a follow-up\u2026",  label: "Reopen"},
  };

  function postNode() {
    if (!ui.post) return null;
    var post = ui.post;
    var ref = postAnchor(post);
    var dot = el("span", {class: "mc-swatch"});
    dot.style.background = post.color || "#475569";
    var body = [
      el("div", {class: "mc-post-head"}, [
        dot,
        el("span", {class: "mc-post-who", text: post.pillar_name || ""}),
        el("span", {class: "mc-age", text: post.ago || ""}),
      ]),
      el("p", {class: "mc-qbig mc-mb", text: post.text || ""}),
    ];
    atAnchor(ref).forEach(function (q) {
      body.push(el("button", {class: "mc-row", onclick: function () {
        show({entry: q.entry_id, from: {post: post}});
      }}, [
        el("span", {class: isOpen(q) ? "mc-chip mc-chip-open" : "mc-chip",
                    text: rowChipText(q)}),
        el("p", {class: "mc-qtext", text: q.question || ""}),
      ]));
    });
    return el("section", {class: "mc-view"}, [
      el("div", {class: "mc-phead"}, [
        el("button", {class: "mc-back", text: "\u2039 What is happening",
                      onclick: function () { show({view: "feed"}); }}),
      ]),
      el("div", {class: "mc-pbody"}, body),
      composer("Reply to " + (post.pillar_name || "this") + "\u2026",
               "goes to " + (post.pillar_name || "the pillar"), "Send",
               function (t) { ask(t, ref, post.pillar_id, firstLine(post.text),
                                  post.text); }),
    ]);
  }

  function anchorNode() {
    if (!ui.anchor) return null;
    var here = atAnchor(ui.anchor);
    var screenAsk = asked(ui.anchor);
    var answered = askAnswered(screenAsk);
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
      var namedBy = (control && control.about)
        || (here[0] && here[0].anchor_title) || ui.anchor;
      body = [el("p", {class: "mc-label", text: "about"}),
              el("p", {class: "mc-qbig", text: namedBy})];
      // The thing being replied to, on the screen where the reply is written.
      // Without it this panel names a heading and shows the conversation,
      // while the paragraph that prompted it sits behind the panel.
      var said = (control && control.excerpt)
        || (here[0] && here[0].anchor_excerpt) || "";
      if (said) body.push(el("blockquote", {class: "mc-quote mc-mb", text: said}));
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
  function ask(text, anchor, pillarId, about, excerpt) {
    var body = {kind: "question", question: text};
    // THE HEADING TRAVELS WITH IT. The page knows what an anchor sits under;
    // until it sent that, the only context reaching a coordinator was the
    // slug the screen's author picked, and the reminder afterwards dropped
    // even the slug. A short answer to something the screen raised arrived as
    // two words and an identifier.
    var ctl = anchorControls.filter(function (x) { return x.ref === anchor; })[0];
    var title = about || (ctl && ctl.about) || "";
    if (title) body.anchor_title = title;
    var said = (ctl && ctl.excerpt) || excerpt || "";
    if (said) body.anchor_excerpt = said;
    // THE POST'S PILLAR, NOT THE SCREEN'S. A reply to something in the feed
    // goes to whoever wrote it, which is usually not the screen being read --
    // defaulting to the current one would deliver it to the wrong coordinator
    // and look, from both ends, like it had been sent correctly.
    var p = pillarId ? {pillar_id: pillarId} : currentPillar();
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
    return mine(q) ? "followup" : "reply";
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
    var ctl = anchorControls.filter(function (x) { return x.ref === anchorRef; })[0];
    return request("write", {kind: "asked", anchor: anchorRef,
                             anchor_title: (ctl && ctl.about) || "",
                             question: question, answer: text})
      .then(function (r) {
        mergeEntry(r && r.question);
        repaintAnchors();
        show({anchor: anchorRef});
      });
  }

  function reply(entryId, text, kind) {
    var body = {kind: kind, entry_id: entryId};
    body[kind === "reply" ? "text" : kind === "answer" ? "answer" : "followup"] = text;
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

  //: A render counter on the host element. The chrome is in a CLOSED shadow
  //: root, deliberately -- coordinator scripts share this realm, so anything
  //: reachable from outside is reachable by them. That also means nothing
  //: outside can see the chrome redraw, so "a live event actually repainted
  //: this page" was untestable in a real browser and went unproven. A counter
  //: is not a capability: it carries no state, exposes no port, and moving it
  //: tells an observer only that a redraw happened -- which is exactly the
  //: fact worth being able to check.
  var renders = 0;

  function render() {
    host.setAttribute("data-mc-render", String(++renders));
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
    var covered = ui.panel || ui.entry || ui.anchor || ui.view || ui.post;
    document.documentElement.style.overflow = covered ? "hidden" : "";
    var strip = covered ? null : stripNode();
    if (strip) chrome.appendChild(strip);
    if (ui.who) chrome.appendChild(whoList());
    var p = panelNode(); if (p) chrome.appendChild(p);
    var e = entryNode(); if (e) chrome.appendChild(e);
    var a = anchorNode(); if (a) chrome.appendChild(a);
    var sp = postNode(); if (sp) chrome.appendChild(sp);
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
    if (control.asks && !askAnswered(control)) {
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
  //: What an anchored element says, without the chrome mounted inside it and
  //: capped so a whole screen cannot ride along on one question.
  function textOf(target) {
    var clone = target.cloneNode(true);
    clone.querySelectorAll("[data-mc-control]").forEach(function (n) { n.remove(); });
    // Decoration is not what the screen SAID: an author can mark badges,
    // timestamps and ref chips out of the excerpt.
    clone.querySelectorAll("[data-mc-noexcerpt]").forEach(function (n) { n.remove(); });
    // textContent joins element boundaries with NOTHING, which is fine for
    // authored HTML (inter-tag whitespace survives) and garbage for
    // DOM-built screens (no whitespace nodes at all): "BlockedThree things
    // wait...me...at.updated Aug 22" was one real excerpt. A space after
    // every element makes boundaries survive; the collapse below dedupes.
    clone.querySelectorAll("*").forEach(function (n) {
      try { n.insertAdjacentText("afterend", " "); } catch (_e) {}
    });
    var text = (clone.textContent || "").replace(/\s+/g, " ").trim();
    return text.length > 600 ? text.slice(0, 597) + "\u2026" : text;
  }

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

  // AN ASK IS ANSWERED WHEN THIS QUESTION HAS BEEN ANSWERED, not when the
  // element it sits on has ever been. Keyed on the anchor, an answered ask
  // silenced its own element for good: the reader answering with a question
  // of their own -- the most ordinary outcome there is -- left the screen with
  // no way to ask again, and rewriting the attribute changed nothing. The
  // question lives in the attribute, so changing it asks a different one.
  function askAnswered(control) {
    if (!control || !control.asks) return false;
    return atAnchor(control.ref).some(function (q) {
      return q.answer && q.question === control.asks;
    });
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
      return c.asks && !askAnswered(c);
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
      // AND WHAT IT SAYS. A heading names the thing; the reader is replying
      // to its CONTENT. Answering a status report or a decision on a screen
      // meant looking at a panel headed "Now" with no sight of the paragraph
      // that prompted it -- and the question, once asked, carried no trace of
      // what it was about either.
      var excerpt = textOf(target);
      var holder = document.createElement("span");
      holder.setAttribute("data-mc-control", "");
      var r = holder.attachShadow({mode: "closed"});
      r.innerHTML = "<style>" + CSS + "</style>";
      var btn = el("button", {
        class: "mc-anchor-btn", title: asks ? "Answer this" : "Discuss",
        // stopPropagation: a real tap on the chip must not ALSO reach the
        // host forwarder below (re-entry) or an author's own card-level
        // tap handler (double open).
        onclick: function (ev) { ev.stopPropagation(); show({anchor: ref}); },
      });
      var control = {ref: ref, btn: btn, asks: asks, about: about,
                     excerpt: excerpt};
      anchorControls.push(control);
      paintAnchor(control);
      r.appendChild(btn);
      // THE HOST FORWARDS INWARD. The root is closed on purpose (author
      // code must not reach the control's internals) — but that also made
      // the control unpressable from outside: an author surface that wants
      // its whole card to act as the tap target can only click the host
      // span, whose listeners-inside-shadow never hear it. Clicks landing
      // on the host (author forwarding, or synthetic clicks retargeted to
      // it) now press the real button; real chip taps never re-enter
      // because the button stops propagation.
      holder.addEventListener("click", function () { btn.click(); });
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
    if (!RELAY_FRAME) {
      var base = "/missions/" + (state.mission_id || "");
      var url = pillarId ? base + "/pillars/" + pillarId : base;
      // Inside the SPA's frame an assign() adds a JOINT history entry the
      // app never pushed — the operator swipe-counted their way out of a
      // stack of them. The frame's internal moves must leave no trail.
      if (window.parent !== window) location.replace(url);
      else location.assign(url);
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
    // Deep link: ?question=<entry_id> opens that conversation's thread on
    // arrival, so a link in a session transcript lands the reader on the
    // question being answered — and live events then play the updates and
    // the answer into the open thread. Real-URL surfaces only: a srcdoc
    // frame has no query string of its own.
    if (!RELAY_FRAME) {
      try {
        var qid = new URLSearchParams(location.search).get("question");
        if (qid && state.questions.some(function (x) { return x.entry_id === qid; }))
          show({entry: qid});
      } catch (_e) {}
    }
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
