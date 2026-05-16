/**
 * S0 — Browser-facing live-update test for the feature-flags Alpine store.
 *
 * Loads tools/dashboard/static/js/lib/feature-flags.js in a vm sandbox
 * with a stubbed Schema.of + Alpine, then exercises:
 *
 *   1. window.Autonomy.flags.get / .all delegate to Schema.of(...).read/all.
 *   2. Strict `=== true` gate on `enabled` so non-bool truthy never reads true.
 *   3. Alpine.store('flags')._load() populates a sync cache from .all().
 *   4. store.get(name) returns false until _load resolves, then the cached bool.
 *   5. Dispatching 'autonomy:flag-changed' triggers _refresh which re-loads.
 *   6. onChange callback registered via Schema.prototype.onChange dispatches
 *      'autonomy:flag-changed' with the change event detail.
 *
 * Spec: graph://40dd9d7a-23a (S0).
 * Run: node --test tools/dashboard/tests/test_feature_flags_store.js
 */
const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const FF_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/feature-flags.js');

function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

function makeHarness(rows) {
  // rows: {key: {enabled: bool, description: str, owner: str}}
  let onChangeCb = null;
  const proxy = {
    set_id: 'dashboard.feature_flags',
    read: async function(key) {
      if (!Object.prototype.hasOwnProperty.call(rows, key)) return null;
      return { key, payload: rows[key] };
    },
    all: async function() {
      const out = [];
      for (const k of Object.keys(rows)) out.push({ key: k, payload: rows[k] });
      return out;
    },
    onChange: function(cb) {
      onChangeCb = cb;
      return function() { onChangeCb = null; };
    },
  };

  const stores = {};
  const winListeners = {};
  const docListeners = {};

  const document = {
    addEventListener(name, cb) {
      (docListeners[name] ||= []).push(cb);
    },
  };

  const windowObj = {
    Schema: {
      of: async function(setId, opts) {
        assert.equal(setId, 'dashboard.feature_flags');
        // Cross-context objects don't reference-equal; check the field.
        assert.equal(opts && opts.revision, 1);
        return proxy;
      },
    },
    Alpine: {
      store(name, value) {
        if (value !== undefined) stores[name] = value;
        return stores[name];
      },
    },
    Autonomy: undefined,
    addEventListener(name, cb) {
      (winListeners[name] ||= []).push(cb);
    },
    dispatchEvent(evt) {
      const cbs = winListeners[evt.type] || [];
      for (const cb of cbs) cb(evt);
      return true;
    },
    CustomEvent: function(type, init) {
      this.type = type;
      this.detail = (init && init.detail) || null;
    },
    console: console,
  };
  windowObj.window = windowObj;

  // Fire the change event via the proxy's onChange (the way schemas.js does).
  function fireSettingChanged(detail) {
    if (typeof onChangeCb === 'function') onChangeCb(detail);
  }

  // Fire alpine:init (the way the document does on Alpine boot).
  function fireAlpineInit() {
    const cbs = docListeners['alpine:init'] || [];
    for (const cb of cbs) cb();
  }

  // The module body uses bare `CustomEvent` (browser-global). In vm
  // sandboxes, only properties on the sandbox object are globals — so
  // mirror it there as well as on windowObj.
  function CustomEventCtor(type, init) {
    this.type = type;
    this.detail = (init && init.detail) || null;
  }
  windowObj.CustomEvent = CustomEventCtor;

  const sandbox = {
    window: windowObj,
    document,
    console,
    setTimeout, setImmediate, clearTimeout, clearImmediate,
    Promise,
    CustomEvent: CustomEventCtor,
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(FF_JS, 'utf8'), sandbox, { filename: 'feature-flags.js' });

  return {
    sandbox,
    window: windowObj,
    document,
    stores,
    fireSettingChanged,
    fireAlpineInit,
    winListeners,
  };
}


describe('window.Autonomy.flags async API', () => {
  it('get() returns true for an enabled flag', async () => {
    const h = makeHarness({ 'voice.client_enabled': { enabled: true, description: 'x', owner: 'S5' } });
    assert.equal(await h.window.Autonomy.flags.get('voice.client_enabled'), true);
  });

  it('get() returns false for a disabled flag', async () => {
    const h = makeHarness({ 'voice.pipe_enabled': { enabled: false, description: 'x', owner: 'S3' } });
    assert.equal(await h.window.Autonomy.flags.get('voice.pipe_enabled'), false);
  });

  it('get() returns false for an absent flag', async () => {
    const h = makeHarness({});
    assert.equal(await h.window.Autonomy.flags.get('not.set'), false);
  });

  it('get() gates strictly on `=== true` (truthy non-bool returns false)', async () => {
    const h = makeHarness({ 'voice.corrupted': { enabled: 1, description: 'x', owner: 'x' } });
    assert.equal(await h.window.Autonomy.flags.get('voice.corrupted'), false);
  });

  it('all() returns {name: payload} for every row', async () => {
    const h = makeHarness({
      'a': { enabled: true, description: 'A', owner: 'S0' },
      'b': { enabled: false, description: 'B', owner: 'S0' },
    });
    const snap = await h.window.Autonomy.flags.all();
    // Sandbox-context objects compare by JSON round-trip to avoid the
    // cross-realm reference-equality check that strict deepEqual does.
    assert.equal(
      JSON.stringify(snap),
      JSON.stringify({
        'a': { enabled: true, description: 'A', owner: 'S0' },
        'b': { enabled: false, description: 'B', owner: 'S0' },
      }),
    );
  });
});


describe('Alpine.store("flags") sync cache + live update', () => {
  it('store.get() returns false before _load resolves', async () => {
    const h = makeHarness({ 'voice.client_enabled': { enabled: true, description: 'x', owner: 'S5' } });
    h.fireAlpineInit();
    // _load() is async; before flush, the synchronous read still returns false.
    const store = h.stores.flags;
    assert.equal(store.isLoaded, false);
    assert.equal(store.get('voice.client_enabled'), false);
  });

  it('store.get() returns the cached bool after _load resolves', async () => {
    const h = makeHarness({
      'voice.client_enabled': { enabled: true, description: 'x', owner: 'S5' },
      'voice.pipe_enabled': { enabled: false, description: 'x', owner: 'S3' },
    });
    h.fireAlpineInit();
    await flush();
    await flush();
    const store = h.stores.flags;
    assert.equal(store.isLoaded, true);
    assert.equal(store.get('voice.client_enabled'), true);
    assert.equal(store.get('voice.pipe_enabled'), false);
    assert.equal(store.get('not.set'), false);
  });

  it('autonomy:flag-changed event triggers _refresh which re-reads', async () => {
    const rows = { 'voice.client_enabled': { enabled: false, description: 'x', owner: 'S5' } };
    const h = makeHarness(rows);
    h.fireAlpineInit();
    await flush(); await flush();
    const store = h.stores.flags;
    assert.equal(store.get('voice.client_enabled'), false);

    // Flip the row server-side; fire the change event the way schemas.js would.
    rows['voice.client_enabled'].enabled = true;
    h.fireSettingChanged({ key: 'voice.client_enabled' });
    // The onChange handler dispatches autonomy:flag-changed which triggers _refresh.
    await flush(); await flush();

    assert.equal(store.get('voice.client_enabled'), true);
  });

  it('onChange dispatches autonomy:flag-changed with the event detail', async () => {
    const h = makeHarness({ 'voice.client_enabled': { enabled: true, description: 'x', owner: 'S5' } });
    // Manually trigger the proxy registration by reading once via the async API
    // (which calls _bindOnChange under the hood).
    await h.window.Autonomy.flags.get('voice.client_enabled');

    const seen = [];
    h.window.addEventListener('autonomy:flag-changed', (evt) => seen.push(evt.detail));

    h.fireSettingChanged({ key: 'voice.client_enabled', flipped: true });
    await flush();

    assert.equal(seen.length, 1);
    assert.deepEqual(seen[0], { key: 'voice.client_enabled', flipped: true });
  });

  it('store._load survives an underlying fetch failure without stalling consumers', async () => {
    const h = makeHarness({});
    // Replace Schema.of to throw.
    h.window.Schema.of = async () => { throw new Error('synthetic'); };
    h.window.Autonomy.flags._resetForTests();
    h.fireAlpineInit();
    await flush(); await flush();
    const store = h.stores.flags;
    assert.equal(store.isLoaded, true);   // marked loaded even on failure
    assert.equal(store.get('voice.client_enabled'), false);
  });
});
