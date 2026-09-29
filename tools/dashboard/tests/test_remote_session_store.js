/**
 * The other machines' sessions in the session store. Presence (the synced
 * Settings, GET /api/sessions/presence) says which exist; forwarded events
 * (session:remote-rows, session:ended) update the ones they name. Keys are
 * name@machine_pub; the machine's display name is never part of one.
 */
const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const STORE_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/session-store.js');

function makeStore(presence) {
  const stores = {};
  const alpine = {
    store(name, obj) {
      if (obj !== undefined) { stores[name] = obj; return obj; }
      return stores[name];
    },
  };
  const handlers = {};
  const docListeners = {};
  const doc = { addEventListener(n, cb) { (docListeners[n] ||= []).push(cb); },
    visibilityState: 'visible', referrer: '' };
  const fetchFn = (url) => Promise.resolve({ ok: true, json: () => Promise.resolve(
    url === '/api/sessions/presence' ? { sessions: presence ? presence() : [] } : []) });
  const sandbox = {
    window: {}, document: doc, Alpine: alpine, fetch: fetchFn, console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    Promise, JSON, Object, Array, Map, Set, Date, Error, parseInt, parseFloat,
  };
  Object.assign(sandbox.window, {
    document: doc, Alpine: alpine, fetch: fetchFn,
    registerHandler(topic, fn) { handlers[topic] = fn; },
    unregisterHandler() {},
    dispatchEvent() {},
  });
  sandbox.registerHandler = sandbox.window.registerHandler;
  sandbox.unregisterHandler = sandbox.window.unregisterHandler;
  sandbox.CustomEvent = class { constructor(t, i) { this.type = t; this.detail = (i || {}).detail; } };
  vm.createContext(sandbox);
  const src = fs.readFileSync(STORE_JS, 'utf8').replace(
    'setTimeout(ensureSessionMessages, 0);', 'setTimeout(window.ensureSessionMessages, 0);');
  vm.runInContext(src, sandbox, { filename: 'session-store.js' });
  for (const cb of (docListeners['alpine:init'] || [])) cb();
  sandbox.window.ensureSessionMessages();
  return { sessions: () => alpine.store('sessions'), handlers, win: sandbox.window };
}

const PUB = 'b1'.repeat(32);
const KEY = 'auto-9@' + PUB;
const ROW = { session_id: KEY, label: 'Sweep', type: 'container', is_live: true,
  state: 'LAUNCHING', startup_state: 'harness_starting', machine: 'sjc-2',
  machine_pub: PUB, machine_reachable: true };
const PRESENCE = { session_id: KEY, project: 'p', type: 'container', label: 'Sweep',
  state: 'ACTIVE', is_live: true, machine: 'sjc-2', machine_pub: PUB, machine_reachable: true };
const flush = () => new Promise((r) => setTimeout(r, 0));

describe('remote sessions in the store', () => {
  it('shows presence rows keyed by machine_pub, named by display name', async () => {
    const h = makeStore(() => [PRESENCE]);
    await h.win.loadRemotePresence();
    const s = h.sessions()[KEY];
    assert.equal(s.isLive, true);
    assert.equal(s.machine, 'sjc-2');
    assert.equal(s.machinePub, PUB);
  });

  it('a forwarded row updates the session it names; a presence row never clears its phase', async () => {
    const h = makeStore(() => [PRESENCE]);
    h.handlers['session:remote-rows']({ rows: [ROW] });
    await h.win.loadRemotePresence();
    assert.equal(h.sessions()[KEY].startupState, 'harness_starting');
    assert.equal(h.sessions()[KEY].state, 'LAUNCHING');
  });

  it('is not ended by this machine\'s registry, nor by missing from forwarded rows', () => {
    const h = makeStore();
    h.handlers['session:remote-rows']({ rows: [ROW] });
    h.handlers['session:registry']([{ session_id: 'auto-1', is_live: true }]);
    h.handlers['session:remote-rows']({ rows: [] });
    assert.equal(h.sessions()[KEY].isLive, true);
  });

  it('is ended by its session:ended, or by its presence row going away', async () => {
    const h = makeStore();
    h.handlers['session:remote-rows']({ rows: [ROW] });
    h.handlers['session:ended']({ id: KEY });
    assert.equal(h.sessions()[KEY].isLive, false);

    let rows = [PRESENCE];
    const g = makeStore(() => rows);
    await g.win.loadRemotePresence();
    rows = [];
    await g.win.loadRemotePresence();
    assert.equal(g.sessions()[KEY].isLive, false);
  });
});

describe('what people read for a remote session', () => {
  it('shows name@<display name>, never the machine key', () => {
    const h = makeStore();
    h.handlers['session:remote-rows']({ rows: [ROW] });
    assert.equal(h.win.sessionDisplayName(KEY), 'auto-9@sjc-2');
    assert.equal(h.win.sessionDisplayName(KEY, 'SJC'), 'auto-9@SJC');
    assert.equal(h.win.sessionDisplayName('auto-1'), 'auto-1');
    assert.equal(h.win.sessionDisplayName('auto-5@' + PUB), 'auto-5');   // name not loaded yet
  });
});

