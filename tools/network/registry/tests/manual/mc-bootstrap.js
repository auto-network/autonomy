
// Mission platform bootstrap. Runs before any coordinator script.
// Exposes NO global namespace: the port lives in this closure only.
(function () {
  "use strict";
  var scriptsAtBoot = document.scripts.length;   // 1 == we are first
  var domFired = false, loadFired = false, earlySawLate = null;

  // An early script must NOT see elements the parser has not reached yet.
  earlySawLate = !!document.getElementById("s1");

  document.addEventListener("DOMContentLoaded", function () { domFired = true; });
  window.addEventListener("load", function () { loadFired = true; report(); });

  var chan = new MessageChannel();
  var port = chan.port1;                          // never leaves this closure
  parent.postMessage({ mcPort: true }, "*", [chan.port2]);

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
        jump.click();
        // The author document sets scroll-behavior:smooth, so sample after
        // the animation settles, not during it. It also sets
        // scroll-margin-top, so the target lands near the top, not at 0.
        setTimeout(function () {
          topAfter = Math.round(target.getBoundingClientRect().top);
          send(mounted, cs, before, window.scrollY);
        }, 1400);
      }); });
    } else { send(mounted, cs, before, before); }
  }

  var jumpHref = null, topBefore = null, topAfter = null;
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
      targetTopBeforeAfter: topBefore + " -> " + topAfter,
      scrollY: afterY,
      leakedGlobal: typeof window.__MC__
    }});
  }
})();
