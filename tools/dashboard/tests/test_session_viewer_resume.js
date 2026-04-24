const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const STORE_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/session-store.js');
const VIEWER_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/pages/session-viewer.js');

function flush() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function makeHarness() {
  const docListeners = {};
  const winListeners = {};
  const components = {};
  const stores = {};
  const fetchCalls = [];
  let reconnectCount = 0;

  const document = {
    visibilityState: 'hidden',
    addEventListener(name, cb) {
      (docListeners[name] ||= []).push(cb);
    },
    removeEventListener(name, cb) {
      const list = docListeners[name] || [];
      const idx = list.indexOf(cb);
      if (idx >= 0) list.splice(idx, 1);
    },
  };

  const windowObj = {
    SessionRenderer: {},
    addEventListener(name, cb) {
      (winListeners[name] ||= []).push(cb);
    },
    removeEventListener(name, cb) {
      const list = winListeners[name] || [];
      const idx = list.indexOf(cb);
      if (idx >= 0) list.splice(idx, 1);
    },
    ensureSessionMessages() {},
    reconnectEvents() { reconnectCount += 1; },
    registerHandler() {},
    unregisterHandler() {},
    _es: { readyState: 2 },
  };

  const alpine = {
    data(name, factory) {
      components[name] = factory;
    },
    store(name, obj) {
      if (obj !== undefined) {
        stores[name] = obj;
        return obj;
      }
      return stores[name];
    },
  };

  const fetchFn = (url) => {
    fetchCalls.push(url);
    if (url === '/api/dao/active_sessions') {
      return Promise.resolve({ json: () => Promise.resolve([]) });
    }
    if (url.startsWith('/api/session/autonomy/auto-test/tail?after=')) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          entries: [{
            type: 'assistant_text',
            content: 'catch-up entry',
            timestamp: '2026-04-24T01:00:00Z',
          }],
          offset: 20,
          is_live: true,
          seq: 5,
          resolved: true,
          type: 'container',
          role: 'builder',
          activity_state: 'thinking',
        }),
      });
    }
    throw new Error('unexpected fetch ' + url);
  };

  const sandbox = {
    window: windowObj,
    document,
    Alpine: alpine,
    fetch: fetchFn,
    console,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    Promise,
    JSON,
    Object,
    Array,
    Map,
    Set,
    Date,
    Error,
    parseInt,
    parseFloat,
  };
  sandbox.window.document = document;
  sandbox.window.Alpine = alpine;
  sandbox.window.fetch = fetchFn;
  vm.createContext(sandbox);

  let storeSrc = fs.readFileSync(STORE_JS, 'utf8');
  storeSrc = storeSrc.replace(
    'setTimeout(ensureSessionMessages, 0);',
    'setTimeout(window.ensureSessionMessages, 0);'
  );
  vm.runInContext(storeSrc, sandbox, { filename: 'session-store.js' });
  vm.runInContext(fs.readFileSync(VIEWER_JS, 'utf8'), sandbox, { filename: 'session-viewer.js' });

  for (const cb of (docListeners['alpine:init'] || [])) cb();

  function makeViewer() {
    const factory = components.sessionViewerPage;
    assert.equal(typeof factory, 'function');
    const viewer = factory({});
    viewer.$watch = function() { return function() {}; };
    viewer.$nextTick = function(fn) { if (typeof fn === 'function') fn(); };
    viewer.$refs = {};
    viewer._rebuildDisplay = function() {};
    viewer._scrollToBottom = function() {};
    return viewer;
  }

  return {
    window: windowObj,
    document,
    stores,
    fetchCalls,
    makeViewer,
    getReconnectCount() { return reconnectCount; },
    emitDocument(name, event) {
      for (const cb of (docListeners[name] || [])) cb(event);
    },
    emitWindow(name, event) {
      for (const cb of (winListeners[name] || [])) cb(event);
    },
  };
}

describe('session viewer resume catch-up', () => {
  it('visibilitychange to visible fetches tail delta from the current offset and only reconnects when EventSource is closed', async () => {
    const h = makeHarness();
    const viewer = h.makeViewer();
    viewer.sessionKey = 'auto-test';
    viewer.project = 'autonomy';
    viewer.sessionId = 'auto-test';
    viewer._tailUrl = '/api/session/autonomy/auto-test/tail';
    viewer.state = 'ready';

    const store = h.window.getSessionStore('auto-test');
    store.offset = 12;

    viewer._setupResumeRecovery();
    h.window._es.readyState = 2;
    h.document.visibilityState = 'visible';
    h.emitDocument('visibilitychange');
    await flush();

    assert.equal(h.getReconnectCount(), 1);
    assert.ok(h.fetchCalls.includes('/api/session/autonomy/auto-test/tail?after=12'));
    assert.equal(store.offset, 20);
    assert.equal(store.entries.length, 1);
    assert.equal(store.entries[0].content, 'catch-up entry');
    viewer.destroy();
  });

  it('heartbeat stall triggers catch-up without reconnect churn when EventSource is still open', async () => {
    const h = makeHarness();
    const viewer = h.makeViewer();
    viewer.sessionKey = 'auto-test';
    viewer.project = 'autonomy';
    viewer.sessionId = 'auto-test';
    viewer._tailUrl = '/api/session/autonomy/auto-test/tail';
    viewer.state = 'ready';

    const store = h.window.getSessionStore('auto-test');
    store.offset = 12;

    h.window._es.readyState = 1;
    viewer._resumeHeartbeatAt = 1000;
    viewer._checkResumeHeartbeat(17050);
    await flush();

    assert.equal(h.getReconnectCount(), 0);
    assert.ok(h.fetchCalls.includes('/api/session/autonomy/auto-test/tail?after=12'));
    assert.equal(store.offset, 20);
    assert.equal(store.entries.length, 1);
    viewer.destroy();
  });

  it('deduplicates near-simultaneous resume triggers into one catch-up fetch and one reconnect', async () => {
    const h = makeHarness();
    const viewer = h.makeViewer();
    viewer.sessionKey = 'auto-test';
    viewer.project = 'autonomy';
    viewer.sessionId = 'auto-test';
    viewer._tailUrl = '/api/session/autonomy/auto-test/tail';
    viewer.state = 'ready';

    const store = h.window.getSessionStore('auto-test');
    store.offset = 12;

    viewer._setupResumeRecovery();
    h.window._es.readyState = 2;
    h.document.visibilityState = 'visible';
    h.emitDocument('visibilitychange');
    h.emitWindow('focus');
    await flush();

    const tailCalls = h.fetchCalls.filter((url) => (
      url === '/api/session/autonomy/auto-test/tail?after=12'
    ));
    assert.equal(tailCalls.length, 1);
    assert.equal(h.getReconnectCount(), 1);
    assert.equal(store.offset, 20);
    assert.equal(store.entries.length, 1);
    viewer.destroy();
  });
});
