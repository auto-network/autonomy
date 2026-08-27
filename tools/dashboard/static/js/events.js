// EventBus SSE client utility.
// Maintains ONE persistent EventSource for the app lifetime — never torn down.
// The server broadcasts ALL topics on every connection; the client filters locally.
// Pages register handlers for topics; the connection is shared across all pages.
//
// Gap detection: tracks global seq from SSE id: field. On reconnect, if seq
// jumps, fetches missed events from /api/events/replay and dispatches in order.
// Epoch detection: SSE id encodes seq:epoch. On epoch change (server restart),
// resets seq counters and shows an interruption banner. Same banner shown for
// unrecoverable replay gaps.
//
// Globals exposed:
//   window._sseCache         — last data received per topic, keyed by topic name
//   window.connectEvents     — legacy compat: (topics, handlers) -> { close() }
//   window.registerHandler   — (topic, fn) — add a per-topic handler
//   window.unregisterHandler — (topic, fn) — remove a per-topic handler

(function () {
  window._sseCache = {};
  var _handlers = {};       // topic -> Set<fn>
  var _topicListening = new Set(); // topics with an ES listener already attached
  var _es = null;             // the single global EventSource
  var _esConnectedAt = 0;     // ms-precision wall-clock when _es opened
  var _lastSeq = 0;
  var _serverEpoch = null;   // int timestamp from server, set from first event
  var _replaying = false;
  var _heldEvents = [];
  // /api/diag depth fields — never read on the hot path.
  var _gapReplaysCount = 0;
  var _lastGapReplayTs = 0;     // ms-precision wall-clock; 0 means never
  var _lastUserInteractionTs = Date.now();
  var _lastSeenTs = Date.now();   // last SSE message of ANY kind (event or heartbeat)
  var _watchdogTimer = null;
  var _restartTicker = null;

  function _dispatch(topic, data) {
    var set = _handlers[topic];
    if (set) {
      set.forEach(function(fn) {
        try { fn(data); } catch (err) {
          console.warn('[EventBus] handler error for topic', topic, err);
        }
      });
    }
  }

  function _onInterruption(reason) {
    // Reset global seq — accept events from the new server
    _lastSeq = 0;

    // Reset all session store seqs — prevent dedup from dropping new events
    if (window.Alpine) {
      try {
        var sessions = Alpine.store('sessions');
        if (sessions) {
          for (var id in sessions) {
            if (sessions.hasOwnProperty(id)) sessions[id].seq = 0;
          }
        }
      } catch (e) { /* store not initialised yet */ }
    }

    // Show banner via Alpine store
    if (window.Alpine) {
      try {
        var app = Alpine.store('app');
        // A structured restart notice is more useful than the generic epoch
        // warning. Keep it on screen through the first event from the new
        // process, which otherwise arrives after the completion event's cache.
        if (!app.restartStatus) app.sseInterrupted = reason || 'Connection interrupted';
      } catch (e) { /* store not initialised yet */ }
    }
  }

  function _restartTick() {
    if (!window.Alpine) return;
    try {
      var app = Alpine.store('app');
      var now = Date.now();
      if (!app.restartStatus) {
        if (_restartTicker) clearInterval(_restartTicker);
        _restartTicker = null;
        return;
      }
      if (app.restartStatus && app.restartStatus.phase === 'countdown' &&
          now >= (app.restartStatus.countdown_ends_at_ms || now)) {
        app.restartStatus.phase = 'restarting';
      }
      app.restartNowMs = now;
    } catch (e) { /* Alpine not initialised yet */ }
  }

  function _showRestart(payload) {
    if (!payload || typeof payload !== 'object' || !window.Alpine) return;
    try {
      var app = Alpine.store('app');
      app.sseInterrupted = false;
      app.restartStatus = payload;
      _restartTick();
      if (!_restartTicker) _restartTicker = setInterval(_restartTick, 250);
      // Completion is kept visible briefly as useful timing evidence, then
      // clears itself without forcing the operator to dismiss a banner.
      if (payload.phase === 'complete' || payload.phase === 'recovered') {
        setTimeout(function() {
          try {
            var current = Alpine.store('app').restartStatus;
            if (current === payload) Alpine.store('app').restartStatus = null;
          } catch (e) { /* page may have navigated */ }
        }, 8000);
      }
    } catch (e) {
      console.warn('[EventBus] restart notice could not be shown', e);
    }
  }

  // Attach a named-event listener for `topic` to the global EventSource.
  // No-op if the EventSource isn't open yet (called again from _connect).
  function _addTopicListener(topic) {
    if (!_es || _topicListening.has(topic)) return;
    _topicListening.add(topic);
    _es.addEventListener(topic, function(e) {
      try {
        _lastSeenTs = Date.now();   // any event proves the stream is alive
        var parts = e.lastEventId.split(':');
        var seq = parseInt(parts[0], 10) || 0;
        var epoch = parseInt(parts[1], 10) || 0;
        var data = JSON.parse(e.data);

        // Epoch change detection — server restarted
        if (epoch > 0 && _serverEpoch !== null && epoch !== _serverEpoch) {
          console.warn('[EventBus] server restarted, epoch ' + _serverEpoch + ' → ' + epoch);
          _onInterruption('Server restarted');
          // An abrupt restart has no graceful countdown. Use the same rich
          // restart notice instead of reviving a second plain-text banner.
          _showRestart({
            phase: 'recovered',
            started_at_ms: Date.now(),
            expected_ms: 30000,
          });
          // The restart (uvicorn hot-reload) also dropped the voice audio WS.
          // Re-establish it the moment the server is confirmed back, rather than
          // letting the voice backoff blindly guess — buffer recovery is automatic.
          try {
            if (window.Autonomy && window.Autonomy.voiceCapture &&
                typeof window.Autonomy.voiceCapture.onServerRecovered === 'function') {
              window.Autonomy.voiceCapture.onServerRecovered();
            }
          } catch (e) { /* voice not present on this page */ }
        }
        if (epoch > 0) _serverEpoch = epoch;

        // During replay, hold all incoming events
        if (_replaying) {
          _heldEvents.push({seq: seq, topic: topic, data: data});
          return;
        }

        // Gap detection: if we've seen events before and this isn't the next one
        if (_lastSeq > 0 && seq > _lastSeq + 1) {
          _replaying = true;
          _heldEvents.push({seq: seq, topic: topic, data: data});
          _replayGap(_lastSeq + 1, seq - 1);
          return;
        }

        if (seq > _lastSeq) _lastSeq = seq;
        window._sseCache[topic] = data;
        _dispatch(topic, data);
      } catch (err) {
        console.warn('[EventBus] parse error for topic', topic, err);
      }
    });
  }

  async function _replayGap(fromSeq, toSeq) {
    _gapReplaysCount++;
    _lastGapReplayTs = Date.now();
    console.info('[EventBus] gap detected: seq ' + fromSeq + '-' + toSeq + ', replaying');
    try {
      var resp = await fetch('/api/events/replay?from=' + fromSeq + '&to=' + toSeq);
      var result = await resp.json();

      if (result.complete) {
        // Dispatch replayed events in order
        for (var i = 0; i < result.events.length; i++) {
          var ev = result.events[i];
          if (ev.seq > _lastSeq) _lastSeq = ev.seq;
          window._sseCache[ev.topic] = ev.data;
          _dispatch(ev.topic, ev.data);
        }
      } else {
        // Buffer doesn't cover the gap — reset seqs and show banner
        console.warn('[EventBus] replay incomplete, events may have been missed');
        _lastSeq = toSeq;
        _onInterruption('Some updates may have been missed');
      }

      // Dispatch held events in order
      _heldEvents.sort(function(a, b) { return a.seq - b.seq; });
      for (var j = 0; j < _heldEvents.length; j++) {
        var held = _heldEvents[j];
        if (held.seq <= _lastSeq) continue;
        _lastSeq = held.seq;
        window._sseCache[held.topic] = held.data;
        _dispatch(held.topic, held.data);
      }
    } catch (err) {
      console.warn('[EventBus] replay fetch failed', err);
      _onInterruption('Some updates may have been missed');
    } finally {
      _heldEvents = [];
      _replaying = false;
    }
  }

  function _connect() {
    // Clear topic tracking so _addTopicListener re-attaches to the new ES.
    // Required for manual reconnect (_es.close() + _connect()) — the old
    // EventSource's listeners don't transfer to the new object.
    _topicListening = new Set();
    // Attach the diag client_id so /api/diag/sessions can correlate this SSE
    // queue back to the tab that POSTed the matching diag reply.
    var cid = '';
    try { cid = _ensureClientId(); } catch (e) { cid = ''; }
    var url = '/api/events';
    if (cid) url += '?client_id=' + encodeURIComponent(cid);
    _es = new EventSource(url);
    _esConnectedAt = Date.now();
    _lastSeenTs = Date.now();
    // The server emits `heartbeat` only when the stream is otherwise idle;
    // receiving one proves liveness and feeds the watchdog during quiet periods.
    _es.addEventListener('heartbeat', function() { _lastSeenTs = Date.now(); });

    // Attach listeners for any topics already registered before _connect ran.
    for (var topic in _handlers) {
      if (_handlers.hasOwnProperty(topic)) {
        _addTopicListener(topic);
      }
    }

    _es.onerror = function(e) {
      // Browser EventSource auto-reconnects; just log.
      console.warn('[EventBus] SSE error, will reconnect', e);
    };
  }

  function reconnectEvents() {
    try {
      if (_es) _es.close();
    } catch (e) {
      console.warn('[EventBus] close before reconnect failed', e);
    }
    _connect();
  }

  // Liveness watchdog. iOS EventSource does NOT fire `error` when a stream silently
  // half-opens (app suspend / network change), so the browser's auto-reconnect
  // never triggers and the tail wedges (observed: a 16h zombie SSE subscriber).
  // We track last-message time — reset by any event AND the server heartbeat — and
  // if nothing arrives for WATCHDOG_MS we force a reconnect ourselves. This is what
  // makes a server restart / dead socket transparent instead of wedging.
  var WATCHDOG_MS = 15000;   // ~3 missed 5s heartbeats
  function _watchdogTick() {
    // A hidden tab legitimately receives nothing (SSE paused); only act when the
    // page is visible — and we also check immediately on becoming visible.
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
    if (Date.now() - _lastSeenTs > WATCHDOG_MS) {
      console.warn('[EventBus] no SSE activity for >' + WATCHDOG_MS + 'ms — forcing reconnect');
      _lastSeenTs = Date.now();   // one reconnect per window; avoid a storm
      reconnectEvents();
    }
  }
  function _startWatchdog() {
    if (_watchdogTimer) return;
    _watchdogTimer = setInterval(_watchdogTick, 5000);
    if (typeof document !== 'undefined' && document.addEventListener) {
      document.addEventListener('visibilitychange', function() {
        if (document.visibilityState === 'visible') _watchdogTick();
      });
    }
  }

  function registerHandler(topic, fn) {
    if (!_handlers[topic]) _handlers[topic] = new Set();
    _handlers[topic].add(fn);
    // Ensure a listener is attached to the EventSource for this topic.
    _addTopicListener(topic);
    // Replay cached data so late-registered handlers get the initial state.
    if (window._sseCache[topic]) {
      try { fn(window._sseCache[topic]); } catch (err) {
        console.warn('[EventBus] handler replay error for topic', topic, err);
      }
    }
  }

  function unregisterHandler(topic, fn) {
    if (_handlers[topic]) _handlers[topic].delete(fn);
  }

  /**
   * Connect to the server's EventBus over SSE.
   * Now a thin wrapper over registerHandler/unregisterHandler.
   * The underlying connection is persistent and shared.
   *
   * @param {string[]} topics   - Topics to subscribe to
   * @param {Object}   handlers - Map of topic -> handler function(data)
   * @returns {{ close: () => void }} - Call .close() to unregister handlers
   */
  function connectEvents(topics, handlers) {
    if (!topics.length) return { close: function() {} };
    var registered = [];
    for (var i = 0; i < topics.length; i++) {
      var topic = topics[i];
      var handler = handlers[topic];
      if (!handler) continue;
      registerHandler(topic, handler);
      registered.push([topic, handler]);
    }
    return {
      close: function() {
        for (var j = 0; j < registered.length; j++) {
          unregisterHandler(registered[j][0], registered[j][1]);
        }
      },
    };
  }

  // ── setting.changed routing ──────────────────────────────
  // Consumers register interest in a specific set_id; the router
  // filters incoming `setting.changed` events and only invokes
  // matching callbacks. Cross-org filtering (if any) is the
  // consumer's responsibility — the event payload carries `org`.
  var _settingListeners = {}; // set_id -> Set<fn>
  var _settingDispatcherInstalled = false;

  function _ensureSettingDispatcher() {
    if (_settingDispatcherInstalled) return;
    _settingDispatcherInstalled = true;
    registerHandler('setting.changed', function(payload) {
      if (!payload || typeof payload !== 'object') return;
      var listeners = _settingListeners[payload.set_id];
      if (!listeners) return;
      listeners.forEach(function(fn) {
        try { fn(payload); } catch (err) {
          console.warn('[EventBus] setting.changed listener error', err);
        }
      });
    });
  }

  // All consumers MUST route through this helper. Direct
  // registerHandler('setting.changed', ...) usage is forbidden — see
  // bead auto-5mz65. Today the bus uses a single `setting.changed`
  // topic with metadata payloads; if we ever migrate to per-set_id
  // topic granularity (e.g. `setting:SET_ID`), this helper is the one
  // place that needs to change.
  function onSettingChanged(setId, callback) {
    if (!setId || typeof callback !== 'function') {
      return function() {};
    }
    _ensureSettingDispatcher();
    if (!_settingListeners[setId]) _settingListeners[setId] = new Set();
    _settingListeners[setId].add(callback);
    return function unsubscribe() {
      var s = _settingListeners[setId];
      if (s) {
        s.delete(callback);
        if (s.size === 0) delete _settingListeners[setId];
      }
    };
  }

  // ── diag:request routing ──────────────────────────────────
  // /api/diag/sessions broadcasts a one-shot diag:request event.
  // Per-tab handlers live in window._diagCollectors keyed by request_type.
  // The page (or session-store.js) registers a collector once; this dispatcher
  // builds the POST body and ships it to /api/diag/client. Unknown
  // request_types are silently ignored for forward-compat.
  function _ensureClientId() {
    try {
      var existing = sessionStorage.getItem('diag_client_id');
      if (existing) return existing;
      var id = (window.crypto && window.crypto.randomUUID)
        ? window.crypto.randomUUID()
        : 'c-' + Math.random().toString(36).slice(2) + '-' + Date.now().toString(36);
      sessionStorage.setItem('diag_client_id', id);
      return id;
    } catch (e) {
      // sessionStorage unavailable — generate a per-tab id without persistence.
      if (!window._diagClientId) {
        window._diagClientId = 'c-' + Math.random().toString(36).slice(2);
      }
      return window._diagClientId;
    }
  }

  // Passive listeners for last-user-interaction. Keep them passive so we
  // never block scroll/keypress; they exist purely to surface idle time
  // in /api/diag responses.
  function _bumpInteraction() { _lastUserInteractionTs = Date.now(); }
  try {
    var opts = { passive: true, capture: true };
    window.addEventListener('keydown', _bumpInteraction, opts);
    window.addEventListener('mousedown', _bumpInteraction, opts);
    window.addEventListener('click', _bumpInteraction, opts);
    window.addEventListener('scroll', _bumpInteraction, opts);
    window.addEventListener('touchstart', _bumpInteraction, opts);
    window.addEventListener('focus', _bumpInteraction, opts);
  } catch (e) { /* non-browser env (jsdom test) — ignore */ }

  function _handlerCount() {
    var n = 0;
    for (var k in _handlers) {
      if (_handlers.hasOwnProperty(k) && _handlers[k]) {
        n += _handlers[k].size || 0;
      }
    }
    return n;
  }

  function _seenIdentitiesSize() {
    try {
      var sessions = (window.Alpine && Alpine.store('sessions')) || {};
      var total = 0;
      for (var id in sessions) {
        if (!sessions.hasOwnProperty(id)) continue;
        var s = sessions[id];
        if (s && s._seenIdentities) {
          total += Object.keys(s._seenIdentities).length;
        }
      }
      return total;
    } catch (e) { return 0; }
  }

  window._diagCollectors = window._diagCollectors || {};
  window._diagClientState = function(emitTs) {
    var es = _es;
    var nowMs = Date.now();
    // emitTs (server-stamped, in ms) → clock_skew_ms = client.now - emit_ts.
    // A positive value means the client clock is ahead of the server.
    var clockSkewMs = null;
    if (typeof emitTs === 'number' && isFinite(emitTs) && emitTs > 0) {
      clockSkewMs = nowMs - emitTs;
    }
    var connType = null;
    try {
      var conn = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
      if (conn && typeof conn.effectiveType === 'string') connType = conn.effectiveType;
    } catch (e) { /* ignore */ }
    var screenW = null, screenH = null;
    try {
      if (window.screen) { screenW = window.screen.width; screenH = window.screen.height; }
    } catch (e) { /* ignore */ }
    var tz = null;
    try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || null; } catch (e) { /* ignore */ }
    return {
      // Existing fields — unchanged shape.
      event_source_ready_state: es ? es.readyState : null,
      replaying: !!_replaying,
      held_events_count: _heldEvents.length,
      client_last_seq: _lastSeq,
      server_epoch: _serverEpoch,
      // New depth fields — see auto-wldnv.
      page: (window.location && (window.location.pathname + window.location.search)) || '',
      referrer: (typeof document !== 'undefined' && document.referrer) || '',
      visibility_state: (typeof document !== 'undefined' && document.visibilityState) || null,
      online: (typeof navigator !== 'undefined') ? !!navigator.onLine : null,
      viewport: {
        w: (typeof window !== 'undefined') ? window.innerWidth : null,
        h: (typeof window !== 'undefined') ? window.innerHeight : null,
      },
      screen: { w: screenW, h: screenH },
      time_zone: tz,
      clock_skew_ms: clockSkewMs,
      last_user_interaction_ms: Math.max(0, nowMs - _lastUserInteractionTs),
      network_type: connType,
      events_handler_count: _handlerCount(),
      seen_identities_size: _seenIdentitiesSize(),
      gap_replays_count: _gapReplaysCount,
      last_gap_replay_ts: _lastGapReplayTs,
      es_connected_age_ms: _esConnectedAt > 0 ? Math.max(0, nowMs - _esConnectedAt) : null,
    };
  };

  registerHandler('diag:request', function(payload) {
    if (!payload || typeof payload !== 'object') return;
    var reqId = payload.req_id;
    var requestType = payload.request_type;
    if (!reqId || !requestType) return;
    var collector = window._diagCollectors[requestType];
    // Forward-compat: unknown request_types are silently ignored.
    if (typeof collector !== 'function') return;
    var collected;
    try {
      collected = collector(payload.params || {}) || {};
    } catch (err) {
      console.warn('[EventBus] diag collector error', requestType, err);
      collected = {};
    }
    // Server stamps emit_ts (seconds) and emit_ts_ms; pass ms-precision
    // form so _diagClientState can compute clock_skew_ms cleanly.
    var emitTsMs = (typeof payload.emit_ts_ms === 'number')
      ? payload.emit_ts_ms
      : (typeof payload.emit_ts === 'number' ? payload.emit_ts * 1000 : null);
    var body = {
      req_id: reqId,
      request_type: requestType,
      client_id: _ensureClientId(),
      payload: Object.assign({ client_state: window._diagClientState(emitTsMs) }, collected),
    };
    try {
      fetch('/api/diag/client', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        keepalive: true,
      }).catch(function(err) {
        console.warn('[EventBus] diag POST failed', err);
      });
    } catch (err) {
      console.warn('[EventBus] diag POST threw', err);
    }
  });

  // Expose API first, connect after — so app.js can register handlers
  // before the initial SSE event arrives.
  window.connectEvents = connectEvents;
  window.registerHandler = registerHandler;
  window.unregisterHandler = unregisterHandler;
  window.reconnectEvents = reconnectEvents;
  window._connect = _connect;
  // /api/diag accessors — read-only views into the gap-replay tracker.
  window._diagGapReplaysCount = function() { return _gapReplaysCount; };
  window._diagLastGapReplayTs = function() { return _lastGapReplayTs; };
  window.dashboardEvents = window.dashboardEvents || {};
  window.dashboardEvents.onSettingChanged = onSettingChanged;
  Object.defineProperty(window, '_lastSeq', {
    get: function() { return _lastSeq; },
    set: function(v) { _lastSeq = v; },
  });
  Object.defineProperty(window, '_es', {
    get: function() { return _es; },
  });

  // Register Alpine stores
  document.addEventListener('alpine:init', function() {
    Alpine.store('app', {
      sseInterrupted: false,
      restartStatus: null,
      restartNowMs: Date.now(),
      restartMessage: function() {
        var status = this.restartStatus;
        if (!status) return '';
        var now = this.restartNowMs || Date.now();
        if (status.phase === 'countdown') {
          var left = Math.max(0, Math.ceil(((status.countdown_ends_at_ms || now) - now) / 1000));
          return 'Server restarting in ' + left + '…';
        }
        if (status.phase === 'complete') {
          return 'Restart complete in ' + ((status.duration_ms || 0) / 1000).toFixed(1) + 's';
        }
        if (status.phase === 'recovered') return 'Server just restarted';
        var elapsed = Math.max(0, now - (status.started_at_ms || now));
        return 'Server is restarting · ' + Math.floor(elapsed / 1000) + 's elapsed';
      },
      restartProgress: function() {
        var status = this.restartStatus;
        if (!status) return 0;
        if (status.phase === 'countdown') return 0;
        if (status.phase === 'complete' || status.phase === 'recovered') return 100;
        var elapsed = Math.max(0, (this.restartNowMs || Date.now()) - (status.started_at_ms || Date.now()));
        return Math.min(100, Math.round(elapsed * 100 / (status.expected_ms || 30000)));
      },
    });
    Alpine.store('pinned', { beads: [] });
  });

  // This is registered by the shared EventSource client, not a page. It is
  // therefore listening before a route's own scripts load and on every screen.
  registerHandler('server:restart', _showRestart);

  // Defer connection to next microtask so synchronous handler registrations
  // in app.js (loaded immediately after this script) are in place.
  setTimeout(_connect, 0);
  _startWatchdog();
})();
