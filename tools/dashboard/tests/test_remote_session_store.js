/**
 * The other machines' sessions in the session store (remote_sessions.Mirror,
 * session:remote-registry): a remote row lands in the same store as a local
 * one, this machine's own registry never ends it, and only its own
 * machine's rows do.
 */
const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const STORE_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/session-store.js');

function makeStore() {
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
  const fetchFn = () => Promise.resolve({ json: () => Promise.resolve([]) });
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
  return { sessions: () => alpine.store('sessions'), handlers };
}

const ROW = { session_id: 'auto-9@sjc-2', label: 'Sweep', type: 'container', is_live: true,
  state: 'LAUNCHING', startup_state: 'harness_starting', machine: 'sjc-2',
  machine_pub: 'b1', machine_reachable: true };

describe('session:remote-registry', () => {
  it('puts another machine\'s session in the store with its machine and phase', () => {
    const h = makeStore();
    h.handlers['session:remote-registry']({ machines: { 'sjc-2': [ROW] } });
    const s = h.sessions()['auto-9@sjc-2'];
    assert.equal(s.isLive, true);
    assert.equal(s.label, 'Sweep');
    assert.equal(s.startupState, 'harness_starting');
    assert.equal(s.machine, 'sjc-2');
    assert.equal(s.machineReachable, true);
  });

  it('is not ended by this machine\'s own registry', () => {
    const h = makeStore();
    h.handlers['session:remote-registry']({ machines: { 'sjc-2': [ROW] } });
    h.handlers['session:registry']([{ session_id: 'auto-1', is_live: true }]);
    assert.equal(h.sessions()['auto-9@sjc-2'].isLive, true);
  });

  it('is ended only when its own machine\'s rows leave it out', () => {
    const h = makeStore();
    const other = { ...ROW, session_id: 'auto-5@lab', machine: 'lab' };
    h.handlers['session:remote-registry']({ machines: { 'sjc-2': [ROW], lab: [other] } });
    h.handlers['session:remote-registry']({ machines: { 'sjc-2': [], lab: [other] } });
    assert.equal(h.sessions()['auto-9@sjc-2'].isLive, false);
    assert.equal(h.sessions()['auto-5@lab'].isLive, true);
  });
});
