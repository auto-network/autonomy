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
/**
 * Apply one committed turn-correction row to a session store's single
 * correction map (auto-hmow2). This is the ONLY writer of correction state:
 * the SSE handler, GET hydration, and optimistic accept/dismiss all funnel
 * through it, so the browser keeps exactly one reactive map per store.
 *
 * The map is replaced immutably (never mutated in place) so Alpine observes
 * the change and re-renders the joined user tile. Applying the same committed
 * row again is a no-op-shaped write: the map ends up value-equal.
 *
 * Ordering guard (delivery is at-least-once and NOT ordered across the SSE and
 * GET-hydration paths): an incoming row whose ``updated_at`` is older than the
 * row already held is DROPPED. This keeps a slow GET-hydration snapshot (read
 * while the row was still pending) from regressing a tile that a newer
 * accept/dismiss SSE event already moved to terminal. Equal ``updated_at`` is
 * applied, so the optimistic accept/dismiss → rollback path (which reuses the
 * original row's timestamp) still works. The server is authoritative and only
 * ever advances ``updated_at``, so dropping strictly-older rows never discards
 * real forward progress. Returns true when a row was applied.
 *
 * Exposed on window so the behavioral sweep exercises the exact function
 * registered to SSE rather than a test-only reimplementation.
 */
window.applyTurnCorrection = function(store, correction) {
  if (!store) return false;
  if (!correction || !correction.target_message_id) return false;
  var current = store._turnCorrections || {};
  var existing = current[correction.target_message_id];
  if (existing && Number(correction.updated_at || 0) < Number(existing.updated_at || 0)) {
    return false;  // stale/out-of-order delivery — keep the newer row
  }
  var next = {};
  for (var k in current) {
    if (Object.prototype.hasOwnProperty.call(current, k)) next[k] = current[k];
  }
  next[correction.target_message_id] = correction;
  store._turnCorrections = next;
  return true;
};

/** Collapse provider model ids into the tight label used on session cards. */
window.compactSessionModel = function(model) {
  var raw = String(model || '').trim();
  if (!raw) return '';

  // Capacity/context suffixes remain in the tooltip but not card chrome.
  var value = raw.replace(/\[[^\]]+\]$/g, '');
  var codex = value.match(/^gpt-([0-9]+(?:\.[0-9]+)?)(?:-([a-z0-9.-]+))?$/i);
  if (codex) {
    var codexFamily = (codex[2] || '').split('-').filter(Boolean).map(function(part) {
      return part.charAt(0).toUpperCase() + part.slice(1);
    }).join('-');
    return codex[1] + (codexFamily ? '-' + codexFamily : '');
  }

  var claude = value.match(/^claude-(opus|sonnet|haiku|fable)-([0-9]+)(?:-([0-9]+))?(?:-[0-9]{8})?$/i);
  if (claude) {
    return claude[1].charAt(0).toUpperCase() + claude[1].slice(1).toLowerCase()
      + '-' + claude[2] + (claude[3] ? '.' + claude[3] : '');
  }
  var legacyClaude = value.match(/^claude-([0-9]+)-([0-9]+)-(opus|sonnet|haiku)(?:-[0-9]{8})?$/i);
  if (legacyClaude) {
    return legacyClaude[3].charAt(0).toUpperCase() + legacyClaude[3].slice(1).toLowerCase()
      + '-' + legacyClaude[1] + '.' + legacyClaude[2];
  }

  value = value.replace(/^claude-/i, '').replace(/^gpt-/i, '');
  return value.split('-').filter(Boolean).map(function(part) {
    return /^[a-z]/i.test(part)
      ? part.charAt(0).toUpperCase() + part.slice(1)
      : part;
  }).join('-');
};

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
        if (s.last_input_at) store.lastInputAt = s.last_input_at;
        if (s.entry_count) store.entryCount = s.entry_count;
        if (s.context_tokens) store.contextTokens = s.context_tokens;
        // An EMPTY last_message never replaces a real one. A completed Codex
        // turn ends with codex_task_complete (internal, content null), so a
        // blanket `!== undefined` blanked the card preview every time a turn
        // finished. Matches the truthiness guards on last_activity /
        // entry_count / context_tokens directly above.
        if (s.last_message) store.lastMessage = s.last_message;
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
        if (s.startup_state !== undefined) store.startupState = s.startup_state;
        if (s.state || s.lifecycle_state) store.state = s.state || s.lifecycle_state;
        if (s.attention !== undefined) store.attention = s.attention;
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

// ── Durable outbox send engine ────────────────────────────────────────────
//
// This lives at the session-store layer, not in session-viewer.js, because
// voice can send to a bound session whose viewer is not mounted. Staging,
// persistence, POST, timeout, restore, and reconciliation all need one owner.
var _OUTBOX_CONFIRM_MS = 9000;
var _outboxTimers = {};
var _outboxInFlight = {};

function _outboxStore(sessionId) {
  if (!sessionId || typeof window.getSessionStore !== 'function') return null;
  return window.getSessionStore(sessionId);
}

function _outboxText(outbox) {
  return (outbox && typeof outbox.text === 'string') ? outbox.text.trim() : '';
}

function _clearOutboxTimer(sessionId) {
  if (_outboxTimers[sessionId]) {
    clearTimeout(_outboxTimers[sessionId]);
    delete _outboxTimers[sessionId];
  }
}

window.markOutboxUnconfirmed = function(sessionId, localId) {
  var s = _outboxStore(sessionId);
  if (s && s.outbox && s.outbox.localId === localId && s.outbox.state === 'sending') {
    s.outbox = Object.assign({}, s.outbox, { state: 'unconfirmed' });
    window.saveOutbox(sessionId, s.outbox);
  }
};

window.armOutboxTimeout = function(sessionId, localId) {
  _clearOutboxTimer(sessionId);
  _outboxTimers[sessionId] = setTimeout(function() {
    window.markOutboxUnconfirmed(sessionId, localId);
  }, _OUTBOX_CONFIRM_MS);
};

window.tryReconcileOutbox = function(sessionId) {
  var s = _outboxStore(sessionId);
  var o = s && s.outbox;
  if (!o || (o.state !== 'sending' && o.state !== 'unconfirmed')) return false;
  var want = _outboxText(o);
  if (!want) return false;
  var entries = (s && s.entries) || [];
  for (var i = entries.length - 1; i >= 0 && i >= entries.length - 8; i--) {
    var e = entries[i];
    if (e && e.type === 'user' && typeof e.content === 'string' &&
        e.content.trim().indexOf(want) !== -1) {
      _clearOutboxTimer(sessionId);
      delete _outboxInFlight[sessionId];
      s.outbox = null;
      window.clearOutbox(sessionId);
      return true;
    }
  }
  return false;
};

window.sendCurrentOutbox = async function(sessionId, options) {
  options = options || {};
  var s = _outboxStore(sessionId);
  var o = s && s.outbox;
  var body = _outboxText(o);
  if (!body || !o.localId || o.state !== 'sending') return false;
  if (!options.force && _outboxInFlight[sessionId] === o.localId) return true;

  _outboxInFlight[sessionId] = o.localId;
  s.outbox = Object.assign({}, o, { delivery: 'posted' });
  window.saveOutbox(sessionId, s.outbox);

  var ok = false;
  try {
    var res = await fetch('/api/session/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: body,
        tmux_session: options.tmuxSession || sessionId,
        client_id: o.localId,
      }),
    });
    var data = await res.json();
    ok = !!(res && res.ok && data && data.ok);
    if (ok) {
      var inputAt = Number(data.last_input_at) || (Number(o.ts) / 1000);
      if (inputAt > (s.lastInputAt || 0)) s.lastInputAt = inputAt;
      _emitSessionStoreChanged('input');
    }
  } catch (e) {
    ok = false;
  }

  if (_outboxInFlight[sessionId] === o.localId) delete _outboxInFlight[sessionId];
  if (!ok) {
    window.markOutboxUnconfirmed(sessionId, o.localId);
    return false;
  }
  window.armOutboxTimeout(sessionId, o.localId);
  return true;
};

window.stageOutboxSend = function(sessionId, outbox, options) {
  var s = _outboxStore(sessionId);
  var body = _outboxText(outbox);
  if (!s || !body) return Promise.resolve(false);
  var next = Object.assign({}, outbox, {
    localId: outbox.localId || (window.newOutboxId ? window.newOutboxId() : ('ob_' + sessionId)),
    state: 'sending',
    text: outbox.text,
    ts: outbox.ts || ((typeof Date !== 'undefined' && Date.now) ? Date.now() : 0),
    delivery: 'queued',
  });
  s.outbox = next;
  window.saveOutbox(sessionId, next);
  return window.sendCurrentOutbox(sessionId, options);
};

window.resendOutbox = function(sessionId, options) {
  var s = _outboxStore(sessionId);
  if (!s || !s.outbox || s.outbox.state !== 'unconfirmed') return Promise.resolve(false);
  s.outbox = Object.assign({}, s.outbox, { state: 'sending', delivery: 'queued' });
  window.saveOutbox(sessionId, s.outbox);
  return window.sendCurrentOutbox(sessionId, Object.assign({}, options || {}, { force: true }));
};

window.dismissOutbox = function(sessionId) {
  var s = _outboxStore(sessionId);
  if (!s) return;
  _clearOutboxTimer(sessionId);
  delete _outboxInFlight[sessionId];
  s.outbox = null;
  window.clearOutbox(sessionId);
};

window.restoreOutbox = function(sessionId) {
  if (!sessionId) return false;
  var saved = window.loadOutbox(sessionId);
  if (!saved) return false;
  if (!_outboxText(saved)) {
    window.clearOutbox(sessionId);
    return false;
  }
  var s = _outboxStore(sessionId);
  if (!s || s.outbox) return false;
  s.outbox = saved;
  if (window.tryReconcileOutbox(sessionId)) return true;
  if (s.outbox && s.outbox.state === 'sending') {
    if (s.outbox.delivery === 'queued') {
      window.sendCurrentOutbox(sessionId);
    } else {
      window.armOutboxTimeout(sessionId, s.outbox.localId);
    }
  }
  return true;
};

// Pending-message state for the session-card / viewer activity dot (auto-xkdoi).
// Returns '' | 'sending' | 'unconfirmed' — the "hasn't cleared yet" states that
// warrant a glanceable indicator. 'capturing' is intentionally excluded (live
// dictation: the user is right there, nothing is at risk). Read-only — never
// creates a store, safe to call per-card in a render loop.
window.outboxPendingState = function(sessionId) {
  if (!sessionId) return '';
  try {
    var sessions = Alpine.store('sessions');
    var s = sessions && sessions[sessionId];
    var o = s && s.outbox;
    if (o && (o.state === 'sending' || o.state === 'unconfirmed')) return o.state;
  } catch (e) { /* store not ready */ }
  return '';
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
      sessionId: sessionId,
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
      // Latest successful direct composer send. Deliberately independent of
      // lastActivity, which advances for assistant output and tool traffic.
      lastInputAt: 0,
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
      // Unified startup FSM. NULL = "not in launching" (default + terminal).
      startupState: null,
      resumable: false,
      harnessState: {},
      // auto-ja51w: transient per-session sub-phase progress; null when none.
      phaseProgress: null,
      // auto-16g9t canonical identity state. chain = ordered rollover
      // file stems (server-authoritative); committed = the span
      // high-water {file, off} advanced ONLY by contiguous spans/cursors
      // — never by entry counts, so dedup can't fake progress;
      // olderCursor = the (file, off) scroll-back pair cursor.
      chain: [],
      committed: null,
      olderCursor: null,
      hasMoreHistory: false,       // whether older-history paging can continue
      // Client-local tiles (uploads) — no entry_ref, display interleaves
      // them by timestamp; never part of the server-truth buffer.
      localEntries: [],
      // Ref-string → entry identity map; the display's paint-safe
      // resolution path (never resolve a tile by array index alone).
      _byRef: {},
      _mergeRev: 0,
      _structureRev: 0,
      _localRefSeq: 0,
      loaded: false,
      _loading: false,   // true during initial fetch — buffers SSE
      _pendingSSE: [],
      // Efficiency counters (auto-16g9t) — cheap increments, published
      // through the /api/diag snapshot; never read on the hot path.
      _counters: {
        merge_inserted: 0,
        merge_merged: 0,
        merge_dropped_duplicate: 0,
        // Lower-fidelity reverse-page duplicates blocked from
        // overwriting richer live entries (the snapshot-vs-reconstruct
        // trade, measurable in production).
        merge_downgrades_blocked: 0,
        span_gaps_detected: 0,
        catchup_stalls: 0,
        // Wake/catch-up protocol counters (commit C).
        wakeups_by_trigger: {},      // trigger reason → count
        wake_happy: 0,               // caught-up wakes (no entries fetched)
        wake_gap: 0,                 // wakes that had to fill a gap
        gap_entries_total: 0,        // entries recovered by catch-ups
        gap_bytes_total: 0,          // raw bytes covered by catch-ups
        on_the_fly_catchups: 0,      // SSE span exposed a hole mid-stream
        catchup_count: 0,
        catchup_latency_ms_total: 0,
        stream_rebuilds: 0,          // SSE connections torn down + reopened
        stream_rebuilds_dead: 0,     // …where the old one was provably dead
        // Viewer believed caught-up, a later fetch proved otherwise.
        // MUST trend to zero — the acceptance gate for truthful catch-up.
        conclusion_contradicted: 0,
      },
      // /api/diag depth fields — never read on the hot path.
      _entriesViaFetchCount: 0,
      _entriesViaSSECount: 0,
      _lastRenderTs: 0,
    };
    setTimeout(function() {
      if (window.restoreOutbox) window.restoreOutbox(sessionId);
    }, 0);
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

// ── The one merge (auto-16g9t) ─────────────────────────────────────────
//
// Every server entry carries entry_ref = {file, off, sub}: the transcript
// file's stem, the raw line's byte offset, and the index among entries
// parsed from that line. store.entries is kept sorted by the tuple
// (chain position of file, off, sub) — store.chain is the server's
// ordered rollover chain. ONE function merges every batch from every
// path (cold-open fetch, scroll-up page, SSE push, wake catch-up):
// present → field-merge, absent → insert in place. This replaced two
// order-assuming paths (append/prepend), an invented
// type+timestamp+content identity, and the _displayDirty flag handshake
// whose clear-before-watcher race duplicated the tail after every
// scroll-up. Harness ids (tool_id) are metadata for the tool maps below,
// never the buffer key.

function _isSubsequence(needle, haystack) {
  var j = 0;
  for (var i = 0; i < haystack.length && j < needle.length; i++) {
    if (haystack[i] === needle[j]) j++;
  }
  return j === needle.length;
}

function _adoptChain(store, chain) {
  // Review S2: if the server's chain is an order-compatible subsequence
  // of ours, we know MORE than the server (it prunes dead predecessors)
  // — adopting would push retained history files to the end and reverse
  // their order. Keep ours.
  if (store.chain.length && _isSubsequence(chain, store.chain)) return;
  var next = chain.slice();
  for (var i = 0; i < store.chain.length; i++) {
    if (next.indexOf(store.chain[i]) === -1) next.push(store.chain[i]);
  }
  var changed = next.length !== store.chain.length;
  if (!changed) {
    for (var j = 0; j < next.length; j++) {
      if (next[j] !== store.chain[j]) { changed = true; break; }
    }
  }
  if (!changed) return;
  store.chain = next;
  // Chain adoption can re-rank files (a scroll-up just revealed an older
  // predecessor). Re-verify sortedness; stable-resort only when broken.
  for (var k = 1; k < store.entries.length; k++) {
    if (_refCompare(store, store.entries[k - 1].entry_ref, store.entries[k].entry_ref) > 0) {
      var indexed = store.entries.map(function(e, n) { return [e, n]; });
      indexed.sort(function(a, b) {
        return _refCompare(store, a[0].entry_ref, b[0].entry_ref) || (a[1] - b[1]);
      });
      store.entries = indexed.map(function(p) { return p[0]; });
      break;
    }
  }
}

function _chainIndex(store, file) {
  var idx = store.chain.indexOf(file);
  if (idx !== -1) return idx;
  // Unknown file — a rollover successor seen before its chain arrived.
  // Place it after everything known; the catch-up fetch this triggers
  // (session-viewer wake/gap path) delivers the authoritative chain.
  store.chain = store.chain.concat([file]);
  return store.chain.length - 1;
}

function _refCompare(store, a, b) {
  var fa = _chainIndex(store, a.file);
  var fb = _chainIndex(store, b.file);
  if (fa !== fb) return fa - fb;
  if (a.off !== b.off) return a.off - b.off;
  return (a.sub || 0) - (b.sub || 0);
}

// Synthetic ref for payloads without one (mock fixtures, stray sources):
// the '~local' pseudo-file sorts after every real chain file and keeps
// arrival order, so refless batches degrade to append semantics.
function _syntheticRef(store, entry) {
  store._localRefSeq = (store._localRefSeq || 0) + 1;
  entry.entry_ref = { file: '~local', off: store._localRefSeq, sub: 0 };
  return entry.entry_ref;
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

// Fidelity ladder for tool_result payload shapes. Reverse/scroll-back
// pages are enriched from a state snapshot rather than full cursor
// reconstruction (the cold-open latency trade the review adjudicated), so
// a window-edge replay can arrive LOWER-fidelity than what the live
// stream delivered — e.g. a wrapper output parsed without its pending
// call comes back as a generic custom_tool_call_output where live had
// exec_command results. A degraded duplicate may NEVER overwrite a
// higher-fidelity entry.
var _RESULT_KIND_FIDELITY = {
  custom_tool_call_output: 0,
  function_call_output: 1,
  exec_command: 2,
  patch_apply_end: 2,
};

// Is the incoming duplicate a lower-fidelity rendering of `existing`?
// Guarded downgrades (each counted via merge_downgrades_blocked):
//   - tool_result.result_kind moving DOWN the fidelity ladder
//   - a terminal tool_result regressing to status 'running'
//   - a semantic tool_use (Read/Grep/Patch upgrade) reverting to the raw
//     exec_command/Bash rendering
function _isDowngrade(existing, incoming) {
  if (existing.type === 'tool_result' && incoming.type === 'tool_result') {
    if (existing.status && existing.status !== 'running' &&
        incoming.status === 'running') {
      return true;
    }
    var ek = _RESULT_KIND_FIDELITY[existing.result_kind];
    var ik = _RESULT_KIND_FIDELITY[incoming.result_kind];
    if (ek !== undefined && ik !== undefined && ik < ek) return true;
  }
  if (existing.type === 'tool_use' && incoming.type === 'tool_use' &&
      existing.semantic_from_exec && !incoming.semantic_from_exec &&
      (incoming.tool_name === 'exec_command' || incoming.tool_name === 'Bash')) {
    return true;
  }
  return false;
}

// Field-merge an incoming duplicate into the entry already at its ref.
// Returns {changed, structural} — structural means the display shape
// (grouping) may have changed, not just a field the tile re-reads.
function _mergeEntryFields(store, existing, incoming) {
  if (_isDowngrade(existing, incoming)) {
    if (store._counters) {
      store._counters.merge_downgrades_blocked =
        (store._counters.merge_downgrades_blocked || 0) + 1;
    }
    return { changed: false, structural: false };
  }
  var structural = false;
  var changed = false;
  if (existing.type === 'tool_use' && incoming.type === 'tool_use' &&
      (existing.tool_name || '') !== (incoming.tool_name || '')) {
    structural = true;
  }
  for (var key in incoming) {
    if (!Object.prototype.hasOwnProperty.call(incoming, key)) continue;
    if (key === 'entry_ref') continue;
    if (
      incoming.semantic_from_exec &&
      key === 'timestamp' &&
      existing.type === 'tool_use' &&
      existing.timestamp
    ) {
      continue;
    }
    if (existing[key] !== incoming[key]) {
      existing[key] = incoming[key];
      changed = true;
    }
  }
  if (existing.type === 'tool_use' && existing.tool_id) _registerToolUse(store, existing);
  if (existing.type === 'tool_result' && existing.tool_id) _registerToolResult(store, existing);
  return { changed: changed, structural: structural };
}

// Lowest index whose entry ref is >= ref (binary search over the sorted buffer).
function _refInsertIndex(store, ref) {
  var lo = 0;
  var hi = store.entries.length;
  while (lo < hi) {
    var mid = (lo + hi) >> 1;
    if (_refCompare(store, store.entries[mid].entry_ref, ref) < 0) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

function _noteEntriesAdded(store, added, provenance, reason) {
  if (added <= 0) return;
  if (provenance === 'fetch') {
    store._entriesViaFetchCount = (store._entriesViaFetchCount || 0) + added;
  } else {
    store._entriesViaSSECount = (store._entriesViaSSECount || 0) + added;
  }
  store._lastRenderTs = Date.now();
  if (window.tryReconcileOutbox) window.tryReconcileOutbox(store.sessionId || '');
  _emitSessionStoreChanged(reason || (provenance === 'fetch' ? 'fetch' : 'message'));
}

/**
 * THE merge: fold one server batch into the store, keyed by entry_ref.
 *
 * Works identically for every source — order of batches never matters,
 * because each entry lands at its tuple position. Display coordination is
 * race-free by monotonic revisions (never a clearable flag):
 *   store._mergeRev      — bumped on any visible change
 *   store._structureRev  — set to _mergeRev when the change was NOT a
 *                          pure tail-append (insert mid-buffer, resort,
 *                          grouping-relevant field change)
 * The viewer's watcher compares revisions it has handled against these;
 * two racing paths can only ever repeat idempotent work, never skip it.
 *
 * Returns the number of entries inserted (0 = all duplicates/merges).
 */
window.mergeSessionEntries = function(store, data, provenance) {
  if (!store) return 0;
  if (data.is_live !== undefined) store.isLive = data.is_live;
  if (Array.isArray(data.chain) && data.chain.length) _adoptChain(store, data.chain);
  var incoming = data.entries;
  if (!incoming || incoming.length === 0) return 0;

  var inserted = 0;
  var merged = 0;
  var droppedDup = 0;
  var structural = false;
  var minInsertRef = null;
  for (var i = 0; i < incoming.length; i++) {
    var entry = incoming[i];
    if (!entry) continue;
    var ref = entry.entry_ref;
    if (!ref || typeof ref.off !== 'number') ref = _syntheticRef(store, entry);
    var at = _refInsertIndex(store, ref);
    var existing = store.entries[at];
    if (existing && _refCompare(store, existing.entry_ref, ref) === 0) {
      var out = _mergeEntryFields(store, existing, entry);
      if (out.changed || out.structural) merged++; else droppedDup++;
      if (out.structural) structural = true;
    } else {
      store.entries.splice(at, 0, entry);
      // Ref → entry identity map: the display layer resolves tiles
      // through this, never by array index, so a descriptor can never
      // paint the wrong entry mid-shift (the transient type-swapped
      // tiles the operator photographed during rapid scroll-back).
      if (!store._byRef) store._byRef = {};
      store._byRef[ref.file + ':' + ref.off + ':' + (ref.sub || 0)] = entry;
      if (entry.type === 'tool_use' && entry.tool_id) _registerToolUse(store, entry);
      if (entry.type === 'tool_result' && entry.tool_id) _registerToolResult(store, entry);
      inserted++;
      if (at !== store.entries.length - 1) structural = true;
      if (minInsertRef === null || _refCompare(store, ref, minInsertRef) < 0) {
        minInsertRef = ref;
      }
    }
  }

  var c = store._counters;
  if (c) {
    c.merge_inserted += inserted;
    c.merge_merged += merged;
    c.merge_dropped_duplicate += droppedDup;
  }
  if (inserted > 0 || merged > 0) {
    store._mergeRev = (store._mergeRev || 0) + 1;
    if (structural) store._structureRev = store._mergeRev;
  }
  store._lastMinInsertRef = minInsertRef;
  _noteEntriesAdded(store, inserted, provenance);
  return inserted;
};

/**
 * Advance the committed span high-water from one batch's raw byte span.
 *
 * committed = {file, off} means "every byte of this file up to off (and
 * every predecessor file entirely) has been applied to the buffer". It
 * advances ONLY on contiguity — a span that starts past it is a GAP
 * (something was dropped between server and us), a span in a different
 * file is a rollover-or-gap; both are returned to the caller (the
 * viewer's catch-up path fetches the range and advances via the fetch
 * cursor instead). Never advanced past dropped data.
 *
 * Returns 'init' | 'advanced' | 'stale' | 'gap' | 'file_switch'.
 */
window.advanceCommittedSpan = function(store, span) {
  if (!store || !span || typeof span.to !== 'number') return 'stale';
  if (!store.committed) {
    store.committed = { file: span.file, off: span.to };
    return 'init';
  }
  if (span.file === store.committed.file) {
    if (span.from > store.committed.off) {
      if (store._counters) store._counters.span_gaps_detected += 1;
      return 'gap';
    }
    if (span.to > store.committed.off) {
      store.committed = { file: span.file, off: span.to };
      return 'advanced';
    }
    return 'stale';
  }
  return 'file_switch';
};

// Client-local tiles (upload attachments) never enter the server-truth
// buffer — they carry no entry_ref, so they live in store.localEntries
// and the display layer interleaves them by timestamp at build time.
window.flushPendingSessionAttachments = function(store, provenance) {
  if (!store || !store._pendingAttachments || store._pendingAttachments.length === 0) return 0;
  var pending = store._pendingAttachments;
  store._pendingAttachments = [];
  var added = 0;
  for (var i = 0; i < pending.length; i++) {
    var p = pending[i];
    var dup = false;
    for (var j = 0; j < store.localEntries.length; j++) {
      var l = store.localEntries[j];
      if (l.rel_path === p.rel_path && l.filename === p.filename &&
          l.timestamp === p.timestamp) { dup = true; break; }
    }
    if (dup) continue;
    store.localEntries.push(p);
    added++;
  }
  if (added > 0) {
    store.localEntries.sort(function(a, b) {
      return (a.timestamp || '').localeCompare(b.timestamp || '');
    });
    store._mergeRev = (store._mergeRev || 0) + 1;
    store._structureRev = store._mergeRev;
    _noteEntriesAdded(store, added, provenance);
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

  // THESE HANDLERS NEED THE STORE, SO THEY WAIT FOR IT. This file is a plain
  // script and Alpine is deferred, so anything registering before Alpine has
  // run reaches getSessionStore -> Alpine.store('sessions') with Alpine
  // undefined. Registering also REPLAYS the cached event for the topic
  // immediately, so an early registration runs a handler at once, against a
  // store that does not exist yet.
  //
  // Nothing crashed, because every dispatch path catches per handler and
  // warns -- which is precisely why it survived: a caught error prints like
  // an uncaught one and the page still works, so it read as fatal to one
  // reader and as noise to everyone else. What actually happened is that the
  // handler aborted partway through the registry list and whatever it had not
  // applied yet was simply not applied.
  //
  // Waiting costs nothing and loses nothing: registration replays the cached
  // event whenever it happens, so deferring registration defers the replay
  // with it, rather than dropping it.
  if (typeof Alpine === 'undefined') {
    document.addEventListener('alpine:init', function() {
      window.ensureSessionMessages();
    }, {once: true});
    return;
  }
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

    // seq survives only as transport telemetry (diag store_seq) — it
    // gates nothing and the halved-seq "server restart" heuristic is
    // gone with the old append paths.
    if (data.seq !== undefined && data.seq > store.seq) store.seq = data.seq;

    // APPLY BEFORE ACK (review B7): the merge must succeed before the
    // committed high-water may advance — an exception mid-merge with the
    // ack already taken would mark progress past unapplied content, the
    // exact loss invariant B1 broke server-side.
    var merged = false;
    try {
      window.mergeSessionEntries(store, data, 'sse');
      merged = true;
    } catch (e) {
      console.warn('[session-store] merge failed; span not committed', e);
    }
    var spanState = (merged && data.span)
      ? window.advanceCommittedSpan(store, data.span) : null;
    // The "first event exposes the gap" path: a span starting past the
    // committed high-water (or a rollover file switch) triggers the
    // viewer's ranged catch-up immediately — no waiting for a wake. A
    // failed merge takes the same path: the catch-up refetches the range
    // and the tuple merge makes the replay idempotent.
    if (spanState === 'gap' || spanState === 'file_switch' || !merged) {
      var gapHandler = window._sessionGapHandlers && window._sessionGapHandlers[id];
      if (typeof gapHandler === 'function') {
        try {
          gapHandler(merged ? spanState : 'merge_failure', data.span);
        } catch (e2) { /* best-effort */ }
      }
    }

    // Update metadata
    if (data.context_tokens !== undefined) store.contextTokens = data.context_tokens;
    var modelChanged = data.model !== undefined && store.model !== data.model;
    if (data.model !== undefined) store.model = data.model;
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
    // A Codex turn_context can change model without yielding a visible
    // transcript entry. Wake list/card consumers for that metadata-only SSE.
    if (modelChanged) _emitSessionStoreChanged('model');
  });

  window.registerHandler('session:turn_corrections', function(data) {
    var id = data && data.session_id;
    if (!id) return;

    // Unopened viewers have no store yet; they hydrate later through GET.
    var store = Alpine.store('sessions')[id];
    if (!store) return;

    if (!window.applyTurnCorrection(store, data && data.correction)) return;
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
      if (s.last_input_at) store.lastInputAt = s.last_input_at;
      // An EMPTY last_message never replaces a real one. A completed Codex
        // turn ends with codex_task_complete (internal, content null), so a
        // blanket `!== undefined` blanked the card preview every time a turn
        // finished. Matches the truthiness guards on last_activity /
        // entry_count / context_tokens directly above.
        if (s.last_message) store.lastMessage = s.last_message;
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
      // Unified startup FSM. Payload sends ``startup_state`` (string or
      // null). Store explicitly nulls on absence so cleared sessions
      // render correctly.
      store.startupState = s.startup_state || null;
      // The one lifecycle truth + telemetry sidecar (FSM consolidation).
      store.state = s.state || s.lifecycle_state || null;
      store.attention = s.attention || null;
      if (s.resumable !== undefined) store.resumable = !!s.resumable;
      if (s.harness_state !== undefined) store.harnessState = s.harness_state;
      // The resume-bridge flag only needs to survive the gap between the
      // resume POST's 202 and this row's first registry appearance — the
      // row is live now, so the bridge is done.
      store._resuming = false;
      // auto-ja51w: transient sub-phase progress (e.g.
      // {repo_index:2, total:3, current_repo:'enterprise_ng'}). Surfaced
      // by SessionMonitor.update_phase(progress=...) — present only while
      // an active progress is set, omitted from payload otherwise. Store
      // gets explicit null on omit so the field clears cleanly.
      store.phaseProgress = s.phase_progress || null;
    }
    // Mark removed sessions as dead. A session absent from the registry is
    // no longer live — including a resume/create whose launch FAILED (the
    // lifecycle writer sets is_live=0 on failure, which drops the row from
    // the live-only registry payload). Clearing the optimistic bridge
    // flags here is what lets the card leave the Active list instead of
    // lingering as a stale "Ended + Resume" ghost; the Recent list renders
    // its true FAILED state (Setup failed + Retry) from the DAO.
    var allSessions = Alpine.store('sessions');
    for (var id in allSessions) {
      if (!activeIds[id] && allSessions[id].isLive) {
        allSessions[id].isLive = false;
        // The row WAS live and is now gone — the launch/session is over
        // (dead, or failed with is_live=0). Never-live placeholder tiles
        // (pending-* / source-id keys) are deliberately untouched: they
        // bridge the create/resume POST round-trip.
        allSessions[id]._resuming = false;
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
      var ref = (e && e.entry_ref) || null;
      return {
        type: (e && e.type) || '',
        timestamp: (e && e.timestamp) || '',
        identity: ref ? (ref.file + ':' + ref.off + ':' + (ref.sub || 0)) : '',
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
      dedup_collisions: (s._counters && s._counters.merge_dropped_duplicate) || 0,
      last_render_ms: lastRenderMs,
      // auto-16g9t identity/merge/catch-up state + counters.
      chain: Array.isArray(s.chain) ? s.chain : [],
      committed: s.committed || null,
      counters: s._counters || {},
    };
  }
  return out;
};
