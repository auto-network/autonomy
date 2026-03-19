// EventBus SSE client utility.
// Maintains ONE persistent EventSource for the app lifetime — never torn down.
// Pages register handlers for topics; the connection is shared across all pages.
//
// Globals exposed:
//   window._sseCache         — last data received per topic, keyed by topic name
//   window.connectEvents     — legacy compat: (topics, handlers) -> { close() }
//   window.registerHandler   — (topic, fn) — add a per-topic handler
//   window.unregisterHandler — (topic, fn) — remove a per-topic handler
//   window.registerTopic     — (topic) — open a dedicated EventSource for a dynamic topic
//   window.unregisterTopic   — (topic) — close and remove the dynamic EventSource

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
    // Replay cached data so late-registered handlers get the initial state
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

  // Dynamic topic registry — one dedicated EventSource per dynamic topic.
  // Use this for topics like "experiments:{id}" that aren't known at page load.
  const _dynamicSources = {}; // topic -> EventSource

  function registerTopic(topic) {
    if (_dynamicSources[topic]) return; // already connected
    const url = '/api/events?topics=' + encodeURIComponent(topic);
    const es = new EventSource(url);
    es.addEventListener(topic, (e) => {
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
    es.onerror = (e) => {
      console.warn('[EventBus] SSE error for dynamic topic', topic, e);
    };
    _dynamicSources[topic] = es;
  }

  function unregisterTopic(topic) {
    const es = _dynamicSources[topic];
    if (es) {
      es.close();
      delete _dynamicSources[topic];
    }
  }

  // Expose API first, connect after — so app.js can register handlers
  // before the initial SSE event arrives.
  window.connectEvents = connectEvents;
  window.registerHandler = registerHandler;
  window.unregisterHandler = unregisterHandler;
  window.registerTopic = registerTopic;
  window.unregisterTopic = unregisterTopic;

  // Defer connection to next microtask so synchronous handler registrations
  // in app.js (loaded immediately after this script) are in place.
  setTimeout(_connect, 0);
})();
