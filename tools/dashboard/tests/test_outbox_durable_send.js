/**
 * auto-xkdoi Phase 2: the durable-send / reconciliation engine.
 *
 * A sent message is NOT cleared on HTTP 200 (that only means tmux accepted the
 * paste). It stays in the outbox (persisted to localStorage) until the JSONL
 * log echoes it back via SSE; if the echo never lands, it parks in
 * 'unconfirmed' — recoverable, never silently dropped. These tests exercise the
 * engine methods directly. Contract: cbb8497c-a1f.
 *
 * Run: node --test tools/dashboard/tests/test_outbox_durable_send.js
 */
const { describe, it, beforeEach } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const STORE_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/session-store.js');
const VIEWER_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/pages/session-viewer.js');

function makeLocalStorage() {
  const store = {};
  return {
    getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
    _store: store,
  };
}

function harness() {
  const docListeners = {};
  const stores = {};
  const components = {};
  const body = { dataset: {}, classList: { add() {}, remove() {}, contains() { return false; } } };
  const document = { body, addEventListener(n, cb) { (docListeners[n] ||= []).push(cb); } };
  let fetchImpl = () => Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) });
  const fetchCalls = [];
  const windowObj = { localStorage: makeLocalStorage() };
  const alpine = {
    data(name, factory) { components[name] = factory; },
    store(name, obj) { if (obj !== undefined) { stores[name] = obj; return obj; } return stores[name]; },
    watch() { return function () {}; },
  };
  const sandbox = {
    window: windowObj, document, Alpine: alpine,
    fetch: (url, opts) => { fetchCalls.push({ url, opts }); return fetchImpl(url, opts); },
    console, setTimeout, clearTimeout, setInterval, clearInterval,
    URLSearchParams, JSON, Object, Array, Date, Math,
  };
  vm.createContext(sandbox);
  const storeSrc = fs.readFileSync(STORE_JS, 'utf8')
    .replace('setTimeout(ensureSessionMessages, 0);', '/* disabled in test */');
  vm.runInContext(storeSrc, sandbox, { filename: STORE_JS });
  vm.runInContext(fs.readFileSync(VIEWER_JS, 'utf8'), sandbox, { filename: VIEWER_JS });
  (docListeners['alpine:init'] || []).forEach((cb) => cb());

  function makeViewer(sessionKey) {
    const v = components.sessionViewerPage({});
    v.$watch = function () { return function () {}; };
    v.$nextTick = function (fn) { if (typeof fn === 'function') fn(); };
    v.sessionKey = sessionKey;
    return v;
  }
  return {
    windowObj, document, stores, makeViewer, fetchCalls,
    setFetch: (fn) => { fetchImpl = fn; },
  };
}

describe('outbox durable send + reconciliation', () => {
  let h, store, v;
  beforeEach(() => {
    h = harness();
    store = h.windowObj.getSessionStore('auto-test');
    store.isLive = true; store.sessionType = 'container';
    v = h.makeViewer('auto-test');
  });

  it('persists the outbox to localStorage BEFORE the POST', async () => {
    store.outbox = { localId: 'ob_a', state: 'sending', source: 'manual', text: 'hello world', ts: 1 };
    await v._durableSend('hello world', 'ob_a');
    assert.equal(h.windowObj.loadOutbox('auto-test').localId, 'ob_a');
    const call = h.fetchCalls.find((c) => c.url === '/api/session/send');
    assert.ok(call, 'should POST to /api/session/send');
    assert.equal(JSON.parse(call.opts.body).message, 'hello world');
  });

  it('stageOutboxSend persists synchronously before the POST resolves', async () => {
    let resolveFetch;
    h.setFetch(() => new Promise((resolve) => {
      resolveFetch = () => resolve({ ok: true, json: () => Promise.resolve({ ok: true }) });
    }));

    const pending = h.windowObj.stageOutboxSend('auto-test', {
      localId: 'ob_stage',
      state: 'capturing',
      source: 'voice',
      text: 'persist before network',
      ts: 1,
    });

    const saved = h.windowObj.loadOutbox('auto-test');
    assert.equal(saved.localId, 'ob_stage');
    assert.equal(saved.state, 'sending');
    assert.equal(saved.text, 'persist before network');
    assert.ok(h.fetchCalls.length >= 1, 'POST owner starts outside any viewer method');

    resolveFetch();
    assert.equal(await pending, true);
  });

  it('restore replays a queued staged outbox that never reached the POST owner', async () => {
    h.windowObj.saveOutbox('auto-test', {
      localId: 'ob_queued',
      state: 'sending',
      source: 'voice',
      text: 'queued before crash',
      ts: 1,
      delivery: 'queued',
    });
    store.outbox = null;

    assert.equal(h.windowObj.restoreOutbox('auto-test'), true);
    assert.equal(store.outbox.localId, 'ob_queued');
    assert.ok(h.fetchCalls.some((c) => c.url === '/api/session/send'), 'queued restore must POST');
  });

  it('does NOT clear the outbox on HTTP 200 (waits for log echo)', async () => {
    store.outbox = { localId: 'ob_b', state: 'sending', source: 'manual', text: 'still pending', ts: 1 };
    await v._durableSend('still pending', 'ob_b');
    assert.ok(store.outbox, 'outbox must survive a 200 — confirmation is the log echo, not the POST');
    assert.equal(store.outbox.state, 'sending');
  });

  it('parks in unconfirmed when the POST fails (text preserved)', async () => {
    h.setFetch(() => Promise.reject(new Error('network down')));
    store.outbox = { localId: 'ob_c', state: 'sending', source: 'manual', text: 'safe text', ts: 1 };
    await v._durableSend('safe text', 'ob_c');
    assert.equal(store.outbox.state, 'unconfirmed');
    assert.equal(store.outbox.text, 'safe text');
    assert.equal(h.windowObj.loadOutbox('auto-test').state, 'unconfirmed');
  });

  it('reconciles (clears) when a matching user entry appears in the log', () => {
    store.outbox = { localId: 'ob_d', state: 'sending', source: 'manual', text: 'reconcile me', ts: 1 };
    h.windowObj.saveOutbox('auto-test', store.outbox);
    store.entries = [
      { type: 'assistant_text', content: 'unrelated' },
      { type: 'user', content: 'reconcile me' },
    ];
    v._tryReconcileOutbox();
    assert.equal(store.outbox, null, 'matched log entry should clear the optimistic tile');
    assert.equal(h.windowObj.loadOutbox('auto-test'), null, 'localStorage cleared too');
  });

  it('does not reconcile on a non-matching entry', () => {
    store.outbox = { localId: 'ob_e', state: 'sending', source: 'manual', text: 'waiting', ts: 1 };
    store.entries = [{ type: 'user', content: 'something else entirely' }];
    v._tryReconcileOutbox();
    assert.ok(store.outbox, 'unmatched log must NOT clear a pending message');
  });

  it('timeout marks a still-sending outbox unconfirmed', () => {
    store.outbox = { localId: 'ob_f', state: 'sending', source: 'manual', text: 'x', ts: 1 };
    v._markOutboxUnconfirmed('ob_f');
    assert.equal(store.outbox.state, 'unconfirmed');
  });

  it('restores a mid-flight outbox after reload and re-arms', () => {
    h.windowObj.saveOutbox('auto-test', { localId: 'ob_g', state: 'sending', source: 'manual', text: 'survived reload', ts: 1 });
    store.outbox = null;          // fresh store after reload
    store.entries = [];
    v._restoreOutbox();
    assert.ok(store.outbox, 'a persisted mid-flight message must come back');
    assert.equal(store.outbox.text, 'survived reload');
  });

  it('restore reconciles immediately if the message already landed while away', () => {
    h.windowObj.saveOutbox('auto-test', { localId: 'ob_h', state: 'sending', source: 'manual', text: 'landed already', ts: 1 });
    store.outbox = null;
    store.entries = [{ type: 'user', content: 'landed already' }];
    v._restoreOutbox();
    assert.equal(store.outbox, null, 'if it already landed, restore should reconcile and clear');
  });

  it('resend re-POSTs an unconfirmed message', () => {
    store.outbox = { localId: 'ob_i', state: 'unconfirmed', source: 'manual', text: 'retry this', ts: 1 };
    v.resendOutbox();
    assert.equal(store.outbox.state, 'sending');
    assert.ok(h.fetchCalls.some((c) => c.url === '/api/session/send'), 'resend must hit the send endpoint');
  });

  it('dismiss clears the persisted outbox so it does not restore on revisit', () => {
    store.outbox = { localId: 'ob_j', state: 'unconfirmed', source: 'manual', text: 'dismiss this', ts: 1 };
    h.windowObj.saveOutbox('auto-test', store.outbox);
    v.dismissOutbox();
    assert.equal(store.outbox, null);
    assert.equal(h.windowObj.loadOutbox('auto-test'), null);
  });
});

// End-to-end lifecycle against the contract (cbb8497c-a1f): the exact sequence
// the voice side drives, walked in order through the real engine.
describe('contract lifecycle end-to-end (cbb8497c-a1f)', () => {
  let h, store, v;
  beforeEach(() => {
    h = harness();
    store = h.windowObj.getSessionStore('auto-test');
    store.isLive = true; store.sessionType = 'container'; store.entries = [];
    v = h.makeViewer('auto-test');
  });

  it('voice capturing -> send-flip -> watcher commits -> reconcile on log echo', async () => {
    // 1. CAPTURING — voice sets the outbox live (newOutboxId at capture start).
    const id = h.windowObj.newOutboxId();
    store.outbox = { localId: id, state: 'capturing', source: 'voice', text: 'scan the new', ts: 1 };
    assert.equal(v.outbox.state, 'capturing');
    assert.equal(v.outboxTileClass(), 'is-capturing');

    // 2. SEND-FLIP — voice finalizes text + flips state (NO direct POST). The
    //    _outboxSendKey watcher would fire _onOutboxSendKey; drive it directly.
    store.outbox = Object.assign({}, store.outbox, { state: 'sending', text: 'scan the new vuln set' });
    await v._onOutboxSendKey();

    // The shared outbox engine owned the POST; outbox is NOT cleared on 200
    // (persisted, sending).
    assert.ok(h.fetchCalls.some((c) => c.url === '/api/session/send'
      && JSON.parse(c.opts.body).message === 'scan the new vuln set'), 'viewer POSTs the final text');
    assert.ok(store.outbox && store.outbox.state === 'sending', 'still pending after 200');
    assert.ok(h.windowObj.loadOutbox('auto-test'), 'persisted to localStorage');

    // 3. RECONCILE — the JSONL log echoes the user turn via SSE; tile merges away.
    store.entries.push({ type: 'user', content: 'scan the new vuln set' });
    v._tryReconcileOutbox();
    assert.equal(store.outbox, null, 'log echo clears the optimistic tile (merge)');
    assert.equal(h.windowObj.loadOutbox('auto-test'), null, 'localStorage cleared on merge');
  });

  it('send -> no log echo -> timeout parks unconfirmed -> Resend re-sends', async () => {
    const id = h.windowObj.newOutboxId();
    store.outbox = { localId: id, state: 'sending', source: 'voice', text: 'never echoed', ts: 1 };
    await v._onOutboxSendKey();
    v._markOutboxUnconfirmed(id);
    assert.equal(store.outbox.state, 'unconfirmed', 'no echo within window -> unconfirmed, not dropped');
    assert.equal(store.outbox.text, 'never echoed', 'text preserved for recovery');

    const before = h.fetchCalls.length;
    v.resendOutbox();
    assert.equal(store.outbox.state, 'sending');
    assert.ok(h.fetchCalls.length > before, 'Resend re-POSTs');
  });
});

// The activity-dot signal: which outbox states light the pending ring.
describe('outboxPendingState (activity-dot pending ring)', () => {
  let h, store;
  beforeEach(() => { h = harness(); store = h.windowObj.getSessionStore('auto-test'); });

  it('empty when there is no pending message', () => {
    assert.equal(h.windowObj.outboxPendingState('auto-test'), '');
  });
  it('EXCLUDES capturing (live dictation — user is right there)', () => {
    store.outbox = { state: 'capturing', text: 'typing', localId: 'x', ts: 1 };
    assert.equal(h.windowObj.outboxPendingState('auto-test'), '');
  });
  it("returns 'sending' for an in-flight message", () => {
    store.outbox = { state: 'sending', text: 'x', localId: 'x', ts: 1 };
    assert.equal(h.windowObj.outboxPendingState('auto-test'), 'sending');
  });
  it("returns 'unconfirmed' for a stuck message", () => {
    store.outbox = { state: 'unconfirmed', text: 'x', localId: 'x', ts: 1 };
    assert.equal(h.windowObj.outboxPendingState('auto-test'), 'unconfirmed');
  });
  it('empty for an unknown session id (read-only, no store created)', () => {
    assert.equal(h.windowObj.outboxPendingState('never-seen'), '');
  });
  it('empty for a falsy id', () => {
    assert.equal(h.windowObj.outboxPendingState(''), '');
  });
});
