
// Mission platform bootstrap. Runs before any coordinator script.
// Exposes NO global namespace: the port lives in this closure only.
(function () {
  "use strict";
  var scriptsAtBoot = document.scripts.length;   // 1 == we are first
  // Snapshot globals before we do anything, so the check below is a real
  // diff rather than a probe for one name we chose ourselves.
  var globalsAtBoot = Object.getOwnPropertyNames(window).length;
  var domFired = false, loadFired = false, earlySawLate = null;

  // An early script must NOT see elements the parser has not reached yet.
  earlySawLate = !!document.getElementById("s1");

  document.addEventListener("DOMContentLoaded", function () { domFired = true; });
  window.addEventListener("load", function () {
    loadFired = true;
    try { report(); }
    catch (err) { port.postMessage({ type: "error", msg: String(err && err.stack || err) }); }
  });

  var chan = new MessageChannel();
  var port = chan.port1;                          // never leaves this closure
  parent.postMessage({ mcPort: true }, "*", [chan.port2]);

  // Liveness probe: sent before load, so a stalled phase can be told apart
  // from a phase that never booted.
  port.postMessage({ type: "alive", readyState: document.readyState });
  document.addEventListener("DOMContentLoaded", function () {
    port.postMessage({ type: "alive", readyState: "DOMContentLoaded" });
  });
  window.addEventListener("load", function () {
    port.postMessage({ type: "alive", readyState: "load" });
  });

  port.onmessage = function (e) {
    if (e.data && e.data.type === "navigate") {
      document.open();
      document.write(e.data.html);
      document.close();
    }
  };

  function mountControls() {
    var n = 0;
    document.querySelectorAll("[data-mc-anchor]").forEach(function (el) {
      var host = document.createElement("span");
      host.attachShadow({ mode: "closed" })
          .appendChild(document.createTextNode("● ask"));
      el.appendChild(host);   // INSIDE the anchored element
      n++;
    });
    return n;
  }

  function report() {
    var mounted = mountControls();
    scrollAtBoot = window.scrollY;
    var cs = getComputedStyle(document.body);
    // Pick an in-page link that is NOT already the current hash: after a
    // document.write the previous hash survives, and re-clicking the same
    // target is a no-op that would look like a scroll failure.
    // Pick a target that is genuinely OFF-SCREEN and not the current hash.
    // A target already at the top cannot move the scroll position, so
    // "did scrollY change" would report failure for a working browser.
    var jump = null, target = null;
    var links = document.querySelectorAll('a[href^="#"]');
    for (var i = 0; i < links.length; i++) {
      var h = links[i].getAttribute("href");
      if (h.length < 2 || h === location.hash) continue;
      var el = document.getElementById(h.slice(1));
      if (el && el.getBoundingClientRect().top > window.innerHeight) {
        jump = links[i]; target = el; break;
      }
    }
    var before = window.scrollY;
    jumpHref = jump ? jump.getAttribute("href") : null;
    if (jump) {
      requestAnimationFrame(function () { requestAnimationFrame(function () {
        topBefore = Math.round(target.getBoundingClientRect().top);
        hashBefore = location.hash;
        readyAtClick = document.readyState;
        jump.click();
        hashAfter = location.hash;
        // The author sets scroll-behavior:smooth, so poll until the
        // position stops changing instead of guessing a delay. A fixed
        // wait was flaky after a 1.8MB document.write: the same target
        // measured 3880 -> 20 on one run and 3880 -> 3880 on the next.
        var last = null, stable = 0, tries = 0;
        (function settle() {
          var now = Math.round(target.getBoundingClientRect().top);
          stable = (now === last) ? stable + 1 : 0;
          last = now;
          if (stable >= 2 || ++tries > 60) {
            topAfter = now;
            settleMs = tries * 100;
            try { return send(mounted, cs, before, window.scrollY); }
            catch (err) { return port.postMessage({ type: "error", msg: "send: " + String(err && err.stack || err) }); }
          }
          setTimeout(settle, 100);
        })();
      }); });
    } else { send(mounted, cs, before, before); }
  }

  var jumpHref = null, topBefore = null, topAfter = null, settleMs = null;
  var hashBefore = null, hashAfter = null, hashFired = false, readyAtClick = null;
  var scrollAtBoot = null;
  window.addEventListener("hashchange", function () { hashFired = true; });
  function send(mounted, cs, beforeY, afterY) {
    port.postMessage({ type: "report", data: {
      compatMode: document.compatMode,
      htmlLang: document.documentElement.lang,
      bodyMargin: cs.margin,
      bodyFontFamily: (cs.fontFamily || "").slice(0, 24),
      authorGlobal: (typeof window.OQ_DATA) + ":" +
                    (window.OQ_DATA ? window.OQ_DATA.length : 0),
      scriptsAtBoot: scriptsAtBoot,
      earlyScriptSawLaterElement: earlySawLate,
      domContentLoadedFired: domFired,
      loadFired: loadFired,
      anchorsFound: document.querySelectorAll("[data-mc-anchor]").length,
      controlsMounted: mounted,
      // The target landed at the top of the viewport: what "scrolled to
      // the fragment" actually means, and independent of which element is
      // the scrolling box on a given engine.
      fragmentScrolled: topAfter !== null && topAfter < topBefore
                        && topAfter >= -4 && topAfter < 120,
      fragmentTarget: jumpHref,
      targetTopBeforeAfter: topBefore + " -> " + topAfter + " (settled " + settleMs + "ms)",
      hashBeforeAfter: hashBefore + " -> " + hashAfter + (hashFired ? " (hashchange)" : " (NO hashchange)"),
      readyStateAtClick: readyAtClick,
      scrollInheritedAtBoot: scrollAtBoot,
      scrollY: afterY,
      // Own-property count on window, boot -> report. Catches any global
      // this bootstrap adds, not just a name it knows to look for. It does
      // NOT isolate ours from the author's, so it is only meaningful
      // alongside reading the IIFE.
      globalsAddedByAnyone: Object.getOwnPropertyNames(window).length - globalsAtBoot,
      probeMC: typeof window.__MC__
    }});
  }
})();
