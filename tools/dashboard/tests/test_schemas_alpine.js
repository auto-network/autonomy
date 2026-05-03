// Tests for ``Schema.alpine()`` — the Alpine-factory reflection wrapper
// (bead auto-2D).
//
// ``Schema.alpine(state, {schemas})`` wraps an Alpine state object so
// that ``init()`` pre-fetches each named schema via ``Schema.of()`` and
// attaches the resulting proxies to the state. The original ``init()``
// (if present) runs after attachment; the original ``destroy()`` (if
// present) runs in the wrapped destroy.
//
// 2D ships the explicit-schemas form. The page-shell injection seam
// (``window.Autonomy._activePluginId``) is exercised here so a future
// follow-up can autobind from the plugin's manifest without breaking
// 2D's contract.
//
// 4B (separate bead) rewrites coordinator-board's page.js to use this
// wrapper; 2D itself does not touch coordinator-board.

const { describe, it, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const Schema = require('../static/js/schemas.js');


// ── Test fixtures ──────────────────────────────────────────────────

function nonVariantPayload(setId) {
  return {
    set_id: setId,
    schema_revision: 1,
    type: 'object',
    properties: { name: { type: 'string' } },
    required: ['name'],
    access_pattern: null,
    key_strategy: null,
    variants: {},
  };
}

function appendOnlyPayload(setId) {
  return {
    set_id: setId,
    schema_revision: 1,
    type: 'object',
    properties: { tile_id: { type: 'string' } },
    required: ['tile_id'],
    access_pattern: 'append_only_log',
    key_strategy: 'uuid_v4',
    variants: {},
  };
}

function makeFetchStub(routes) {
  const calls = [];
  async function _fetch(path, opts) {
    calls.push({ path: path, opts: opts });
    const handler = routes[path];
    if (!handler) {
      return { ok: false, status: 404, json: async () => ({}) };
    }
    return (typeof handler === 'function') ? handler(path, opts) : handler;
  }
  _fetch.calls = calls;
  return _fetch;
}

function metaResponse(payload) {
  return { ok: true, status: 200, json: async () => ({ payload: payload }) };
}

beforeEach(() => {
  Schema._clearCache();
  Schema._clearExtensions();
  Schema._registerExtension(Schema._patternExtension);
  Schema._registerExtension(Schema._variantExtension);
  Schema._clearFetchOverride();
  // Clean window state — these tests run in node, no real window.
  if (typeof globalThis.window === 'undefined') {
    globalThis.window = { Autonomy: {} };
  } else {
    globalThis.window.Autonomy = globalThis.window.Autonomy || {};
    globalThis.window.Autonomy._activePluginId = null;
  }
});

afterEach(() => {
  Schema._clearCache();
  Schema._clearExtensions();
  Schema._clearFetchOverride();
});


// ── Wrapper shape ────────────────────────────────────────────

describe('Schema.alpine — wrapper shape', () => {
  it('returns a state with init wired', () => {
    const state = Schema.alpine({ tab: 'primary' }, {
      schemas: { Foo: 'x.foo' },
    });
    assert.equal(state.tab, 'primary');
    assert.equal(typeof state.init, 'function');
  });

  it('preserves user-defined fields and methods', () => {
    const userMethod = function() { return 42; };
    const state = Schema.alpine({
      counter: 0,
      myMethod: userMethod,
    }, { schemas: {} });

    assert.equal(state.counter, 0);
    assert.equal(state.myMethod, userMethod);
  });

  it('throws on non-object state', () => {
    assert.throws(() => Schema.alpine(null), /requires a state object/);
    assert.throws(() => Schema.alpine(undefined), /requires a state object/);
    assert.throws(() => Schema.alpine('not-an-object'), /requires a state object/);
  });

  it('throws when schemas option is not an object', () => {
    assert.throws(
      () => Schema.alpine({}, { schemas: 'not-a-map' }),
      /name→set_id map/,
    );
  });
});


// ── init() composition ───────────────────────────────────────

describe('Schema.alpine — init() composition', () => {
  it('attaches each named schema as a proxy on the state', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/x.foo%231':
        metaResponse(nonVariantPayload('x.foo')),
      '/api/graph/settings/autonomy.schema/x.bar%231':
        metaResponse(nonVariantPayload('x.bar')),
    });
    Schema._setFetchOverride(stub);

    const state = Schema.alpine({}, {
      schemas: { Foo: 'x.foo', Bar: 'x.bar' },
    });

    await state.init();

    assert.equal(typeof state.Foo, 'object');
    assert.equal(state.Foo.set_id, 'x.foo');
    assert.equal(state.Bar.set_id, 'x.bar');
  });

  it('calls the original init() AFTER schema attachment', async () => {
    const events = [];
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/x.foo%231':
        metaResponse(appendOnlyPayload('x.foo')),
    });
    Schema._setFetchOverride(stub);

    const state = Schema.alpine({
      async init() {
        // By the time this runs, this.Foo should already be attached.
        events.push('orig-init-saw-Foo=' + (typeof this.Foo));
      },
    }, { schemas: { Foo: 'x.foo' } });

    await state.init();
    assert.deepEqual(events, ['orig-init-saw-Foo=object']);
  });

  it('works when no original init() is provided', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/x.foo%231':
        metaResponse(nonVariantPayload('x.foo')),
    });
    Schema._setFetchOverride(stub);

    const state = Schema.alpine({}, { schemas: { Foo: 'x.foo' } });
    await state.init();
    assert.equal(state.Foo.set_id, 'x.foo');
  });

  it('works when no schemas are provided (degenerate but valid)', async () => {
    const events = [];
    const state = Schema.alpine({
      async init() { events.push('user-init'); },
    });

    await state.init();
    assert.deepEqual(events, ['user-init']);
  });

  it('attaches proxies in parallel (single tick)', async () => {
    let pending = 0;
    let maxPending = 0;
    async function delayedRoute(path) {
      pending += 1;
      maxPending = Math.max(maxPending, pending);
      await new Promise(r => setTimeout(r, 10));
      pending -= 1;
      return metaResponse(nonVariantPayload(path.split('/').pop().replace('%231', '')));
    }
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/x.a%231': delayedRoute,
      '/api/graph/settings/autonomy.schema/x.b%231': delayedRoute,
      '/api/graph/settings/autonomy.schema/x.c%231': delayedRoute,
    });
    Schema._setFetchOverride(stub);

    const state = Schema.alpine({}, {
      schemas: { A: 'x.a', B: 'x.b', C: 'x.c' },
    });
    await state.init();
    // All three fetches were in flight simultaneously, not serialised.
    assert.equal(maxPending, 3);
  });
});


// ── Per-entry revision binding ───────────────────────────────

describe('Schema.alpine — explicit revision per entry', () => {
  it('binds a schema at a non-default revision when entry is {set_id, revision}', async () => {
    function payloadV(rev) {
      return {
        set_id: 'multi.rev', schema_revision: rev,
        type: 'object', properties: { a: { type: 'string' } },
        required: ['a'], access_pattern: null, key_strategy: null,
        variants: {},
      };
    }
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/multi.rev%231': metaResponse(payloadV(1)),
      '/api/graph/settings/autonomy.schema/multi.rev%232': metaResponse(payloadV(2)),
    });
    Schema._setFetchOverride(stub);

    const state = Schema.alpine({}, {
      schemas: {
        Plain: 'multi.rev',
        Bumped: { set_id: 'multi.rev', revision: 2 },
      },
    });
    await state.init();

    assert.equal(state.Plain.revision, 1);
    assert.equal(state.Bumped.revision, 2);
    assert.notStrictEqual(state.Plain, state.Bumped);
  });

  it('throws when entry is neither a string nor a {set_id, revision} object', async () => {
    const stub = makeFetchStub({});
    Schema._setFetchOverride(stub);

    const state = Schema.alpine({}, {
      schemas: { Bad: { revision: 2 } },  // missing set_id
    });
    await assert.rejects(state.init(), /must be a set_id string/);
  });
});


// ── destroy() composition ────────────────────────────────────

describe('Schema.alpine — destroy() composition', () => {
  it('calls the original destroy() when provided', () => {
    let called = false;
    const state = Schema.alpine({
      destroy() { called = true; },
    }, { schemas: {} });

    state.destroy();
    assert.equal(called, true);
  });

  it('works when no original destroy() is provided', () => {
    const state = Schema.alpine({}, { schemas: {} });
    state.destroy();  // does not throw
  });

  it('does not throw if the original destroy() raises', () => {
    const origWarn = console.warn;
    console.warn = () => {};
    try {
      const state = Schema.alpine({
        destroy() { throw new Error('boom'); },
      }, { schemas: {} });
      state.destroy();  // does not propagate
    } finally {
      console.warn = origWarn;
    }
  });
});


// ── onChange auto-dispose ────────────────────────────────────
//
// Schema.alpine wraps every attached proxy in a tracking shim that
// captures the unsubscribe functions returned by ``.onChange`` and
// drains them on destroy. Consumers therefore don't need to maintain
// their own _unsubs arrays — the wrapper is the dispose surface.

describe('Schema.alpine — onChange auto-dispose', () => {
  function fakeProxyOnChange() {
    const calls = [];
    let nextId = 0;
    const fn = function(callback) {
      const id = ++nextId;
      calls.push({ id, callback, alive: true });
      return function unsub() {
        const entry = calls.find(c => c.id === id);
        if (entry) entry.alive = false;
      };
    };
    fn._calls = calls;
    return fn;
  }

  it('disposes registered onChange listeners on destroy', async () => {
    const fooFetch = makeFetchStub({
      '/api/graph/settings/autonomy.schema/x.foo%231': metaResponse(nonVariantPayload('x.foo')),
    });
    Schema._setFetchOverride(fooFetch);

    const state = Schema.alpine({
      async init() {
        this.Foo.onChange(() => {});
        this.Foo.onChange(() => {});
      },
    }, { schemas: { Foo: 'x.foo' } });

    // Replace the cached proxy's onChange with a fake we can inspect.
    await state.init.call(state);
    // The shim's onChange forwards to the underlying proxy's onChange,
    // which is the no-op fallback in test (events.js not loaded). Swap
    // it for a tracker so we can assert dispose actually runs.
    const cached = await Schema.of('x.foo');  // returns same cached proxy
    cached.onChange = fakeProxyOnChange();
    // Re-register through the shim so the tracker observes them.
    state.Foo.onChange(() => {});
    state.Foo.onChange(() => {});
    assert.equal(cached.onChange._calls.length, 2);
    assert.equal(cached.onChange._calls.every(c => c.alive), true);

    state.destroy();
    assert.equal(cached.onChange._calls.every(c => !c.alive), true,
      'all listeners registered through the shim should be released');
  });

  it('does not affect onChange listeners of other components sharing the same cached proxy', async () => {
    const fooFetch = makeFetchStub({
      '/api/graph/settings/autonomy.schema/x.foo%231': metaResponse(nonVariantPayload('x.foo')),
    });
    Schema._setFetchOverride(fooFetch);

    const stateA = Schema.alpine({}, { schemas: { Foo: 'x.foo' } });
    await stateA.init.call(stateA);
    const stateB = Schema.alpine({}, { schemas: { Foo: 'x.foo' } });
    await stateB.init.call(stateB);

    const cached = await Schema.of('x.foo');
    cached.onChange = fakeProxyOnChange();

    stateA.Foo.onChange(() => {});  // listener owned by A
    stateB.Foo.onChange(() => {});  // listener owned by B
    assert.equal(cached.onChange._calls.length, 2);

    stateA.destroy();
    // A's listener released, B's untouched.
    assert.equal(cached.onChange._calls[0].alive, false);
    assert.equal(cached.onChange._calls[1].alive, true);

    stateB.destroy();
    assert.equal(cached.onChange._calls[1].alive, false);
  });

  it('runs auto-dispose before the consumer destroy() so handlers stop firing during teardown', async () => {
    const fooFetch = makeFetchStub({
      '/api/graph/settings/autonomy.schema/x.foo%231': metaResponse(nonVariantPayload('x.foo')),
    });
    Schema._setFetchOverride(fooFetch);

    const order = [];
    const state = Schema.alpine({
      destroy() { order.push('user-destroy'); },
    }, { schemas: { Foo: 'x.foo' } });
    await state.init.call(state);

    const cached = await Schema.of('x.foo');
    cached.onChange = function() {
      order.push('onChange-registered');
      return function() { order.push('listener-released'); };
    };
    state.Foo.onChange(() => {});

    state.destroy();
    assert.deepEqual(order, [
      'onChange-registered',
      'listener-released',
      'user-destroy',
    ]);
  });

  it('continues releasing remaining listeners if one unsub throws', async () => {
    const fooFetch = makeFetchStub({
      '/api/graph/settings/autonomy.schema/x.foo%231': metaResponse(nonVariantPayload('x.foo')),
    });
    Schema._setFetchOverride(fooFetch);

    const state = Schema.alpine({}, { schemas: { Foo: 'x.foo' } });
    await state.init.call(state);

    const released = [];
    const cached = await Schema.of('x.foo');
    let unsubId = 0;
    cached.onChange = function() {
      const id = ++unsubId;
      return function() {
        if (id === 1) throw new Error('boom');
        released.push(id);
      };
    };
    state.Foo.onChange(() => {});
    state.Foo.onChange(() => {});
    state.Foo.onChange(() => {});

    const origWarn = console.warn;
    console.warn = () => {};
    try {
      state.destroy();
    } finally {
      console.warn = origWarn;
    }
    assert.deepEqual(released, [2, 3]);
  });
});


// ── Page-shell injection seam ────────────────────────────────

describe('Schema.alpine — page-shell injection seam', () => {
  it('reads window.Autonomy._activePluginId when present', async () => {
    globalThis.window.Autonomy._activePluginId = 'coordinator-board';
    const state = Schema.alpine({}, { schemas: {} });
    assert.equal(state._pluginId, 'coordinator-board');
  });

  it('leaves _pluginId unset when window.Autonomy._activePluginId is missing', async () => {
    globalThis.window.Autonomy._activePluginId = null;
    const state = Schema.alpine({}, { schemas: {} });
    assert.equal(typeof state._pluginId, 'undefined');
  });

  it('preserves user-supplied _pluginId over the auto-injected value', async () => {
    globalThis.window.Autonomy._activePluginId = 'coordinator-board';
    const state = Schema.alpine({ _pluginId: 'explicit-override' }, {
      schemas: {},
    });
    assert.equal(state._pluginId, 'explicit-override');
  });
});


// ── End-to-end: typed methods reachable through Alpine state ─

describe('Schema.alpine — typed methods on attached proxies', () => {
  it('proxies expose pattern and variant methods just like Schema.of', async () => {
    const variantPayload = appendOnlyPayload('x.decision');
    variantPayload.variants = {
      thumb_yes: {
        type: 'object', properties: {}, required: [], variants: {},
      },
    };
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/x.decision%231':
        metaResponse(variantPayload),
    });
    Schema._setFetchOverride(stub);

    const state = Schema.alpine({}, { schemas: { Decision: 'x.decision' } });
    await state.init();

    assert.equal(typeof state.Decision.append, 'function');
    assert.equal(typeof state.Decision.thumb_yes, 'function');
  });
});
