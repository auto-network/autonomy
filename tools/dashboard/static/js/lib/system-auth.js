/** Reference-counted bracket for browser/system authentication UI. */
(function () {
  var depth = 0;

  function publish() {
    if (typeof window === 'undefined' || typeof window.dispatchEvent !== 'function') return;
    try {
      window.dispatchEvent(new CustomEvent('autonomy:system-auth-change', {
        detail: { active: depth > 0, depth: depth, reason: 'webauthn' },
      }));
    } catch (_e) {}
  }

  function begin() {
    depth += 1;
    publish();
    var ended = false;
    return function end() {
      if (ended) return;
      ended = true;
      depth = Math.max(0, depth - 1);
      publish();
    };
  }

  async function run(operation) {
    var end = begin();
    try {
      return await operation();
    } finally {
      end();
    }
  }

  window.Autonomy = window.Autonomy || {};
  window.Autonomy.systemAuth = { begin: begin, run: run, get active() { return depth > 0; }, get depth() { return depth; } };
})();
