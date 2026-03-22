// Test harness — exposes window.__ui (read-only state) and window.__test (actions + wait utilities)
// for agent-browser testing. Always available, not just in mock mode.

(function () {

  // ── window.__ui — read-only state queries ──

  window.__ui = {
    page: function () { return location.pathname; },

    actionSheetOpen: function () {
      return window.actionSheet ? window.actionSheet.isOpen() : false;
    },

    sessions: function () {
      var store = Alpine.store('sessions');
      return store ? Object.keys(store) : [];
    },

    sessionCount: function () {
      var store = Alpine.store('sessions');
      return store ? Object.keys(store).length : 0;
    },

    getSession: function (id) {
      var store = Alpine.store('sessions');
      return store ? (store[id] || null) : null;
    },

    currentSessionId: function () {
      var el = document.querySelector('[data-session-id]');
      return el ? el.dataset.sessionId : undefined;
    },

    navBadges: function () {
      var get = function (id) {
        var el = document.getElementById(id);
        return el ? el.textContent.trim() : '';
      };
      return {
        beads: get('badge-beads'),
        sessions: get('badge-sessions'),
        dispatch: get('badge-dispatch'),
        timeline: get('badge-timeline'),
        terminal: get('badge-terminal'),
      };
    },
  };

  // ── window.__test — semantic actions + wait utilities ──

  window.__test = {

    // Navigation
    navigateTo: function (path) { return navigateTo(path); },

    // Session interactions
    tapSession: function (tmuxName) {
      var card = document.querySelector('[data-session-id="' + tmuxName + '"]');
      if (card) { card.click(); return true; }
      return false;
    },

    closeSession: function (tmuxName) {
      var card = document.querySelector('[data-session-id="' + tmuxName + '"]');
      var btn = card ? card.querySelector('[data-testid="session-close-btn"]') : null;
      if (btn) { btn.click(); return true; }
      return false;
    },

    dismissActionSheet: function () {
      return window.actionSheet.dismiss();
    },

    // Wait utilities
    waitFor: function (fn, timeoutMs) {
      if (timeoutMs === undefined) timeoutMs = 5000;
      return new Promise(function (resolve) {
        var start = Date.now();
        var check = function () {
          if (fn()) return resolve(true);
          if (Date.now() - start > timeoutMs) return resolve(false);
          requestAnimationFrame(check);
        };
        check();
      });
    },

    waitForPage: function (path, timeoutMs) {
      if (timeoutMs === undefined) timeoutMs = 5000;
      return window.__test.waitFor(function () { return location.pathname === path; }, timeoutMs);
    },

    waitForActionSheet: function (timeoutMs) {
      if (timeoutMs === undefined) timeoutMs = 3000;
      return window.__test.waitFor(function () { return window.__ui.actionSheetOpen(); }, timeoutMs);
    },

    waitForSessionCount: function (count, timeoutMs) {
      if (timeoutMs === undefined) timeoutMs = 5000;
      return window.__test.waitFor(function () { return window.__ui.sessionCount() >= count; }, timeoutMs);
    },
  };

})();
