// Tests for the generic Schema proxy runtime in
// ``tools/dashboard/static/js/schemas.js`` (bead auto-2A).
//
// Verifies the proxy correctly consumes the meta-Setting payload shape
// produced by 1D and exposes the generic substrate primitives without
// any pattern-aware or variant-aware convenience yet — those land in
// 2B and 2C respectively. Extension seam is exercised so 2B / 2C have
// a known surface to attach to.

const { describe, it, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const Schema = require('../static/js/schemas.js');


// ── Test fixtures ──────────────────────────────────────────────────

function nonVariantPayload() {
  return {
    set_id: 'dashboard.coordinator-canvas',
    schema_revision: 1,
    type: 'object',
    properties: {
      question: { type: 'string', description: 'The banger' },
      ageMin: { type: 'integer' },
    },
    required: ['question'],
    access_pattern: 'singleton',
    key_strategy: 'fixed:default',
    variants: {},
  };
}

function variantPayload() {
  return {
    set_id: 'dashboard.coordinator-decision',
    schema_revision: 1,
    type: 'object',
    properties: {
      tile_id: { type: 'string', description: 'Tile reference' },
      sentAt: { type: 'string' },
    },
    required: ['tile_id'],
    access_pattern: 'append_only_log',
    key_strategy: 'uuid_v4',
    variants: {
      thumb_yes: {
        type: 'object',
        properties: {
          tile_id: { type: 'string', description: 'Tile reference' },
          sentAt: { type: 'string' },
        },
        required: ['tile_id'],
        variants: {},
      },
      choice: {
        type: 'object',
        properties: {
          tile_id: { type: 'string', description: 'Tile reference' },
          sentAt: { type: 'string' },
          choice: { type: 'string', description: 'Picked text' },
        },
        required: ['tile_id', 'choice'],
        variants: {},
      },
    },
  };
}

function undecoratedPayload() {
  return {
    set_id: 'plain.example',
    schema_revision: 1,
    type: 'object',
    properties: { name: { type: 'string' } },
    required: ['name'],
    access_pattern: null,
    key_strategy: null,
    variants: {},
  };
}

// Stub fetch returns the meta-Setting wrapper (`{payload: <schema>}`)
// for the schema-introspection endpoint and the canned response for
// other URLs. Tests that need different shapes register their own.
function makeFetchStub(routes) {
  const calls = [];
  async function _fetch(path, opts) {
    calls.push({ path: path, opts: opts });
    const handler = routes[path];
    if (!handler) {
      return { ok: false, status: 404, json: async () => ({}) };
    }
    const result = (typeof handler === 'function') ? handler(path, opts) : handler;
    return result;
  }
  _fetch.calls = calls;
  return _fetch;
}

function metaResponse(payload) {
  return {
    ok: true,
    status: 200,
    json: async () => ({ payload: payload }),
  };
}

beforeEach(() => {
  Schema._clearCache();
  Schema._clearExtensions();
  Schema._clearFetchOverride();
});

afterEach(() => {
  Schema._clearCache();
  Schema._clearExtensions();
  Schema._clearFetchOverride();
});


// ── Construction + payload consumption ───────────────────────

describe('Schema.of — non-variant payload', () => {
  it('exposes set_id, revision, fields, required, access_pattern, key_strategy', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-canvas%231':
        metaResponse(nonVariantPayload()),
    });
    Schema._setFetchOverride(stub);

    const proxy = await Schema.of('dashboard.coordinator-canvas');

    assert.equal(proxy.set_id, 'dashboard.coordinator-canvas');
    assert.equal(proxy.revision, 1);
    assert.deepEqual(proxy.fields, {
      question: { type: 'string', description: 'The banger' },
      ageMin: { type: 'integer' },
    });
    assert.deepEqual(proxy.required, ['question']);
    assert.equal(proxy.access_pattern, 'singleton');
    assert.equal(proxy.key_strategy, 'fixed:default');
    assert.deepEqual(proxy.variants, {});
  });
});

describe('Schema.of — variant-bearing payload', () => {
  it('exposes the variants tree as data', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-decision%231':
        metaResponse(variantPayload()),
    });
    Schema._setFetchOverride(stub);

    const proxy = await Schema.of('dashboard.coordinator-decision');

    assert.equal(proxy.access_pattern, 'append_only_log');
    assert.equal(proxy.key_strategy, 'uuid_v4');
    assert.deepEqual(Object.keys(proxy.variants).sort(), ['choice', 'thumb_yes']);
    // Variant payload is the recursive shape: properties + required + nested variants.
    assert.equal(proxy.variants.choice.properties.choice.description, 'Picked text');
    assert.deepEqual(proxy.variants.choice.required, ['tile_id', 'choice']);
  });

  it('does not yet attach per-variant convenience methods', async () => {
    // 2C attaches Decision.thumb_yes / Decision.choice etc. — 2A is generic only.
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-decision%231':
        metaResponse(variantPayload()),
    });
    Schema._setFetchOverride(stub);
    const proxy = await Schema.of('dashboard.coordinator-decision');
    assert.equal(typeof proxy.thumb_yes, 'undefined');
    assert.equal(typeof proxy.choice, 'undefined');
  });
});

describe('Schema.of — undecorated schema', () => {
  it('emits null access_pattern and null key_strategy', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/plain.example%231':
        metaResponse(undecoratedPayload()),
    });
    Schema._setFetchOverride(stub);
    const proxy = await Schema.of('plain.example');
    assert.equal(proxy.access_pattern, null);
    assert.equal(proxy.key_strategy, null);
  });

  it('does not attach pattern-aware convenience methods', async () => {
    // 2B attaches .append / .set / .upsert based on access_pattern — 2A generic only.
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/plain.example%231':
        metaResponse(undecoratedPayload()),
    });
    Schema._setFetchOverride(stub);
    const proxy = await Schema.of('plain.example');
    assert.equal(typeof proxy.append, 'undefined');
    assert.equal(typeof proxy.set, 'undefined');
    assert.equal(typeof proxy.upsert, 'undefined');
  });
});


// ── Caching ──────────────────────────────────────────────────

describe('Schema.of — caching', () => {
  it('returns the same instance on repeated calls', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-canvas%231':
        metaResponse(nonVariantPayload()),
    });
    Schema._setFetchOverride(stub);

    const a = await Schema.of('dashboard.coordinator-canvas');
    const b = await Schema.of('dashboard.coordinator-canvas');

    assert.strictEqual(a, b);
    // And only one fetch (the second was served from cache).
    assert.equal(stub.calls.length, 1);
  });

  it('refetches when force=true', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-canvas%231':
        metaResponse(nonVariantPayload()),
    });
    Schema._setFetchOverride(stub);

    await Schema.of('dashboard.coordinator-canvas');
    await Schema.of('dashboard.coordinator-canvas', { force: true });

    assert.equal(stub.calls.length, 2);
  });
});


// ── Generic read paths ───────────────────────────────────────

describe('Schema.read', () => {
  it('issues GET against /api/graph/settings/<set_id>/<key>', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-canvas%231':
        metaResponse(nonVariantPayload()),
      '/api/graph/settings/dashboard.coordinator-canvas/default': {
        ok: true,
        status: 200,
        json: async () => ({ payload: { question: 'q' }, key: 'default' }),
      },
    });
    Schema._setFetchOverride(stub);

    const proxy = await Schema.of('dashboard.coordinator-canvas');
    const member = await proxy.read('default');

    assert.equal(member.payload.question, 'q');
    assert.equal(member.key, 'default');
  });

  it('returns null on non-OK response', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/plain.example%231':
        metaResponse(undecoratedPayload()),
      // Missing handler for /api/graph/settings/plain.example/<key> — 404 default.
    });
    Schema._setFetchOverride(stub);

    const proxy = await Schema.of('plain.example');
    const member = await proxy.read('missing');
    assert.equal(member, null);
  });

  it('forwards target_revision in the query string', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-canvas%231':
        metaResponse(nonVariantPayload()),
      '/api/graph/settings/dashboard.coordinator-canvas/default?target_revision=2': {
        ok: true,
        status: 200,
        json: async () => ({ payload: { question: 'q' } }),
      },
    });
    Schema._setFetchOverride(stub);

    const proxy = await Schema.of('dashboard.coordinator-canvas');
    const member = await proxy.read('default', { target_revision: 2 });
    assert.equal(member.payload.question, 'q');
  });
});

describe('Schema.all', () => {
  it('issues GET against /api/graph/settings/<set_id> and unwraps members', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-canvas%231':
        metaResponse(nonVariantPayload()),
      '/api/graph/settings/dashboard.coordinator-canvas': {
        ok: true,
        status: 200,
        json: async () => ({ members: [{ key: 'a' }, { key: 'b' }] }),
      },
    });
    Schema._setFetchOverride(stub);

    const proxy = await Schema.of('dashboard.coordinator-canvas');
    const members = await proxy.all();
    assert.deepEqual(members, [{ key: 'a' }, { key: 'b' }]);
  });

  it('returns empty array when members are missing or response not OK', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/plain.example%231':
        metaResponse(undecoratedPayload()),
      '/api/graph/settings/plain.example': {
        ok: true,
        status: 200,
        json: async () => ({}),  // no `members` key
      },
    });
    Schema._setFetchOverride(stub);

    const proxy = await Schema.of('plain.example');
    const members = await proxy.all();
    assert.deepEqual(members, []);
  });
});


// ── Generic write — the substrate escape hatch ───────────────

describe('Schema.write', () => {
  it('POSTs to /api/graph/setting with set_id, schema_revision, key, payload', async () => {
    let captured = null;
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-canvas%231':
        metaResponse(nonVariantPayload()),
      '/api/graph/setting': (path, opts) => {
        captured = { path: path, opts: opts };
        return { ok: true, status: 200, json: async () => ({ ok: true }) };
      },
    });
    Schema._setFetchOverride(stub);

    const proxy = await Schema.of('dashboard.coordinator-canvas');
    const result = await proxy.write({
      key: 'default',
      payload: { question: 'q' },
    });

    assert.equal(result.ok, true);
    assert.equal(captured.opts.method, 'POST');
    const body = JSON.parse(captured.opts.body);
    assert.equal(body.set_id, 'dashboard.coordinator-canvas');
    assert.equal(body.schema_revision, 1);
    assert.equal(body.key, 'default');
    assert.deepEqual(body.payload, { question: 'q' });
  });

  it('rejects on missing key', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-canvas%231':
        metaResponse(nonVariantPayload()),
    });
    Schema._setFetchOverride(stub);

    const proxy = await Schema.of('dashboard.coordinator-canvas');
    await assert.rejects(
      () => proxy.write({ payload: { question: 'q' } }),
      /requires a string key/,
    );
  });

  it('throws when the server rejects the write', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-canvas%231':
        metaResponse(nonVariantPayload()),
      '/api/graph/setting': {
        ok: false, status: 400,
        json: async () => ({ error: 'validation failed' }),
      },
    });
    Schema._setFetchOverride(stub);

    const proxy = await Schema.of('dashboard.coordinator-canvas');
    await assert.rejects(
      () => proxy.write({ key: 'default', payload: { question: 'q' } }),
      /validation failed/,
    );
  });
});


// ── Subscription ─────────────────────────────────────────────

describe('Schema.onChange', () => {
  it('returns a no-op unsubscriber when window.dashboardEvents is missing', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/plain.example%231':
        metaResponse(undecoratedPayload()),
    });
    Schema._setFetchOverride(stub);
    const proxy = await Schema.of('plain.example');
    // No window.dashboardEvents in this Node test environment.
    const unsub = proxy.onChange(() => {});
    assert.equal(typeof unsub, 'function');
    unsub();  // does not throw
  });

  it('returns a no-op unsubscriber when callback is not a function', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/plain.example%231':
        metaResponse(undecoratedPayload()),
    });
    Schema._setFetchOverride(stub);
    const proxy = await Schema.of('plain.example');
    const unsub = proxy.onChange(null);
    assert.equal(typeof unsub, 'function');
  });
});


// ── Extension seam (2B / 2C land via this path) ──────────────

describe('Schema._registerExtension', () => {
  it('invokes registered extensions with (proxy, payload) at construction', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-canvas%231':
        metaResponse(nonVariantPayload()),
    });
    Schema._setFetchOverride(stub);

    let captured = null;
    Schema._registerExtension(function(proxy, payload) {
      captured = { proxy: proxy, payload: payload };
      proxy.extensionMarker = true;
    });

    const proxy = await Schema.of('dashboard.coordinator-canvas');
    assert.ok(captured);
    assert.strictEqual(captured.proxy, proxy);
    assert.equal(captured.payload.set_id, 'dashboard.coordinator-canvas');
    assert.equal(proxy.extensionMarker, true);
  });

  it('multiple extensions stack in registration order', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/plain.example%231':
        metaResponse(undecoratedPayload()),
    });
    Schema._setFetchOverride(stub);

    const order = [];
    Schema._registerExtension(function(p) { order.push('first'); p.first = true; });
    Schema._registerExtension(function(p) { order.push('second'); p.second = true; });

    const proxy = await Schema.of('plain.example');
    assert.deepEqual(order, ['first', 'second']);
    assert.equal(proxy.first, true);
    assert.equal(proxy.second, true);
  });

  it('does not break the proxy when an extension throws', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/plain.example%231':
        metaResponse(undecoratedPayload()),
    });
    Schema._setFetchOverride(stub);

    // Silence the warning for clean test output.
    const origWarn = console.warn;
    console.warn = () => {};
    try {
      Schema._registerExtension(function() { throw new Error('boom'); });
      Schema._registerExtension(function(p) { p.afterFailure = true; });
      const proxy = await Schema.of('plain.example');
      assert.equal(proxy.set_id, 'plain.example');
      assert.equal(proxy.afterFailure, true);
    } finally {
      console.warn = origWarn;
    }
  });

  it('extensions read the variants tree to know whether the schema has variants', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-decision%231':
        metaResponse(variantPayload()),
      '/api/graph/settings/autonomy.schema/plain.example%231':
        metaResponse(undecoratedPayload()),
    });
    Schema._setFetchOverride(stub);

    Schema._registerExtension(function(proxy, payload) {
      proxy.variantSlugs = Object.keys(payload.variants || {});
    });

    const decision = await Schema.of('dashboard.coordinator-decision');
    const plain = await Schema.of('plain.example');

    assert.deepEqual(decision.variantSlugs.sort(), ['choice', 'thumb_yes']);
    assert.deepEqual(plain.variantSlugs, []);
  });
});


// ── Error paths ──────────────────────────────────────────────

describe('Schema.of — error handling', () => {
  it('throws when the meta-Setting payload fetch fails', async () => {
    const stub = makeFetchStub({});  // 404 for everything
    Schema._setFetchOverride(stub);

    await assert.rejects(
      () => Schema.of('does.not.exist'),
      /failed to fetch payload/,
    );
  });

  it('throws when the response is empty or malformed', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/empty%231': {
        ok: true, status: 200, json: async () => ({}),
      },
    });
    Schema._setFetchOverride(stub);

    await assert.rejects(
      () => Schema.of('empty'),
      /empty or malformed/,
    );
  });

  it('throws on non-string set_id', async () => {
    await assert.rejects(
      () => Schema.of(123),
      /requires a string set_id/,
    );
  });
});
