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
        if (Array.isArray(s.todos)) store.todos = s.todos;
        if (s.nag_enabled !== undefined) store.nagEnabled = !!s.nag_enabled;
        if (s.nag_interval) store.nagInterval = s.nag_interval;
        if (s.nag_message !== undefined) store.nagMessage = s.nag_message;
        if (s.dispatch_nag_enabled !== undefined) store.dispatchNagEnabled = !!s.dispatch_nag_enabled;
        if (s.resolved !== undefined) store.resolved = !!s.resolved;
        if (s.org) store.org = s.org;
        // auto-ngis4: surface harness + model into the per-session store
        // so the canonical session-card partial can paint the icon-rail
        // badge alongside role/type/org.
        if (s.harness) store.harness = s.harness;
        if (s.model !== undefined) store.model = s.model;
        if (s.harness_token !== undefined) store.harnessToken = s.harness_token;
        if (s.harness_token_alias !== undefined) store.harnessTokenAlias = s.harness_token_alias;
      }
      _emitSessionStoreChanged('seed');
    })
    .catch(function(e) { console.warn('[session-store] seed fetch error', e); });
});

// ── Draft durability ─────────────────────────────────────────────────────
// The in-memory store survives SPA navigation but is lost on full page
// reload and — critically on mobile — when iOS Safari evicts a backgrounded
// page from memory. That silently destroys a composed-but-unsent draft when
// the user swipes away and comes back. Mirror every draft to localStorage so
// it survives reload, backgrounding, and swipe-away. localStorage is the
// durable backing; the in-memory store stays the hot-path read.
var _DRAFT_PREFIX = 'sessionDraft:';

window.saveDraft = function(sessionId, text) {
  if (!sessionId) return;
  try {
    if (text && text.length) {
      window.localStorage.setItem(_DRAFT_PREFIX + sessionId, text);
    } else {
      window.localStorage.removeItem(_DRAFT_PREFIX + sessionId);
    }
  } catch (e) { /* private mode / quota — in-memory draft still works */ }
};

window.loadDraft = function(sessionId) {
  if (!sessionId) return '';
  try {
    return window.localStorage.getItem(_DRAFT_PREFIX + sessionId) || '';
  } catch (e) { return ''; }
};

window.clearDraft = function(sessionId) {
  if (!sessionId) return;
  try { window.localStorage.removeItem(_DRAFT_PREFIX + sessionId); }
  catch (e) { /* ignore */ }
};

// ── Outbox identity (auto-xkdoi Phase 2 / voice #28 contract) ──────────────
// Stable client-side id for a pending message across its whole lifecycle
// (capturing → sending → confirmed/unconfirmed). It is the dedup key the
// viewer uses to reconcile the optimistic tile against the JSONL log entry.
// The voice path calls this at the START of a capturing tile so the id is
// stable from first word through send — no placeholder swap on send.
var _OUTBOX_PREFIX = 'sessionOutbox:';

// Persist the pending message so it survives a full reload / iOS eviction
// MID-SEND — the whole point of the outbox: a sent-but-unconfirmed message is
// never lost, even if the page dies before the log echoes it back. On the next
// mount the viewer restores it and re-arms reconciliation.
window.saveOutbox = function(sessionId, outbox) {
  if (!sessionId) return;
  try {
    if (outbox) window.localStorage.setItem(_OUTBOX_PREFIX + sessionId, JSON.stringify(outbox));
    else window.localStorage.removeItem(_OUTBOX_PREFIX + sessionId);
  } catch (e) { /* private mode / quota — in-memory outbox still works */ }
};
window.loadOutbox = function(sessionId) {
  if (!sessionId) return null;
  try {
    var raw = window.localStorage.getItem(_OUTBOX_PREFIX + sessionId);
    return raw ? JSON.parse(raw) : null;
  } catch (e) { return null; }
};
window.clearOutbox = function(sessionId) {
  if (!sessionId) return;
  try { window.localStorage.removeItem(_OUTBOX_PREFIX + sessionId); }
  catch (e) { /* ignore */ }
};

var _outboxSeq = 0;
window.newOutboxId = function() {
  _outboxSeq += 1;
  // No Math.random/Date.now dependency required for uniqueness within a tab:
  // a monotonic counter plus the store's load epoch is enough, and stays
  // deterministic for tests. Epoch keeps ids distinct across reloads.
  return 'ob_' + (window._outboxEpoch || (window._outboxEpoch = (window.performance && performance.now ? Math.floor(performance.now()) : 0))) + '_' + _outboxSeq;
};

window.getSessionStore = function(sessionId) {
  var sessions = Alpine.store('sessions');
  if (!sessions[sessionId]) {
    sessions[sessionId] = {
      entries: [],
      offset: 0,
      seq: 0,
      isLive: false,
      sessionType: '',
      project: '',
      label: '',
      role: '',
      startedAt: 0,
      entryCount: 0,
      contextTokens: 0,
      topics: [],
      todos: [],
      nagEnabled: false,
      nagInterval: 15,
      nagMessage: '',
      dispatchNagEnabled: false,
      sizeMB: '0',
      lastActivity: 0,
      lastMessage: '',
      draftText: '',
      // Pending/optimistic outbox message (auto-xkdoi). null when idle; else
      // { localId, state:'capturing'|'sending'|'unconfirmed', source, text, ts }.
      // Declared here so it's a reactive property from store creation — the
      // voice side writes to it and the viewer's pending tile reads it.
      outbox: null,
      resolved: false,
      toolMap: {},       // tool_id -> { tool_name }
      resultMap: {},     // tool_id -> tool_result entry
      activityState: 'idle',       // server-derived: idle | thinking | tool_running | dead
      pendingToolIds: {},          // server-derived: tool_id -> true (set-like object)
      harness: '',                 // auto-ngis4: claude | codex | future
      model: null,                 // auto-ngis4: most-recent assistant-turn model id
      harnessToken: null,          // auto-08n3f: Anthropic org UUID (substrate key on dashboard.claude.credentials)
      harnessTokenAlias: null,     // auto-08n3f: friendly alias from dashboard.claude.credentials.alias (UI display)
      // auto-yfcoc: startup-phase fields, additive. Defaults match the
      // backend's INSERT defaults so the derivation reads sensible
      // values even before the first session:registry broadcast lands.
      setupPhase: 'pending',
      harnessPhase: 'pending',
      resumable: false,
      harnessState: {},
      olderBefore: null,           // reverse-tail cursor for older-history paging
      hasMoreHistory: false,       // whether older-history paging can continue
      loaded: false,
      _loading: false,   // true during initial fetch — buffers SSE
      _pendingSSE: [],
      _displayDirty: false,
      // /api/diag depth fields — never read on the hot path.
      _entriesViaFetchCount: 0,
      _entriesViaSSECount: 0,
      _dedupCollisionsCount: 0,
      _lastRenderTs: 0,
    };
  }
  return sessions[sessionId];
};

function _emitSessionStoreChanged(reason) {
  if (typeof window === 'undefined' || typeof window.dispatchEvent !== 'function' || typeof CustomEvent !== 'function') return;
  window.dispatchEvent(new CustomEvent('sessions:store-changed', {
    detail: { reason: reason || 'update' },
  }));
}

function _emitSessionRegistryChanged() {
  if (typeof window === 'undefined' || typeof window.dispatchEvent !== 'function' || typeof CustomEvent !== 'function') return;
  window.dispatchEvent(new CustomEvent('sessions:registry-changed'));
}

/**
 * Compute a stable identity key for an entry. Used to dedup entries when SSE
 * gap-replay re-delivers payloads whose per-session seq is "behind" store.seq
 * (e.g. iOS native EventSource Last-Event-ID reconnect, or _fetchBacklog
 * re-running on page re-init during app-resume).
 *
 *   tool_use   → "tu:<tool_id>"
 *   tool_result → "tr:<tool_id>"
 *   anything else → "<type>:<timestamp>:<content-slice>"
 */
function _entryIdentity(entry) {
  if (!entry) return null;
  if (entry.type === 'tool_use' && entry.tool_id) return 'tu:' + entry.tool_id;
  if (entry.type === 'tool_result' && entry.tool_id) return 'tr:' + entry.tool_id;
  var t = entry.type || '?';
  var ts = entry.timestamp || '';
  var c = '';
  if (typeof entry.content === 'string') {
    c = entry.content.length > 200 ? entry.content.slice(0, 200) : entry.content;
  } else if (entry.content !== undefined && entry.content !== null) {
    try { c = JSON.stringify(entry.content).slice(0, 200); } catch (_) { c = ''; }
  }
  return t + ':' + ts + ':' + c;
}

function _ensureSeenIdentities(store) {
  if (store._seenIdentities) return;
  store._seenIdentities = {};
  for (var i = 0; i < store.entries.length; i++) {
    var seedKey = _entryIdentity(store.entries[i]);
    if (seedKey) store._seenIdentities[seedKey] = true;
  }
}

function _registerToolUse(store, entry) {
  store.toolMap[entry.tool_id] = {
    tool_name: entry.tool_name || '?',
    entry: entry,
  };
}

function _registerToolResult(store, entry) {
  var existing = store.resultMap[entry.tool_id];
  if (existing && existing.status === 'completed' && entry.status === 'running') {
    return;
  }
  if (!existing || existing.result_kind !== 'exec_command' || entry.result_kind === 'exec_command') {
    store.resultMap[entry.tool_id] = entry;
  }
}

function _findToolUseEntry(store, toolId) {
  var mapped = store.toolMap[toolId];
  if (mapped && mapped.entry) return mapped.entry;
  for (var i = store.entries.length - 1; i >= 0; i--) {
    var entry = store.entries[i];
    if (entry.type === 'tool_use' && entry.tool_id === toolId) return entry;
  }
  return null;
}

function _findToolResultEntry(store, toolId) {
  var mapped = store.resultMap[toolId];
  if (mapped) return mapped;
  for (var i = store.entries.length - 1; i >= 0; i--) {
    var entry = store.entries[i];
    if (entry.type === 'tool_result' && entry.tool_id === toolId) return entry;
  }
  return null;
}

function _mergeExistingEntry(store, existing, incoming) {
  if (!existing || !incoming) return false;
  if (
    existing.type === 'tool_result' &&
    incoming.type === 'tool_result' &&
    existing.status &&
    existing.status !== 'running' &&
    incoming.status === 'running'
  ) {
    return false;
  }
  var displayChanged = false;
  if (existing.type === 'tool_use' && incoming.type === 'tool_use') {
    if ((existing.tool_name || '') !== (incoming.tool_name || '')) displayChanged = true;
  }
  for (var key in incoming) {
    if (!Object.prototype.hasOwnProperty.call(incoming, key)) continue;
    if (
      incoming.semantic_from_exec &&
      key === 'timestamp' &&
      existing.type === 'tool_use' &&
      existing.timestamp
    ) {
      continue;
    }
    existing[key] = incoming[key];
  }
  if (existing.type === 'tool_use' && existing.tool_id) _registerToolUse(store, existing);
  if (existing.type === 'tool_result' && existing.tool_id) _registerToolResult(store, existing);
  if (displayChanged) store._displayDirty = true;
  return displayChanged;
}

function _appendUniqueEntry(store, entry, insertAt) {
  _ensureSeenIdentities(store);
  var key = _entryIdentity(entry);
  if (key && store._seenIdentities[key]) {
    store._dedupCollisionsCount = (store._dedupCollisionsCount || 0) + 1;
    var existing = null;
    if (entry.type === 'tool_use' && entry.tool_id) existing = _findToolUseEntry(store, entry.tool_id);
    if (entry.type === 'tool_result' && entry.tool_id) existing = _findToolResultEntry(store, entry.tool_id);
    if (existing) {
      _mergeExistingEntry(store, existing, entry);
      return false;
    }
    if (entry.type === 'tool_use' && entry.tool_id) _registerToolUse(store, entry);
    if (entry.type === 'tool_result' && entry.tool_id) _registerToolResult(store, entry);
    return false;
  }
  if (key) store._seenIdentities[key] = true;
  if (entry.type === 'tool_use' && entry.tool_id) _registerToolUse(store, entry);
  if (entry.type === 'tool_result' && entry.tool_id) _registerToolResult(store, entry);
  if (insertAt === undefined || insertAt === null || insertAt >= store.entries.length) {
    store.entries.push(entry);
  } else {
    store.entries.splice(insertAt, 0, entry);
  }
  return true;
}

/**
 * Append entries to store with entry-identity dedup.
 *
 * store.seq is still advanced from data.seq, but it does NOT gate appending —
 * gap-replay payloads whose seq is below store.seq must still land if their
 * entries are new. Server-restart detection (seq halved) still resets store.seq.
 *
 * Returns number of entries actually added (0 if all are duplicates).
 *
 * ``provenance`` ('fetch' | 'sse') tracks where the entries came from for
 * /api/diag — defaults to 'sse' since that's the streaming path.
 */
window.appendSessionEntries = function(store, data, provenance) {
  // Advance store.seq, with server-restart detection.
  if (data.seq !== undefined) {
    if (data.seq > store.seq) {
      store.seq = data.seq;
    } else if (store.seq > 1 && data.seq * 2 < store.seq) {
      // Seq regression — significantly lower → server restart.
      store.seq = data.seq;
    }
    // Otherwise data.seq <= store.seq (replay/duplicate): leave store.seq alone
    // and fall through to entry-identity dedup.
  }

  if (data.is_live !== undefined) store.isLive = data.is_live;

  if (!data.entries || data.entries.length === 0) return 0;

  var pa = store._pendingAttachments;
  var added = 0;
  for (var i = 0; i < data.entries.length; i++) {
    var entry = data.entries[i];
    while (pa && pa.length && (pa[0].timestamp || '') <= (entry.timestamp || '')) {
      if (_appendUniqueEntry(store, pa.shift())) added++;
    }
    if (_appendUniqueEntry(store, entry)) added++;
  }
  while (pa && pa.length) {
    if (_appendUniqueEntry(store, pa.shift())) added++;
  }
  if (added > 0) {
    if (provenance === 'fetch') {
      store._entriesViaFetchCount = (store._entriesViaFetchCount || 0) + added;
    } else {
      store._entriesViaSSECount = (store._entriesViaSSECount || 0) + added;
    }
    store._lastRenderTs = Date.now();
    _emitSessionStoreChanged(provenance === 'fetch' ? 'fetch' : 'message');
  }
  return added;
};

window.prependSessionEntries = function(store, data, provenance) {
  if (data.seq !== undefined) {
    if (data.seq > store.seq) {
      store.seq = data.seq;
    } else if (store.seq > 1 && data.seq * 2 < store.seq) {
      store.seq = data.seq;
    }
  }

  if (data.is_live !== undefined) store.isLive = data.is_live;

  if (!data.entries || data.entries.length === 0) return 0;

  var added = 0;
  var insertAt = 0;
  for (var i = 0; i < data.entries.length; i++) {
    if (_appendUniqueEntry(store, data.entries[i], insertAt)) {
      added++;
      insertAt++;
    }
  }
  if (added > 0) {
    if (provenance === 'fetch') {
      store._entriesViaFetchCount = (store._entriesViaFetchCount || 0) + added;
    } else {
      store._entriesViaSSECount = (store._entriesViaSSECount || 0) + added;
    }
    store._lastRenderTs = Date.now();
    _emitSessionStoreChanged(provenance === 'fetch' ? 'fetch-prepend' : 'prepend');
  }
  return added;
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

    window.appendSessionEntries(store, data, 'sse');

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

  window.registerHandler('session:turn_corrections', function(data) {
    var id = data && data.session_id;
    if (!id) return;

    var sessions = Alpine.store('sessions');
    var store = sessions[id];
    if (!store) return;

    var correction = data && data.correction;
    if (!correction || !correction.target_message_id) return;

    store._turnCorrections = store._turnCorrections || {};
    store._turnCorrections[correction.target_message_id] = correction;
    store._lastRenderTs = Date.now();
    _emitSessionStoreChanged('turn_correction');
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
      if (Array.isArray(s.todos)) store.todos = s.todos;
      store.nagEnabled = !!s.nag_enabled;
      store.nagInterval = s.nag_interval || 15;
      store.nagMessage = s.nag_message || '';
      store.dispatchNagEnabled = !!s.dispatch_nag_enabled;
      store.isLive = s.is_live;
      store.startedAt = s.started_at || 0;
      if (s.last_activity) store.lastActivity = s.last_activity;
      if (s.last_message !== undefined) store.lastMessage = s.last_message;
      if (s.activity_state !== undefined) store.activityState = s.activity_state;
      if (s.org) store.org = s.org;
      store.resolved = !!s.resolved;
      // auto-ngis4: SSE registry must plumb harness + model the same way
      // the HTTP seed path does, otherwise newly-registered sessions paint
      // an "unknown" badge until a full page reload.
      if (s.harness) store.harness = s.harness;
      if (s.model !== undefined) store.model = s.model;
      if (s.harness_token !== undefined) store.harnessToken = s.harness_token;
      if (s.harness_token_alias !== undefined) store.harnessTokenAlias = s.harness_token_alias;
      // auto-yfcoc: startup-phase fields. Additive — the lifecycle
      // derivation reads these off the row, never the store, so the
      // partial works against either the raw registry shape or the
      // populated store entry. Defaults applied at store-creation time
      // so a missing field never confuses the derivation.
      if (s.setup_phase !== undefined) store.setupPhase = s.setup_phase;
      if (s.harness_phase !== undefined) store.harnessPhase = s.harness_phase;
      if (s.resumable !== undefined) store.resumable = !!s.resumable;
      if (s.harness_state !== undefined) store.harnessState = s.harness_state;
    }
    // Mark removed sessions as dead
    var allSessions = Alpine.store('sessions');
    for (var id in allSessions) {
      if (!activeIds[id] && allSessions[id].isLive) {
        allSessions[id].isLive = false;
      }
    }
    _emitSessionStoreChanged('registry');
    _emitSessionRegistryChanged();
  });

  // Handle label_update events — update stored session's label field
  window.registerHandler('label_update', function(data) {
    if (!data || !data.session_id) return;
    var sessions = Alpine.store('sessions');
    // Store key is tmux_name, which is the session_id
    if (sessions[data.session_id]) {
      sessions[data.session_id].label = data.label || '';
      _emitSessionStoreChanged('label');
    }
  });
};

// Register SSE handlers on startup — session store is always alive
setTimeout(ensureSessionMessages, 0);

/**
 * Build the per-session marker dict consumed by /api/diag/sessions.
 *
 * Returns { id: { store_seq, tile_count, store_loading, pending_sse_count,
 * is_focused_viewer, last_activity_ms, last_topic_seq,
 * store_first_entry_ts, store_last_entry_ts, out_of_order_count, idle_ms,
 * tail_3 } } for each id we have a store entry for. Ids without a store
 * are omitted so the server-side diff is easier to reason about.
 */
window._diagSnapshotSessions = function(ids) {
  var sessions = (window.Alpine && Alpine.store('sessions')) || {};
  var out = {};
  if (!Array.isArray(ids) || ids.length === 0) return out;
  var nowMs = Date.now();
  var focused = window._diagFocusedViewerId || null;
  for (var i = 0; i < ids.length; i++) {
    var id = ids[i];
    var s = sessions[id];
    if (!s) continue;
    var entries = Array.isArray(s.entries) ? s.entries : [];
    var firstTs = entries.length ? (entries[0] && entries[0].timestamp) || '' : '';
    var lastTs = entries.length
      ? (entries[entries.length - 1] && entries[entries.length - 1].timestamp) || ''
      : '';
    // Out-of-order count: any entry whose timestamp is < its predecessor's.
    // Cheap O(n) scan — only invoked on diag, not on the hot path.
    var ooo = 0;
    var prev = null;
    for (var j = 0; j < entries.length; j++) {
      var t = entries[j] && entries[j].timestamp;
      if (prev && t && t < prev) ooo++;
      if (t) prev = t;
    }
    var tail10Source = entries.slice(-10);
    var tail10 = tail10Source.map(function(e) {
      return {
        type: (e && e.type) || '',
        timestamp: (e && e.timestamp) || '',
        identity: _entryIdentity(e) || '',
      };
    });
    var nullSeq = 0;
    for (var k = 0; k < entries.length; k++) {
      var ek = entries[k];
      if (ek && (ek.seq === null || ek.seq === undefined)) nullSeq++;
    }
    var lastActivitySec = s.lastActivity || 0;
    var lastActivityMs = lastActivitySec ? Math.max(0, nowMs - lastActivitySec * 1000) : null;
    var lastRenderMs = s._lastRenderTs ? Math.max(0, nowMs - s._lastRenderTs) : null;
    var seenSize = (s._seenIdentities && Object.keys(s._seenIdentities).length) || 0;
    out[id] = {
      store_seq: s.seq || 0,
      tile_count: entries.length,
      store_loading: !!s._loading,
      pending_sse_count: Array.isArray(s._pendingSSE) ? s._pendingSSE.length : 0,
      is_focused_viewer: focused === id,
      last_activity_ms: lastActivityMs,
      last_topic_seq: window._lastSeq || 0,
      store_first_entry_ts: firstTs,
      store_last_entry_ts: lastTs,
      out_of_order_count: ooo,
      idle_ms: lastActivityMs,
      tail_10: tail10,
      // tail_3 retained for backwards compatibility with the auto-zh75w shape.
      tail_3: tail10.slice(-3),
      // /api/diag depth fields — never read on the hot path.
      entries_via_fetch_count: s._entriesViaFetchCount || 0,
      entries_via_sse_count: s._entriesViaSSECount || 0,
      entries_with_null_seq_count: nullSeq,
      gap_replays_count: (window._diagGapReplaysCount && window._diagGapReplaysCount()) || 0,
      last_gap_replay_ts: (window._diagLastGapReplayTs && window._diagLastGapReplayTs()) || 0,
      dedup_collisions: s._dedupCollisionsCount || 0,
      last_render_ms: lastRenderMs,
      seen_identities_size: seenSize,
    };
  }
  return out;
};
