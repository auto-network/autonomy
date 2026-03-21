// EventBus SSE client utility.
// Maintains ONE persistent EventSource for the app lifetime — never torn down.
// The server broadcasts ALL topics on every connection; the client filters locally.
// Pages register handlers for topics; the connection is shared across all pages.
//
// Gap detection: tracks global seq from SSE id: field. On reconnect, if seq
// jumps, fetches missed events from /api/events/replay and dispatches in order.
// If the buffer can't cover the gap, fires 'sse-gap-unrecoverable' CustomEvent.
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

  // Attach a named-event listener for `topic` to the global EventSource.
  // No-op if the EventSource isn't open yet (called again from _connect).
  function _addTopicListener(topic) {
    if (!_es || _topicListening.has(topic)) return;
    _topicListening.add(topic);
    _es.addEventListener(topic, function(e) {
      try {
        var seq = parseInt(e.lastEventId, 10) || 0;
        var data = JSON.parse(e.data);

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
        // Buffer doesn't cover the gap — notify consumers to re-fetch
        console.warn('[EventBus] replay incomplete, triggering store re-fetch');
        _lastSeq = toSeq;
        window.dispatchEvent(new CustomEvent('sse-gap-unrecoverable', {
          detail: { fromSeq: fromSeq, toSeq: toSeq }
        }));
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
      window.dispatchEvent(new CustomEvent('sse-gap-unrecoverable', {
        detail: { fromSeq: fromSeq, toSeq: toSeq }
      }));
    } finally {
      _heldEvents = [];
      _replaying = false;
    }
  }

  function _connect() {
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

  // Expose API first, connect after — so app.js can register handlers
  // before the initial SSE event arrives.
  window.connectEvents = connectEvents;
  window.registerHandler = registerHandler;
  window.unregisterHandler = unregisterHandler;

  // Defer connection to next microtask so synchronous handler registrations
  // in app.js (loaded immediately after this script) are in place.
  setTimeout(_connect, 0);
})();
