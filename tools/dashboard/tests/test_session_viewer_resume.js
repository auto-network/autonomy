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
  const requestCalls = [];
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

  const fetchFn = (url, options) => {
    fetchCalls.push(url);
    requestCalls.push({ url, options: options || null });
    if (url === '/api/dao/active_sessions') {
      return Promise.resolve({ json: () => Promise.resolve([]) });
    }
    if (url === '/api/session/resume') {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ tmux_name: 'auto-test', label: 'Enterprise NG' }),
      });
    }
    if (url.startsWith('/api/session/autonomy/auto-test/tail?after_file=f&after=12')) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          entries: [{
            type: 'assistant_text',
            content: 'catch-up entry',
            timestamp: '2026-04-24T01:00:00Z',
            entry_ref: { file: 'f', off: 12, sub: 0 },
          }],
          chain: ['f'],
          cursor: { file: 'f', off: 20 },
          window_spans: [{ file: 'f', from: 12, to: 20 }],
          has_more_forward: false,
          offset: 20,
          is_live: true,
          resolved: true,
          type: 'container',
          role: 'builder',
          activity_state: 'thinking',
        }),
      });
    }
    if (url === '/api/worktrees') {
      return Promise.resolve({ ok: true, json: () => Promise.resolve([]) });
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
    requestCalls,
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

const RANGED_URL = '/api/session/autonomy/auto-test/tail?after_file=f&after=12';

function primeViewer(h) {
  const viewer = h.makeViewer();
  viewer.sessionKey = 'auto-test';
  viewer.project = 'autonomy';
  viewer.sessionId = 'auto-test';
  viewer._tailUrl = '/api/session/autonomy/auto-test/tail';
  viewer.state = 'ready';
  const store = h.window.getSessionStore('auto-test');
  store.offset = 12;
  store.chain = ['f'];
  store.committed = { file: 'f', off: 12 };
  return { viewer, store };
}

describe('session viewer resume catch-up (auto-16g9t wake protocol)', () => {
  it('visibilitychange rebuilds the SSE connection even when readyState claims OPEN (the iOS zombie hole)', async () => {
    const h = makeHarness();
    const { viewer, store } = primeViewer(h);

    viewer._setupResumeRecovery();
    // Zombie: the stream reports OPEN while delivering nothing. The old
    // code gated reconnect on readyState === 2 and hung here forever.
    h.window._es.readyState = 1;
    h.document.visibilityState = 'visible';
    h.emitDocument('visibilitychange');
    await flush();

    assert.equal(h.getReconnectCount(), 1,
      'wake must rebuild the connection unconditionally');
    assert.ok(h.fetchCalls.includes(RANGED_URL),
      'catch-up must be ranged from the committed (file, offset) pair');
    assert.equal(store.offset, 20);
    assert.equal(store.entries.length, 1);
    assert.equal(store.entries[0].content, 'catch-up entry');
    assert.deepEqual({ ...store.committed }, { file: 'f', off: 20 },
      'committed advances via the fetch cursor');
    assert.equal(store._counters.wake_gap, 1);
    assert.equal(store._counters.stream_rebuilds, 1);
    assert.equal(store._counters.conclusion_contradicted, 0);
    viewer.destroy();
  });

  it('heartbeat stall triggers the same unconditional rebuild + ranged catch-up', async () => {
    const h = makeHarness();
    const { viewer, store } = primeViewer(h);

    h.window._es.readyState = 1;
    viewer._resumeHeartbeatAt = 1000;
    viewer._checkResumeHeartbeat(17050);
    await flush();

    assert.equal(h.getReconnectCount(), 1);
    assert.ok(h.fetchCalls.includes(RANGED_URL));
    assert.equal(store.offset, 20);
    assert.equal(store.entries.length, 1);
    assert.equal(store._counters.wakeups_by_trigger.heartbeat, 1);
    viewer.destroy();
  });

  it('deduplicates near-simultaneous resume triggers into one catch-up fetch and one reconnect', async () => {
    const h = makeHarness();
    const { viewer, store } = primeViewer(h);

    viewer._setupResumeRecovery();
    h.window._es.readyState = 2;
    h.document.visibilityState = 'visible';
    h.emitDocument('visibilitychange');
    h.emitWindow('focus');
    await flush();

    const tailCalls = h.fetchCalls.filter((url) => url === RANGED_URL);
    assert.equal(tailCalls.length, 1);
    assert.equal(h.getReconnectCount(), 1);
    assert.equal(store._counters.stream_rebuilds_dead, 1,
      'a provably-dead connection is counted as such');
    assert.equal(store.offset, 20);
    assert.equal(store.entries.length, 1);
    viewer.destroy();
  });
});

describe('session viewer cross-org resume', () => {
  it('sends only the stable source id and leaves owner lookup to the server', async () => {
    const h = makeHarness();
    const { viewer } = primeViewer(h);
    viewer._setupWatchers = function() {};
    viewer._armResumeReadyWatch = function() {};
    viewer._resumeMeta = {
      resumable: true,
      sourceId: 'f919a3fe-39a9-4eb1-b9e7-3154a4dac34c',
      sessionUuid: '76b3e374-e2b9-4304-bbce-878580d68351',
      filePath: '/host/enterprise-ng/session.jsonl',
    };

    await viewer.resumeFromViewer();

    const call = h.requestCalls.find((entry) => entry.url === '/api/session/resume');
    assert.ok(call, 'resume request was not sent');
    assert.equal(call.options.headers['Content-Type'], 'application/json');
    assert.deepEqual(JSON.parse(call.options.body), {
      source_id: 'f919a3fe-39a9-4eb1-b9e7-3154a4dac34c',
    });
  });
});
