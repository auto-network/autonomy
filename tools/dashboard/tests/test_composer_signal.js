/**
 * auto-xkdoi / voice #28: the "composer surface active" cross-component signal.
 *
 * The pending/outbox tile and the voice side MUST key on the IDENTICAL
 * condition for "this viewer's bottom composer surface is active" or they
 * disagree (caption suppressed but no tile, or tile up but voice still POSTs).
 * The viewer owns that signal: getter `_composerActive` mirrors the exact
 * condition .sv-input renders under (session-view.html:289), and
 * `_syncComposerSignal()` publishes it to document.body as
 * `body.sv-viewer-composer-active` + `body.dataset.svComposerSession = <sid>`.
 * Contract note: cbb8497c-a1f.
 *
 * Run: node --test tools/dashboard/tests/test_composer_signal.js
 */
const { describe, it, beforeEach } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const STORE_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/session-store.js');
const VIEWER_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/pages/session-viewer.js');

function makeBody() {
  const classes = new Set();
  const dataset = {};
  return {
    dataset,
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      toggle: (c, on) => { if (on) classes.add(c); else classes.delete(c); },
      contains: (c) => classes.has(c),
    },
    _classes: classes,
  };
}

function harness() {
  const docListeners = {};
  const stores = {};
  const components = {};
  const body = makeBody();
  const document = {
    body,
    addEventListener(name, cb) { (docListeners[name] ||= []).push(cb); },
  };
  const windowObj = { visualViewport: null };
  const alpine = {
    data(name, factory) { components[name] = factory; },
    store(name, obj) {
      if (obj !== undefined) { stores[name] = obj; return obj; }
      return stores[name];
    },
    watch() { return function () {}; },
  };
  const sandbox = {
    window: windowObj, document, Alpine: alpine,
    fetch: () => Promise.resolve({ json: () => Promise.resolve([]) }),
    console, setTimeout, clearTimeout, setInterval, clearInterval,
    URLSearchParams, JSON, Object, Array, Date, Math,
  };
  vm.createContext(sandbox);
  // Disable the load-time SSE bootstrap (bare ident not bound in vm).
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
  return { windowObj, document, body, stores, makeViewer, alpine };
}

describe('composer-active signal (_composerActive + _syncComposerSignal)', () => {
  let h, store;
  beforeEach(() => {
    h = harness();
    store = h.windowObj.getSessionStore('auto-test');
  });

  it('is true for a live, tmux-linked container session', () => {
    store.isLive = true; store.sessionType = 'container'; store.resolved = false;
    const v = h.makeViewer('auto-test');
    assert.equal(v._composerActive, true);
  });

  it('publishes class + dataset sid to body when active', () => {
    store.isLive = true; store.sessionType = 'container';
    const v = h.makeViewer('auto-test');
    v._syncComposerSignal();
    assert.equal(h.body.classList.contains('sv-viewer-composer-active'), true);
    assert.equal(h.body.dataset.svComposerSession, 'auto-test');
  });

  it('clears the signal when the terminal is shown (composer hidden)', () => {
    store.isLive = true; store.sessionType = 'container';
    const v = h.makeViewer('auto-test');
    v._syncComposerSignal();
    assert.equal(h.body.classList.contains('sv-viewer-composer-active'), true);
    v.showTerminal = true;
    v._syncComposerSignal();
    assert.equal(h.body.classList.contains('sv-viewer-composer-active'), false);
    assert.equal(h.body.dataset.svComposerSession, undefined);
  });

  it('is false for a dead session', () => {
    store.isLive = false; store.sessionType = 'container';
    const v = h.makeViewer('auto-test');
    assert.equal(v._composerActive, false);
  });

  it('host session needs _linked (resolved); not active until linked', () => {
    store.isLive = true; store.sessionType = 'host'; store.resolved = false;
    const v = h.makeViewer('auto-test');
    assert.equal(v._composerActive, false);
    store.resolved = true;
    assert.equal(v._composerActive, true);
  });

  it('outbox getter reflects the store; tile-present flag tracks render condition', () => {
    store.isLive = true; store.sessionType = 'container';
    const v = h.makeViewer('auto-test');
    // idle: no outbox, no tile-present even though composer is active
    assert.equal(v.outbox, null);
    v._syncTilePresent();
    assert.equal(h.body.classList.contains('sv-outbox-tile-present'), false);
    // a pending message appears (e.g. voice sets capturing)
    store.outbox = { localId: 'ob_1', state: 'capturing', source: 'voice', text: 'hel', ts: 1 };
    assert.equal(v.outbox.state, 'capturing');
    assert.equal(v.outboxTileClass(), 'is-capturing');
    v._syncTilePresent();
    assert.equal(h.body.classList.contains('sv-outbox-tile-present'), true);
    // message clears (merged into log) → tile-present drops
    store.outbox = null;
    v._syncTilePresent();
    assert.equal(h.body.classList.contains('sv-outbox-tile-present'), false);
  });

  it('tile-present is false when composer inactive even if outbox set', () => {
    store.isLive = false; store.sessionType = 'container';
    store.outbox = { localId: 'ob_2', state: 'sending', source: 'manual', text: 'x', ts: 1 };
    const v = h.makeViewer('auto-test');
    v._syncTilePresent();
    assert.equal(h.body.classList.contains('sv-outbox-tile-present'), false);
  });

  it('empty outbox text does not count as a renderable pending tile', () => {
    store.isLive = true; store.sessionType = 'container';
    store.outbox = { localId: 'ob_empty', state: 'sending', source: 'voice', text: '', ts: 1 };
    const v = h.makeViewer('auto-test');
    assert.equal(v.outbox, null);
    v._syncTilePresent();
    assert.equal(h.body.classList.contains('sv-outbox-tile-present'), false);
  });

  it('resendOutbox flips state back to sending', () => {
    store.isLive = true; store.sessionType = 'container';
    store.outbox = { localId: 'ob_3', state: 'unconfirmed', source: 'manual', text: 'x', ts: 1 };
    const v = h.makeViewer('auto-test');
    v.resendOutbox();
    assert.equal(h.stores.sessions['auto-test'].outbox.state, 'sending');
  });

  it('does not stomp another viewer\'s signal when clearing', () => {
    // viewer B owns the signal; viewer A (inactive) must not clear it.
    h.body.classList.add('sv-viewer-composer-active');
    h.body.dataset.svComposerSession = 'other-session';
    const a = h.makeViewer('auto-test');  // inactive (store not live)
    a._syncComposerSignal();
    assert.equal(h.body.classList.contains('sv-viewer-composer-active'), true);
    assert.equal(h.body.dataset.svComposerSession, 'other-session');
  });
});
