// EventBus SSE client utility.
// Provides connectEvents(topics, handlers) for subscribing to server-sent events.
// Loaded before app.js so all pages can call connectEvents() as a global.

(function () {
  /**
   * Connect to the server's EventBus over SSE.
   *
   * @param {string[]} topics   - Array of topic names to subscribe to
   * @param {Object}   handlers - Map of topic -> handler function(data)
   * @returns {{ close: () => void }} - Handle; call .close() to disconnect
   *
   * Usage in Alpine init()/destroy():
   *   init()    { this._events = connectEvents(['dispatch', 'nav'], {
   *     dispatch: data => { this.active = data.active; ... },
   *     nav:      data => { this.openBeads = data.open_beads; },
   *   }); }
   *   destroy() { this._events?.close(); }
   */
  function connectEvents(topics, handlers) {
    if (!topics.length) return { close: () => {} };
    const url = '/api/events?topics=' + topics.join(',');
    const es = new EventSource(url);

    for (const topic of topics) {
      const handler = handlers[topic];
      if (!handler) continue;
      es.addEventListener(topic, (e) => {
        try {
          handler(JSON.parse(e.data));
        } catch (err) {
          console.warn('[EventBus] parse error for topic', topic, err);
        }
      });
    }

    es.onerror = (e) => {
      // Browser EventSource auto-reconnects; just log.
      console.warn('[EventBus] SSE error, will reconnect', e);
    };

    return {
      close() { es.close(); },
    };
  }

  window.connectEvents = connectEvents;
})();
