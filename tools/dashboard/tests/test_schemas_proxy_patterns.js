// Tests for the pattern-aware Schema proxy methods (bead auto-2B).
//
// 2B attaches ``.append`` / ``.set`` / ``.upsert`` to proxies based on
// ``payload.access_pattern`` + ``payload.key_strategy`` — the metadata
// 1B's @decorators stamped on the schema and 1D surfaced in the
// meta-Setting payload. Undecorated schemas gain no convenience
// methods; variant-bearing schemas still gain pattern methods (the
// pattern is a property of the base, inherited by variants — the
// per-variant convenience methods are 2C's job).
//
// The pattern extension auto-registers at module load. Each test
// starts with ``_clearExtensions`` + re-registers ``_patternExtension``
// so test ordering against the shared module state stays
// deterministic.

const { describe, it, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const Schema = require('../static/js/schemas.js');


// ── Test fixtures ──────────────────────────────────────────────────

function appendOnlyPayload() {
  return {
    set_id: 'dashboard.coordinator-decision',
    schema_revision: 1,
    type: 'object',
    properties: {
      tile_id: { type: 'string' },
      sentAt: { type: 'string' },
    },
    required: ['tile_id'],
    access_pattern: 'append_only_log',
    key_strategy: 'uuid_v4',
    variants: {},
  };
}

function singletonPayload(keyStrategy) {
  return {
    set_id: 'dashboard.operator-message-to-coordinator',
    schema_revision: 1,
    type: 'object',
    properties: { text: { type: 'string' } },
    required: ['text'],
    access_pattern: 'singleton',
    key_strategy: keyStrategy || 'fixed:default',
    variants: {},
  };
}

function keyedPerEntityPayload() {
  return {
    set_id: 'dashboard.coordinator-tile',
    schema_revision: 1,
    type: 'object',
    properties: { label: { type: 'string' } },
    required: ['label'],
    access_pattern: 'keyed_per_entity',
    key_strategy: 'natural',
    variants: {},
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

function unknownPatternPayload() {
  return {
    set_id: 'future.pattern',
    schema_revision: 1,
    type: 'object',
    properties: {},
    required: [],
    access_pattern: 'lcg_growable_buffer',
    key_strategy: 'invented_for_test',
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

function okResponse(body) {
  return { ok: true, status: 200, json: async () => body };
}

beforeEach(() => {
  Schema._clearCache();
  Schema._clearExtensions();
  Schema._registerExtension(Schema._patternExtension);
  Schema._clearFetchOverride();
});

afterEach(() => {
  Schema._clearCache();
  Schema._clearExtensions();
  Schema._clearFetchOverride();
});


// ── @append_only_log → .append(payload) ──────────────────────

describe('append_only_log → proxy.append', () => {
  it('attaches .append on schemas with access_pattern=append_only_log', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-decision%231':
        metaResponse(appendOnlyPayload()),
    });
    Schema._setFetchOverride(stub);
    const proxy = await Schema.of('dashboard.coordinator-decision');
    assert.equal(typeof proxy.append, 'function');
    assert.equal(typeof proxy.set, 'undefined');
    assert.equal(typeof proxy.upsert, 'undefined');
  });

  it('routes through proxy.write with a generated UUID key', async () => {
    let captured = null;
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-decision%231':
        metaResponse(appendOnlyPayload()),
      '/api/graph/setting': (path, opts) => {
        captured = JSON.parse(opts.body);
        return okResponse({ ok: true });
      },
    });
    Schema._setFetchOverride(stub);
    const proxy = await Schema.of('dashboard.coordinator-decision');
    await proxy.append({ tile_id: 'auto-foo', sentAt: '2026-05-01T00:00:00Z' });

    assert.equal(captured.set_id, 'dashboard.coordinator-decision');
    assert.equal(captured.schema_revision, 1);
    assert.deepEqual(captured.payload, {
      tile_id: 'auto-foo',
      sentAt: '2026-05-01T00:00:00Z',
    });
    // RFC4122 v4 UUID: 8-4-4-4-12 hex digits, version 4 in bits 12-15
    // of clock_seq_hi, variant 10 in bits 6-7 of clock_seq_low.
    assert.match(captured.key,
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  });

  it('generates distinct keys across calls', async () => {
    const keys = [];
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-decision%231':
        metaResponse(appendOnlyPayload()),
      '/api/graph/setting': (path, opts) => {
        keys.push(JSON.parse(opts.body).key);
        return okResponse({ ok: true });
      },
    });
    Schema._setFetchOverride(stub);
    const proxy = await Schema.of('dashboard.coordinator-decision');
    await proxy.append({ tile_id: 'a' });
    await proxy.append({ tile_id: 'b' });
    await proxy.append({ tile_id: 'c' });

    assert.equal(keys.length, 3);
    assert.equal(new Set(keys).size, 3);  // all distinct
  });
});


// ── @singleton → .set(payload) ───────────────────────────────

describe('singleton → proxy.set', () => {
  it('attaches .set on schemas with access_pattern=singleton', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.operator-message-to-coordinator%231':
        metaResponse(singletonPayload()),
    });
    Schema._setFetchOverride(stub);
    const proxy = await Schema.of('dashboard.operator-message-to-coordinator');
    assert.equal(typeof proxy.set, 'function');
    assert.equal(typeof proxy.append, 'undefined');
    assert.equal(typeof proxy.upsert, 'undefined');
  });

  it('uses the fixed key from key_strategy "fixed:default"', async () => {
    let captured = null;
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.operator-message-to-coordinator%231':
        metaResponse(singletonPayload('fixed:default')),
      '/api/graph/setting': (path, opts) => {
        captured = JSON.parse(opts.body);
        return okResponse({ ok: true });
      },
    });
    Schema._setFetchOverride(stub);
    const proxy = await Schema.of('dashboard.operator-message-to-coordinator');
    await proxy.set({ text: 'hi' });

    assert.equal(captured.key, 'default');
    assert.deepEqual(captured.payload, { text: 'hi' });
  });

  it('uses the fixed key from key_strategy "fixed:canonical"', async () => {
    let captured = null;
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.operator-message-to-coordinator%231':
        metaResponse(singletonPayload('fixed:canonical')),
      '/api/graph/setting': (path, opts) => {
        captured = JSON.parse(opts.body);
        return okResponse({ ok: true });
      },
    });
    Schema._setFetchOverride(stub);
    const proxy = await Schema.of('dashboard.operator-message-to-coordinator');
    await proxy.set({ text: 'hi' });

    assert.equal(captured.key, 'canonical');
  });

  it('falls back to "default" on malformed key_strategy with a warning', async () => {
    const origWarn = console.warn;
    const warnings = [];
    console.warn = (...args) => warnings.push(args.join(' '));
    try {
      const stub = makeFetchStub({
        '/api/graph/settings/autonomy.schema/dashboard.operator-message-to-coordinator%231':
          metaResponse(singletonPayload('not-prefixed')),
        '/api/graph/setting': okResponse({ ok: true }),
      });
      Schema._setFetchOverride(stub);
      const proxy = await Schema.of('dashboard.operator-message-to-coordinator');
      assert.equal(typeof proxy.set, 'function');
      assert.ok(warnings.some(w => w.includes('singleton key_strategy')),
        'expected a warning about singleton key_strategy');
    } finally {
      console.warn = origWarn;
    }
  });
});


// ── @keyed_per_entity → .upsert(key, payload) ────────────────

describe('keyed_per_entity → proxy.upsert', () => {
  it('attaches .upsert on schemas with access_pattern=keyed_per_entity', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-tile%231':
        metaResponse(keyedPerEntityPayload()),
    });
    Schema._setFetchOverride(stub);
    const proxy = await Schema.of('dashboard.coordinator-tile');
    assert.equal(typeof proxy.upsert, 'function');
    assert.equal(typeof proxy.append, 'undefined');
    assert.equal(typeof proxy.set, 'undefined');
  });

  it('routes (key, payload) through proxy.write', async () => {
    let captured = null;
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-tile%231':
        metaResponse(keyedPerEntityPayload()),
      '/api/graph/setting': (path, opts) => {
        captured = JSON.parse(opts.body);
        return okResponse({ ok: true });
      },
    });
    Schema._setFetchOverride(stub);
    const proxy = await Schema.of('dashboard.coordinator-tile');
    await proxy.upsert('auto-foo', { label: 'My Tile' });

    assert.equal(captured.set_id, 'dashboard.coordinator-tile');
    assert.equal(captured.key, 'auto-foo');
    assert.deepEqual(captured.payload, { label: 'My Tile' });
  });

  it('rejects when key is missing or non-string', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-tile%231':
        metaResponse(keyedPerEntityPayload()),
    });
    Schema._setFetchOverride(stub);
    const proxy = await Schema.of('dashboard.coordinator-tile');
    assert.throws(() => proxy.upsert('', { label: 'x' }), /requires a string key/);
    assert.throws(() => proxy.upsert(null, { label: 'x' }), /requires a string key/);
  });
});


// ── Undecorated schema gets nothing ──────────────────────────

describe('undecorated schema', () => {
  it('does not gain pattern-aware methods', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/plain.example%231':
        metaResponse(undecoratedPayload()),
    });
    Schema._setFetchOverride(stub);
    const proxy = await Schema.of('plain.example');

    assert.equal(typeof proxy.append, 'undefined');
    assert.equal(typeof proxy.set, 'undefined');
    assert.equal(typeof proxy.upsert, 'undefined');
    // Generic surface is still present.
    assert.equal(typeof proxy.read, 'function');
    assert.equal(typeof proxy.all, 'function');
    assert.equal(typeof proxy.write, 'function');
  });
});


// ── Unknown access_pattern is logged but doesn't crash ───────

describe('unknown access_pattern', () => {
  it('does not attach any convenience method and warns', async () => {
    const origWarn = console.warn;
    const warnings = [];
    console.warn = (...args) => warnings.push(args.join(' '));
    try {
      const stub = makeFetchStub({
        '/api/graph/settings/autonomy.schema/future.pattern%231':
          metaResponse(unknownPatternPayload()),
      });
      Schema._setFetchOverride(stub);
      const proxy = await Schema.of('future.pattern');

      assert.equal(typeof proxy.append, 'undefined');
      assert.equal(typeof proxy.set, 'undefined');
      assert.equal(typeof proxy.upsert, 'undefined');
      // Generic write still works.
      assert.equal(typeof proxy.write, 'function');
      assert.ok(warnings.some(w => w.includes('unknown access_pattern')),
        'expected a warning about unknown access_pattern');
    } finally {
      console.warn = origWarn;
    }
  });
});


// ── Variant-bearing schema with @append_only_log ─────────────

describe('variant-bearing schema with access_pattern=append_only_log', () => {
  it('still attaches .append (variants do not suppress pattern methods)', async () => {
    const variant = appendOnlyPayload();
    variant.variants = {
      thumb_yes: {
        type: 'object',
        properties: { tile_id: { type: 'string' } },
        required: ['tile_id'],
        variants: {},
      },
    };
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-decision%231':
        metaResponse(variant),
    });
    Schema._setFetchOverride(stub);
    const proxy = await Schema.of('dashboard.coordinator-decision');

    assert.equal(typeof proxy.append, 'function');
    // Per-variant methods (.thumb_yes / etc.) are 2C's territory.
    assert.equal(typeof proxy.thumb_yes, 'undefined');
  });
});


// ── Pattern extension is registered by default ───────────────

describe('default registration', () => {
  it('Schema._patternExtension is exposed for re-registration', () => {
    assert.equal(typeof Schema._patternExtension, 'function');
  });

  it('after _clearExtensions, schemas gain no pattern methods', async () => {
    Schema._clearExtensions();  // intentional: drop the auto-registered extension
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-decision%231':
        metaResponse(appendOnlyPayload()),
    });
    Schema._setFetchOverride(stub);
    const proxy = await Schema.of('dashboard.coordinator-decision');
    assert.equal(typeof proxy.append, 'undefined');
  });
});
