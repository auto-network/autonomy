// Test harness — exposes window.__ui (read-only state) and window.__test (actions + wait utilities)
// for agent-browser testing. Always available, not just in mock mode.

(function () {

  // ── window.__ui — read-only state queries ──

  window.__ui = {
    page: function () { return location.pathname; },

    actionSheetOpen: function () {
      var el = document.querySelector('[x-data*="sessionsPage"]');
      if (!el || !el._x_dataStack) return false;
      var data = Alpine.$data(el);
      return data?.actionSheet?.open ?? false;
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

    longPressSession: function (tmuxName) {
      var el = document.querySelector('[x-data*="sessionsPage"]');
      if (!el || !el._x_dataStack) return false;
      var page = Alpine.$data(el);
      if (!page) return false;
      var s = (page.interactive || []).find(function (s) { return s.tmux_session === tmuxName; })
           || (page.agents || []).find(function (s) { return s.tmux_session === tmuxName; });
      if (s && page.showCloseConfirm) { page.showCloseConfirm(s); return true; }
      return false;
    },

    dismissActionSheet: function () {
      var el = document.querySelector('[x-data*="sessionsPage"]');
      if (!el || !el._x_dataStack) return false;
      var page = Alpine.$data(el);
      if (page && page.closeActionSheet) { page.closeActionSheet(); return true; }
      return false;
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
