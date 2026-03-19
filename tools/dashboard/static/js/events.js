// EventBus SSE client utility.
// Maintains ONE persistent EventSource for the app lifetime — never torn down.
// The server broadcasts ALL topics on every connection; the client filters locally.
// Pages register handlers for topics; the connection is shared across all pages.
//
// Globals exposed:
//   window._sseCache         — last data received per topic, keyed by topic name
//   window.connectEvents     — legacy compat: (topics, handlers) -> { close() }
//   window.registerHandler   — (topic, fn) — add a per-topic handler
//   window.unregisterHandler — (topic, fn) — remove a per-topic handler

(function () {
  window._sseCache = {};
  const _handlers = {};       // topic -> Set<fn>
  const _topicListening = new Set(); // topics with an ES listener already attached
  let _es = null;             // the single global EventSource

  // Attach a named-event listener for `topic` to the global EventSource.
  // No-op if the EventSource isn't open yet (called again from _connect).
  function _addTopicListener(topic) {
    if (!_es || _topicListening.has(topic)) return;
    _topicListening.add(topic);
    _es.addEventListener(topic, (e) => {
      try {
        const data = JSON.parse(e.data);
        window._sseCache[topic] = data;
        const set = _handlers[topic];
        if (set) {
          set.forEach(fn => {
            try { fn(data); } catch (err) {
              console.warn('[EventBus] handler error for topic', topic, err);
            }
          });
        }
      } catch (err) {
        console.warn('[EventBus] parse error for topic', topic, err);
      }
    });
  }

  function _connect() {
    _es = new EventSource('/api/events');

    // Attach listeners for any topics already registered before _connect ran.
    for (const topic of Object.keys(_handlers)) {
      _addTopicListener(topic);
    }

    _es.onerror = (e) => {
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
    if (!topics.length) return { close: () => {} };
    const registered = [];
    for (const topic of topics) {
      const handler = handlers[topic];
      if (!handler) continue;
      registerHandler(topic, handler);
      registered.push([topic, handler]);
    }
    return {
      close() {
        for (const [topic, handler] of registered) {
          unregisterHandler(topic, handler);
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
