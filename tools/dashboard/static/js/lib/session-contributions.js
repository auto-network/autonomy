// Plugin-owned badges and icon actions shared by session cards and viewers.
//
// One batched core request fans out to enabled plugin callbacks. Relationship
// knowledge never leaks into this client: it only caches normalized renderer
// descriptors and refreshes a session when a plugin publishes the common SSE
// topic.
(function () {
  window.Autonomy = window.Autonomy || {};

  var loaded = {};
  var inFlight = {};

  function store() {
    if (!window.Alpine || typeof Alpine.store !== 'function') return null;
    return Alpine.store('sessionContributions');
  }

  function cleanIds(values) {
    var seen = {};
    var out = [];
    (values || []).forEach(function (value) {
      var id = String(value || '').trim();
      if (!id || seen[id]) return;
      seen[id] = true;
      out.push(id);
    });
    return out;
  }

  var api = {
    async load(sessionIds, options) {
      var force = !!(options && options.force);
      var ids = cleanIds(sessionIds).filter(function (id) {
        return !inFlight[id] && (force || !loaded[id]);
      });
      if (!ids.length) return;
      ids.forEach(function (id) { inFlight[id] = true; });
      try {
        var fetcher = (window.Autonomy && window.Autonomy.fetch) || window.fetch;
        var response = await fetcher('/api/session-contributions', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({session_ids: ids}),
        });
        if (!response.ok) return;
        var body = await response.json();
        var sessions = body && body.sessions && typeof body.sessions === 'object'
          ? body.sessions
          : {};
        var target = store();
        if (!target) return;
        var next = Object.assign({}, target.bySession || {});
        ids.forEach(function (id) {
          next[id] = Array.isArray(sessions[id]) ? sessions[id] : [];
          loaded[id] = true;
        });
        target.bySession = next;
      } catch (_) {
        // Contributions are optional chrome; session content remains usable.
      } finally {
        ids.forEach(function (id) { delete inFlight[id]; });
      }
    },

    forSession(sessionId, kind) {
      var target = store();
      var rows = target && target.bySession
        ? (target.bySession[String(sessionId || '')] || [])
        : [];
      if (!kind) return rows;
      return rows.filter(function (row) { return row && row.kind === kind; });
    },

    style(item) {
      var accent = item && String(item.accent || '').trim();
      if (!accent || !window.CSS || !window.CSS.supports('color', accent)) return '';
      return '--session-contribution-accent:' + accent;
    },

    open(item) {
      var href = item && item.href;
      if (!href) return;
      if (typeof window.navigateTo === 'function') window.navigateTo(href);
      else window.location.assign(href);
    },
  };

  window.Autonomy.sessionContributions = api;

  var liveLoadScheduled = false;
  function loadLiveSessions() {
    liveLoadScheduled = false;
    if (!window.Alpine || typeof Alpine.store !== 'function') return;
    var sessions = Alpine.store('sessions') || {};
    api.load(Object.keys(sessions));
  }

  function scheduleLiveLoad() {
    if (liveLoadScheduled) return;
    liveLoadScheduled = true;
    setTimeout(loadLiveSessions, 0);
  }

  document.addEventListener('alpine:init', function () {
    Alpine.store('sessionContributions', {bySession: {}});
    scheduleLiveLoad();
  });

  // Shared session cards also appear outside the Sessions page. Following the
  // global session store makes plugin chrome portable to every such surface;
  // page code only needs explicit loads for non-live/history-only sessions.
  if (typeof window.addEventListener === 'function') {
    window.addEventListener('sessions:store-changed', scheduleLiveLoad);
  }

  if (typeof window.registerHandler === 'function') {
    window.registerHandler('session-contributions', function (payload) {
      var sessionId = payload && payload.session_id;
      if (sessionId) api.load([sessionId], {force: true});
    });
  }
})();
