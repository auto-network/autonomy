/**
 * Session backing store — shared cache outside Alpine component lifecycle.
 *
 * Fed by three paths:
 *   1. fetchWithProgress (first visit)
 *   2. SSE session:{id} push (live updates)
 *   3. EventBus gap replay (reconnect recovery)
 *
 * All three paths use seq-checked append — no duplicates, no gaps.
 * Store persists across SPA navigations; lost only on full page reload.
 *
 * Depends on: events.js (registerHandler, unregisterHandler)
 */
(function() {
  window._sessionStore = {};

  window.getSessionStore = function(sessionId) {
    if (!window._sessionStore[sessionId]) {
      window._sessionStore[sessionId] = {
        entries: [],
        offset: 0,
        seq: 0,
        isLive: true,
        sessionType: '',
        tmuxSession: '',
        toolMap: {},       // tool_id -> { tool_name }
        resultMap: {},     // tool_id -> tool_result entry
        loaded: false,
        _sseRegistered: false,
        _loading: false,   // true during initial fetch — buffers SSE
        _pendingSSE: [],
      };
    }
    return window._sessionStore[sessionId];
  };

  /**
   * Append entries to store with seq-based dedup.
   * Returns number of entries actually added (0 if all duped).
   */
  window.appendSessionEntries = function(store, data) {
    // Seq dedup — skip if already seen
    if (data.seq !== undefined && data.seq <= store.seq) return 0;
    if (data.seq !== undefined) store.seq = data.seq;

    if (data.is_live !== undefined) store.isLive = data.is_live;

    if (!data.entries || data.entries.length === 0) return 0;

    // Track tool IDs and results
    for (var i = 0; i < data.entries.length; i++) {
      var entry = data.entries[i];
      if (entry.type === 'tool_use' && entry.tool_id) {
        store.toolMap[entry.tool_id] = { tool_name: entry.tool_name || '?' };
      }
      if (entry.type === 'tool_result' && entry.tool_id) {
        store.resultMap[entry.tool_id] = entry;
      }
    }

    for (var j = 0; j < data.entries.length; j++) store.entries.push(data.entries[j]);
    return data.entries.length;
  };

  /**
   * Register SSE handler for a session (idempotent — only registers once).
   * Handler lives outside component lifecycle — survives SPA navigation.
   * Buffers events during initial fetch (_loading flag).
   */
  window.ensureSessionSSE = function(sessionId) {
    var store = window.getSessionStore(sessionId);
    if (store._sseRegistered || !store.isLive) return;
    store._sseRegistered = true;

    var topic = 'session:' + sessionId;
    var handler = function(data) {
      // Buffer during initial fetch to avoid race condition
      if (store._loading) {
        store._pendingSSE.push(data);
        return;
      }
      window.appendSessionEntries(store, data);

      // Session died — freeze store, unregister handler
      if (data.is_live === false) {
        window.unregisterHandler(topic, handler);
        store._sseRegistered = false;
      }
    };
    window.registerHandler(topic, handler);
  };
})();
