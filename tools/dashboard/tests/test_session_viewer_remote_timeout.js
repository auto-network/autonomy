// A remote session's tail that ran out of time: the
// viewer keeps what it shows, says the machine did not answer in time, and
// retries with backoff -- never "unreachable" with 0 entries.
const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const STORE_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/session-store.js');
const VIEWER_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/pages/session-viewer.js');

const PUB = 'b1'.repeat(32);
const ADDRESS = 'auto-9@' + PUB;
const TAIL = '/api/session/p/' + ADDRESS + '/tail';

const settle = () => new Promise((resolve) => setImmediate(resolve));

function harness(replies) {
  const docListeners = {};
  const components = {};
  const stores = {};
  const timers = [];
  const fetched = [];
  const document = {
    visibilityState: 'hidden',
    addEventListener(name, cb) { (docListeners[name] ||= []).push(cb); },
    removeEventListener() {},
  };
  const windowObj = {
    SessionRenderer: {},
    addEventListener() {}, removeEventListener() {},
    ensureSessionMessages() {}, reconnectEvents() {},
    registerHandler() {}, unregisterHandler() {},
    _es: { readyState: 2 },
  };
  const alpine = {
    data(name, factory) { components[name] = factory; },
    store(name, obj) { if (obj !== undefined) { stores[name] = obj; return obj; } return stores[name]; },
  };
  const fetchFn = (url) => {
    if (!String(url).startsWith(TAIL)) {      // the viewer's other reads
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve([]) });
    }
    fetched.push(url);
    const body = replies.shift();
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
  };
  const sandbox = {
    window: windowObj, document, Alpine: alpine, fetch: fetchFn, console,
    setTimeout(fn, ms) { timers.push({ fn, ms }); return timers.length; },
    clearTimeout() {}, setInterval, clearInterval,
    Promise, JSON, Object, Array, Map, Set, Date, Error, parseInt, parseFloat,
  };
  Object.assign(windowObj, { document, Alpine: alpine, fetch: fetchFn });
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(STORE_JS, 'utf8').replace(
    'setTimeout(ensureSessionMessages, 0);', 'setTimeout(window.ensureSessionMessages, 0);'),
    sandbox, { filename: 'session-store.js' });
  vm.runInContext(fs.readFileSync(VIEWER_JS, 'utf8'), sandbox, { filename: 'session-viewer.js' });
  for (const cb of (docListeners['alpine:init'] || [])) cb();
  const viewer = components.sessionViewerPage({});
  viewer.$watch = () => () => {};
  viewer.$nextTick = (fn) => { if (typeof fn === 'function') fn(); };
  viewer.$refs = {};
  viewer._rebuildDisplay = () => {};
  viewer._scrollToBottom = () => {};
  viewer.sessionKey = ADDRESS;
  viewer._tailUrl = TAIL;
  const store = windowObj.getSessionStore(ADDRESS);
  store.isLive = true;
  store.entries.push({ type: 'assistant_text', content: 'already shown' });
  timers.length = 0;      // setup's own timers are not the retry's
  return { viewer, store, timers, fetched };
}

const TIMED_OUT = {
  entries: [], session_id: ADDRESS, machine: 'sjc-2', machine_pub: PUB,
  machine_reachable: true,
  machine_timed_out: { machine: 'sjc-2', reason: 'reply-timeout', detail: 'no reply within 20.0s' },
};
const ANSWERED = { entries: [], session_id: ADDRESS, machine: 'sjc-2', machine_pub: PUB,
                   machine_reachable: true, is_live: true };

describe('a remote tail that timed out', () => {
  it('keeps what is shown, says so, and is not "unreachable"', () => {
    const { viewer, store } = harness([]);
    viewer._applyTailPayload(store, TIMED_OUT);
    assert.equal(viewer.machineSlow, true);
    assert.equal(viewer.machineDown, false, 'entries stay visible, input stays enabled');
    assert.equal(viewer.machineText(), 'sjc-2 did not answer in time — retrying');
    assert.equal(store.entries.length, 1);
    assert.equal(store.isLive, true, 'a timeout says nothing about the session');
  });

  it('retries the tail with backoff until the machine answers', async () => {
    const { viewer, store, timers, fetched } = harness([TIMED_OUT, ANSWERED]);
    viewer._applyTailPayload(store, TIMED_OUT);
    viewer._applyTailPayload(store, TIMED_OUT);     // one retry pending at a time
    assert.deepEqual(timers.map((t) => t.ms), [3000]);
    timers[0].fn();
    await settle();
    assert.equal(fetched.length, 1);
    assert.ok(fetched[0].startsWith(TAIL + '?tail_entries='));
    assert.deepEqual(timers.map((t) => t.ms), [3000, 6000], 'the second wait doubles');
    timers[1].fn();
    await settle();
    assert.equal(viewer.remoteMachine.state, 'reachable');
    assert.equal(viewer.machineSlow, false);
    assert.equal(timers.length, 2, 'no retry once it answered');
  });

  it('stops retrying once the viewer shows another session', async () => {
    const { viewer, store, timers, fetched } = harness([]);
    viewer._applyTailPayload(store, TIMED_OUT);
    viewer.sessionKey = 'auto-1';
    timers[0].fn();
    await settle();
    assert.equal(fetched.length, 0);
  });
});
