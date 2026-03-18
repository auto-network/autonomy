// EventBus SSE client utility.
// Maintains ONE persistent EventSource for the app lifetime — never torn down.
// Pages register handlers for topics; the connection is shared across all pages.
//
// Globals exposed:
//   window._sseCache         — last data received per topic, keyed by topic name
//   window.connectEvents     — legacy compat: (topics, handlers) -> { close() }
//   window.registerHandler   — (topic, fn) — add a per-topic handler
//   window.unregisterHandler — (topic, fn) — remove a per-topic handler

(function () {
  window._sseCache = {};
  const _handlers = {};  // topic -> Set<fn>

  // All topics the persistent connection subscribes to.
  // Add new topics here as pages are added.
  const _TOPICS = ['dispatch', 'nav'];

  function _connect() {
    const url = '/api/events?topics=' + _TOPICS.join(',');
    const es = new EventSource(url);

    for (const topic of _TOPICS) {
      es.addEventListener(topic, (e) => {
        try {
          const data = JSON.parse(e.data);
          // Update global cache so pages can read on mount.
          window._sseCache[topic] = data;
          // Dispatch to all registered handlers.
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

    es.onerror = (e) => {
      // Browser EventSource auto-reconnects; just log.
      console.warn('[EventBus] SSE error, will reconnect', e);
    };
  }

  function registerHandler(topic, fn) {
    if (!_handlers[topic]) _handlers[topic] = new Set();
    _handlers[topic].add(fn);
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

  // Start the one persistent connection at module load time.
  _connect();

  window.connectEvents = connectEvents;
  window.registerHandler = registerHandler;
  window.unregisterHandler = unregisterHandler;
})();
