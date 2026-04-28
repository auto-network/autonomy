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
})();
