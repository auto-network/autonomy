// A remote session's tail that ran out of time: the
// viewer keeps what it shows, says the machine did not answer in time, and
// fetches the tail again only when something shows the machine answering --
// never on a timer, never "unreachable" with 0 entries (auto-gv73v).
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
    assert.equal(viewer.machineText(), 'sjc-2 did not answer in time \u2014 retrying');
    assert.equal(store.entries.length, 1);
    assert.equal(store.isLive, true, 'a timeout says nothing about the session');
  });

  it('sets no timer: nothing runs while nothing happens', () => {
    const { viewer, store, timers, fetched } = harness([]);
    viewer._applyTailPayload(store, TIMED_OUT);
    assert.equal(timers.length, 0);
    assert.equal(fetched.length, 0);
  });

  it('fetches the tail once when something shows the machine answering', async () => {
    const { viewer, store, fetched } = harness([ANSWERED]);
    viewer._applyTailPayload(store, TIMED_OUT);
    viewer._refetchAfterTimeout();      // e.g. a live entry arrived
    viewer._refetchAfterTimeout();      // and another, before the answer: still one fetch
    await settle();
    assert.equal(fetched.length, 1);
    assert.ok(fetched[0].startsWith(TAIL + '?tail_entries='));
    assert.equal(viewer.remoteMachine.state, 'reachable');
    viewer._refetchAfterTimeout();      // answered: nothing more to do
    await settle();
    assert.equal(fetched.length, 1);
  });

  it('a refetch that times out again waits for the next sign, not a timer', async () => {
    const { viewer, store, timers, fetched } = harness([TIMED_OUT]);
    viewer._applyTailPayload(store, TIMED_OUT);
    viewer._refetchAfterTimeout();
    await settle();
    assert.equal(fetched.length, 1);
    assert.equal(viewer.machineSlow, true);
    assert.equal(timers.length, 0);
    assert.equal(store.entries.length, 1);
  });

  it('does nothing when the machine is answering', async () => {
    const { viewer, fetched } = harness([]);
    viewer._refetchAfterTimeout();
    await settle();
    assert.equal(fetched.length, 0);
  });
});
