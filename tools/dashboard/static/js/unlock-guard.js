/* Session-expiry guard for the SPA shell.
 *
 * The human unlock gate (tools/dashboard/unlock_routes.py) answers a
 * locked HUMAN request one of two ways: a top-level page navigation gets
 * a 302 to /unlock, but a fetch()-driven request (the SPA's /pages/*
 * fragment loaders, any /api call the chrome makes) gets a 401 JSON
 * carrying {"unlock": "/unlock"} — deliberately NOT a redirect, so the
 * SPA doesn't transparently follow it and inject the unlock page's HTML
 * into a fragment slot.
 *
 * Without this guard, those fragment loaders (app.js) do fetch().text()
 * with no status check and would render the 401 body as page content.
 * One global fetch wrapper turns any such 401 into the full-page
 * navigation the gate intends — covering every current and future
 * fetch site at once, so a session that expires mid-use lands on the
 * lock screen instead of showing garbage.
 *
 * Deliberately absent from the /unlock page itself (its own template
 * doesn't load base.html), so there is no redirect loop.
 */
(function () {
  'use strict';

  if (window.__autonomyUnlockGuardInstalled) return;
  window.__autonomyUnlockGuardInstalled = true;

  var _fetch = window.fetch.bind(window);
  var _redirecting = false;

  window.fetch = function (input, init) {
    return _fetch(input, init).then(function (resp) {
      if (resp.status === 401 && !_redirecting) {
        var unlock = resp.headers.get('x-autonomy-unlock');
        // Confirm it's OUR gate (not some unrelated 401 from a plugin or
        // upstream API) before hijacking the navigation.
        if (unlock) {
          _redirecting = true;
          var next = location.pathname + location.search;
          location.assign(unlock + '?next=' + encodeURIComponent(next));
        }
      }
      return resp;
    });
  };
})();
