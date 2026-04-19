/**
 * Session backing store — Alpine.store('sessions') for reactive shared state.
 *
 * Fed by three paths:
 *   1. fetchWithProgress (first visit)
 *   2. SSE session:messages push (live updates)
 *   3. EventBus gap replay (reconnect recovery)
 *
 * All three paths use seq-checked append — no duplicates, no gaps.
 * Store persists across SPA navigations; lost only on full page reload.
 * Mutations through the Alpine proxy trigger automatic re-renders.
 *
 * Two SSE topics:
 *   - session:registry — roster of active sessions (register/deregister only)
 *   - session:messages — entry stream with session_id routing
 *
 * Depends on: events.js (registerHandler, unregisterHandler), Alpine.js
 */
document.addEventListener('alpine:init', function() {
  Alpine.store('sessions', {});

  // Seed all session stores from HTTP on first page load (arch spec v9 §6e).
  // Runs once on SPA boot regardless of which page the user lands on.
  fetch('/api/dao/active_sessions')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (!Array.isArray(data)) return;
      for (var i = 0; i < data.length; i++) {
        var s = data[i];
        var store = window.getSessionStore(s.session_id);
        store.project = s.project || '';
        store.sessionType = s.type || '';
        store.label = s.label || '';
        if (s.role) store.role = s.role;
        store.isLive = s.is_live !== false;
        store.startedAt = s.started_at || 0;
        if (s.last_activity) store.lastActivity = s.last_activity;
        if (s.entry_count) store.entryCount = s.entry_count;
        if (s.context_tokens) store.contextTokens = s.context_tokens;
        if (s.last_message !== undefined) store.lastMessage = s.last_message;
        if (s.topics) store.topics = s.topics;
        if (s.nag_enabled !== undefined) store.nagEnabled = !!s.nag_enabled;
        if (s.nag_interval) store.nagInterval = s.nag_interval;
        if (s.nag_message !== undefined) store.nagMessage = s.nag_message;
        if (s.dispatch_nag_enabled !== undefined) store.dispatchNagEnabled = !!s.dispatch_nag_enabled;
        if (s.resolved !== undefined) store.resolved = !!s.resolved;
      }
    })
    .catch(function(e) { console.warn('[session-store] seed fetch error', e); });
});

window.getSessionStore = function(sessionId) {
  var sessions = Alpine.store('sessions');
  if (!sessions[sessionId]) {
    sessions[sessionId] = {
      entries: [],
      offset: 0,
      seq: 0,
      isLive: true,
      sessionType: '',
      project: '',
      label: '',
      role: '',
      startedAt: 0,
      entryCount: 0,
      contextTokens: 0,
      topics: [],
      nagEnabled: false,
      nagInterval: 15,
      nagMessage: '',
      dispatchNagEnabled: false,
      sizeMB: '0',
      lastActivity: 0,
      lastMessage: '',
      draftText: '',
      resolved: false,
      toolMap: {},       // tool_id -> { tool_name }
      resultMap: {},     // tool_id -> tool_result entry
      activityState: 'idle',       // server-derived: idle | thinking | tool_running | dead
      pendingToolIds: {},          // server-derived: tool_id -> true (set-like object)
      loaded: false,
      _loading: false,   // true during initial fetch — buffers SSE
      _pendingSSE: [],
    };
  }
  return sessions[sessionId];
};

/**
 * Append entries to store with seq-based dedup.
 * Returns number of entries actually added (0 if all duped).
 */
window.appendSessionEntries = function(store, data) {
  // Seq dedup — skip if already seen
  // Detect seq regression (server restart resets seq to 0)
  if (data.seq !== undefined && data.seq <= store.seq) {
    // If seq dropped to less than half the old value, it's a server restart
    if (store.seq > 1 && data.seq * 2 < store.seq) {
      store.seq = data.seq;
    } else {
      return 0;
    }
  }
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
 * Register global SSE handlers for session:messages and session:registry.
 * Idempotent — only registers once. Call from any page that needs session data.
 */
var _messagesRegistered = false;
window.ensureSessionMessages = function() {
  if (_messagesRegistered) return;
  _messagesRegistered = true;

  window.registerHandler('session:messages', function(data) {
    var id = data.session_id;
    if (!id) return;

    // Only process sessions we have stores for
    var sessions = Alpine.store('sessions');
    var store = sessions[id];
    if (!store) return;

    // Buffer during initial fetch
    if (store._loading) {
      store._pendingSSE.push(data);
      return;
    }

    window.appendSessionEntries(store, data);

    // Update metadata
    if (data.context_tokens !== undefined) store.contextTokens = data.context_tokens;
    if (data.size_bytes !== undefined) store.sizeMB = (data.size_bytes / 1048576).toFixed(1);
    store.lastActivity = Date.now() / 1000;

    // Update server-derived activity state
    if (data.activity_state !== undefined) store.activityState = data.activity_state;
    if (data.pending_tool_ids !== undefined) {
      var ptids = {};
      for (var k = 0; k < data.pending_tool_ids.length; k++) {
        ptids[data.pending_tool_ids[k]] = true;
      }
      store.pendingToolIds = ptids;
    }

    if (data.is_live === false) {
      store.isLive = false;
    }
  });

  window.registerHandler('session:registry', function(registrySessions) {
    var activeIds = {};
    for (var i = 0; i < registrySessions.length; i++) {
      var s = registrySessions[i];
      activeIds[s.session_id] = true;
      var store = window.getSessionStore(s.session_id);
      store.project = s.project || '';
      store.sessionType = s.type || '';
      store.label = s.label || '';
      store.role = s.role || '';
      store.entryCount = s.entry_count || 0;
      if (s.context_tokens) store.contextTokens = s.context_tokens;
      if (s.topics) store.topics = s.topics;
      store.nagEnabled = !!s.nag_enabled;
      store.nagInterval = s.nag_interval || 15;
      store.nagMessage = s.nag_message || '';
      store.dispatchNagEnabled = !!s.dispatch_nag_enabled;
      store.isLive = s.is_live;
      store.startedAt = s.started_at || 0;
      if (s.last_activity) store.lastActivity = s.last_activity;
      if (s.last_message !== undefined) store.lastMessage = s.last_message;
      if (s.activity_state !== undefined) store.activityState = s.activity_state;
      store.resolved = !!s.resolved;
    }
    // Mark removed sessions as dead
    var allSessions = Alpine.store('sessions');
    for (var id in allSessions) {
      if (!activeIds[id] && allSessions[id].isLive) {
        allSessions[id].isLive = false;
      }
    }
  });

  // Handle label_update events — update stored session's label field
  window.registerHandler('label_update', function(data) {
    if (!data || !data.session_id) return;
    var sessions = Alpine.store('sessions');
    // Store key is tmux_name, which is the session_id
    if (sessions[data.session_id]) {
      sessions[data.session_id].label = data.label || '';
    }
  });
};

// Register SSE handlers on startup — session store is always alive
setTimeout(ensureSessionMessages, 0);
