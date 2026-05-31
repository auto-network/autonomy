/**
 * auto-xkdoi: Draft durability — drafts must survive full reload and iOS
 * backgrounding/eviction, not just soft SPA navigation.
 *
 * The in-memory session store is lost on full page reload and when iOS
 * Safari evicts a backgrounded page from memory (the "swipe away and come
 * back" case). session-store.js now mirrors every draft to localStorage via
 * saveDraft/loadDraft/clearDraft so composed-but-unsent text is never lost.
 *
 * These tests verify the localStorage-backed helpers directly:
 *   1. saveDraft persists text; loadDraft reads it back across a simulated
 *      reload (fresh module load, same localStorage).
 *   2. Empty/blank save removes the key (no stale empty drafts).
 *   3. clearDraft removes the key (send path).
 *   4. Helpers are resilient when localStorage throws (private mode/quota).
 *   5. getSessionStore is unaffected and still returns a store with draftText.
 *
 * Run: node --test tools/dashboard/tests/test_draft_durability.js
 */
const { describe, it, beforeEach } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const STORE_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/session-store.js');

function makeLocalStorage(backing) {
  const store = backing || {};
  return {
    _store: store,
    getItem(k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
    setItem(k, v) { store[k] = String(v); },
    removeItem(k) { delete store[k]; },
  };
}

// Load session-store.js into a fresh sandbox sharing the given localStorage
// backing object, so two loads can simulate a page reload against the same
// persistent storage.
function loadStore(localStorageBacking, opts) {
  const docListeners = {};
  const stores = {};
  const windowObj = {};
  const document = {
    addEventListener(name, cb) { (docListeners[name] ||= []).push(cb); },
  };
  const alpine = {
    store(name, obj) {
      if (obj !== undefined) { stores[name] = obj; return obj; }
      return stores[name];
    },
  };
  windowObj.localStorage = (opts && opts.throwing)
    ? { getItem() { throw new Error('boom'); },
        setItem() { throw new Error('boom'); },
        removeItem() { throw new Error('boom'); } }
    : makeLocalStorage(localStorageBacking);

  const sandbox = {
    window: windowObj,
    document,
    Alpine: alpine,
    fetch: () => Promise.resolve({ json: () => Promise.resolve([]) }),
    console,
    setTimeout,
    clearTimeout,
  };
  vm.createContext(sandbox);
  // The file kicks off SSE wiring at load via `setTimeout(ensureSessionMessages, 0)`.
  // These tests only exercise the draft helpers, so drop that load-time timer to
  // avoid an async leak into later tests (it would touch registerHandler etc.).
  let storeSrc = fs.readFileSync(STORE_JS, 'utf8').replace(
    'setTimeout(ensureSessionMessages, 0);',
    '/* SSE bootstrap disabled in draft-helper test */'
  );
  vm.runInContext(storeSrc, sandbox, { filename: STORE_JS });
  // alpine:init wires Alpine.store('sessions'); fire it so getSessionStore works.
  (docListeners['alpine:init'] || []).forEach((cb) => cb());
  return windowObj;
}

describe('draft durability — localStorage helpers', () => {
  it('persists a draft and reads it back across a simulated reload', () => {
    const backing = {};
    const w1 = loadStore(backing);
    w1.saveDraft('auto-abc', 'half-typed message');
    // Simulate full page reload: fresh module load, same localStorage backing.
    const w2 = loadStore(backing);
    assert.equal(w2.loadDraft('auto-abc'), 'half-typed message');
  });

  it('keeps drafts isolated per session id', () => {
    const backing = {};
    const w = loadStore(backing);
    w.saveDraft('auto-a', 'aaa');
    w.saveDraft('auto-b', 'bbb');
    assert.equal(w.loadDraft('auto-a'), 'aaa');
    assert.equal(w.loadDraft('auto-b'), 'bbb');
  });

  it('removes the key when saving empty text (no stale empty drafts)', () => {
    const backing = {};
    const w = loadStore(backing);
    w.saveDraft('auto-abc', 'something');
    w.saveDraft('auto-abc', '');
    assert.equal(w.loadDraft('auto-abc'), '');
    assert.equal(Object.keys(backing).length, 0);
  });

  it('clearDraft removes the persisted draft (send path)', () => {
    const backing = {};
    const w = loadStore(backing);
    w.saveDraft('auto-abc', 'to be sent');
    w.clearDraft('auto-abc');
    assert.equal(w.loadDraft('auto-abc'), '');
    assert.equal(Object.keys(backing).length, 0);
  });

  it('returns empty for unknown/empty session ids without throwing', () => {
    const w = loadStore({});
    assert.equal(w.loadDraft('never-saved'), '');
    assert.equal(w.loadDraft(''), '');
    assert.doesNotThrow(() => w.saveDraft('', 'x'));
    assert.doesNotThrow(() => w.clearDraft(''));
  });

  it('degrades gracefully when localStorage throws (private mode / quota)', () => {
    const w = loadStore(null, { throwing: true });
    assert.doesNotThrow(() => w.saveDraft('auto-abc', 'x'));
    assert.doesNotThrow(() => w.clearDraft('auto-abc'));
    // loadDraft swallows the throw and yields empty rather than crashing mount.
    assert.equal(w.loadDraft('auto-abc'), '');
  });

  it('does not disturb getSessionStore (draftText field still present)', () => {
    const w = loadStore({});
    const s = w.getSessionStore('auto-abc');
    assert.equal(s.draftText, '');
    assert.ok(Array.isArray(s.entries));
  });
});
