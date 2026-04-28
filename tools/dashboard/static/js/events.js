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
  var _lastSeq = 0;
  var _serverEpoch = null;   // int timestamp from server, set from first event
  var _replaying = false;
  var _heldEvents = [];

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
        Alpine.store('app').sseInterrupted = reason || 'Connection interrupted';
      } catch (e) { /* store not initialised yet */ }
    }
  }

  // Attach a named-event listener for `topic` to the global EventSource.
  // No-op if the EventSource isn't open yet (called again from _connect).
  function _addTopicListener(topic) {
    if (!_es || _topicListening.has(topic)) return;
    _topicListening.add(topic);
    _es.addEventListener(topic, function(e) {
      try {
        var parts = e.lastEventId.split(':');
        var seq = parseInt(parts[0], 10) || 0;
        var epoch = parseInt(parts[1], 10) || 0;
        var data = JSON.parse(e.data);

        // Epoch change detection — server restarted
        if (epoch > 0 && _serverEpoch !== null && epoch !== _serverEpoch) {
          console.warn('[EventBus] server restarted, epoch ' + _serverEpoch + ' → ' + epoch);
          _onInterruption('Server restarted');
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
    _es = new EventSource('/api/events');

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

  window._diagCollectors = window._diagCollectors || {};
  window._diagClientState = function() {
    var es = _es;
    return {
      event_source_ready_state: es ? es.readyState : null,
      replaying: !!_replaying,
      held_events_count: _heldEvents.length,
      client_last_seq: _lastSeq,
      server_epoch: _serverEpoch,
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
    var body = {
      req_id: reqId,
      request_type: requestType,
      client_id: _ensureClientId(),
      payload: Object.assign({ client_state: window._diagClientState() }, collected),
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
    Alpine.store('app', { sseInterrupted: false });
    Alpine.store('pinned', { beads: [] });
  });

  // Defer connection to next microtask so synchronous handler registrations
  // in app.js (loaded immediately after this script) are in place.
  setTimeout(_connect, 0);
})();
