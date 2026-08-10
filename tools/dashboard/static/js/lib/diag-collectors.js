/**
 * Diag collectors — per-request_type handlers invoked by events.js when a
 * `diag:request` SSE event arrives. Each entry maps a request_type to a
 * function(params) -> { sessions: {...} | other_keys } that returns the
 * payload body (excluding the `client_state` block, which events.js adds).
 *
 * Forward-compat: unknown request_types are silently ignored on the client,
 * so adding a new collector is a non-breaking change.
 */
(function () {
  if (!window._diagCollectors) window._diagCollectors = {};

  /**
   * `session_markers` — per-session marker dict for one or more sessions.
   * Reads directly from Alpine.store('sessions')[id]; falls back to an empty
   * dict if the store isn't ready (e.g. very early page boot).
   */
  window._diagCollectors['session_markers'] = function (params) {
    var ids = (params && Array.isArray(params.sessions)) ? params.sessions : [];
    if (typeof window._diagSnapshotSessions === 'function') {
      return { sessions: window._diagSnapshotSessions(ids) };
    }
    return { sessions: {} };
  };

  /**
   * `session_store_dump` — a range slice of one session's entry buffer
   * (auto-64nx3): {type, tool_name, timestamp, entry_ref} per entry, so a
   * phone-only rendering report can be byte-diffed against server truth
   * remotely in seconds instead of a screenshot conversation. Read-only,
   * bounded, never the full content payload.
   */
  window._diagCollectors['session_store_dump'] = function (params) {
    var id = params && params.session;
    var from = Math.max(0, (params && params.from) | 0);
    var limit = Math.min(Math.max(1, (params && params.limit) || 200), 500);
    var sessions = (window.Alpine && Alpine.store('sessions')) || {};
    var s = id && sessions[id];
    if (!s || !Array.isArray(s.entries)) {
      return { session: id || null, total: 0, from: from, entries: [] };
    }
    var out = [];
    var slice = s.entries.slice(from, from + limit);
    for (var i = 0; i < slice.length; i++) {
      var e = slice[i] || {};
      out.push({
        type: e.type || '',
        tool_name: e.tool_name || null,
        timestamp: e.timestamp || '',
        content_len: typeof e.content === 'string' ? e.content.length : null,
        entry_ref: e.entry_ref || null,
      });
    }
    return { session: id, total: s.entries.length, from: from, entries: out };
  };
})();
