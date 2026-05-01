// Tests for the variant-aware Schema proxy methods (bead auto-2C).
//
// 2C walks the recursive ``payload.variants`` tree (emitted by 1D
// from the meta-Setting) and attaches per-variant convenience methods
// to the proxy. Each leaf variant becomes a callable that auto-injects
// ``{kind: <variant slug>}`` and routes through the pattern method
// that 2B attached (``.append`` / ``.set`` / ``.upsert``), or the
// generic ``.write`` when no pattern applies.
//
// Nested namespaces (``SourceControl.review.read``) become sub-objects
// with recursive variant methods. The leaf slug is what stamps ``kind``;
// intermediate nodes are not themselves callable.

const { describe, it, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const Schema = require('../static/js/schemas.js');


// ── Test fixtures ──────────────────────────────────────────────────

function appendOnlyVariantPayload() {
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
    variants: {
      thumb_yes: {
        type: 'object',
        properties: { tile_id: { type: 'string' } },
        required: ['tile_id'],
        variants: {},
      },
      thumb_no: {
        type: 'object',
        properties: { tile_id: { type: 'string' } },
        required: ['tile_id'],
        variants: {},
      },
      choice: {
        type: 'object',
        properties: {
          tile_id: { type: 'string' },
          choice: { type: 'string' },
        },
        required: ['tile_id', 'choice'],
        variants: {},
      },
    },
  };
}

function singletonVariantPayload() {
  return {
    set_id: 'x.singleton-with-variants',
    schema_revision: 1,
    type: 'object',
    properties: {},
    required: [],
    access_pattern: 'singleton',
    key_strategy: 'fixed:default',
    variants: {
      a: { type: 'object', properties: {}, required: [], variants: {} },
      b: { type: 'object', properties: {}, required: [], variants: {} },
    },
  };
}

function keyedVariantPayload() {
  return {
    set_id: 'x.keyed-with-variants',
    schema_revision: 1,
    type: 'object',
    properties: {},
    required: [],
    access_pattern: 'keyed_per_entity',
    key_strategy: 'natural',
    variants: {
      first: { type: 'object', properties: {}, required: [], variants: {} },
      second: { type: 'object', properties: {}, required: [], variants: {} },
    },
  };
}

function nestedNamespacePayload() {
  return {
    set_id: 'autonomy.capability-contract',
    schema_revision: 1,
    type: 'object',
    properties: {},
    required: [],
    access_pattern: null,
    key_strategy: null,
    variants: {
      branch_status: {
        type: 'object', properties: {}, required: [], variants: {},
      },
      review: {
        type: 'object', properties: {}, required: [],
        variants: {
          review_read: {
            type: 'object',
            properties: { branch: { type: 'string' } },
            required: ['branch'],
            variants: {},
          },
          review_refresh: {
            type: 'object', properties: {}, required: [], variants: {},
          },
        },
      },
      gates: {
        type: 'object', properties: {}, required: [],
        variants: {
          gates_snapshot: {
            type: 'object', properties: {}, required: [], variants: {},
          },
          gates_watch_set: {
            type: 'object', properties: {}, required: [], variants: {},
          },
        },
      },
    },
  };
}

function nonVariantPayload() {
  return {
    set_id: 'plain.example',
    schema_revision: 1,
    type: 'object',
    properties: { name: { type: 'string' } },
    required: ['name'],
    access_pattern: 'singleton',
    key_strategy: 'fixed:default',
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
  // 2C variant methods route through 2B pattern methods — register both.
  Schema._registerExtension(Schema._patternExtension);
  Schema._registerExtension(Schema._variantExtension);
  Schema._clearFetchOverride();
});

afterEach(() => {
  Schema._clearCache();
  Schema._clearExtensions();
  Schema._clearFetchOverride();
});


// ── append_only_log + variants → variantName(payload) ────────

describe('variant methods on append_only_log schema', () => {
  it('attaches each variant slug as a method on the proxy', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-decision%231':
        metaResponse(appendOnlyVariantPayload()),
    });
    Schema._setFetchOverride(stub);

    const proxy = await Schema.of('dashboard.coordinator-decision');
    assert.equal(typeof proxy.thumb_yes, 'function');
    assert.equal(typeof proxy.thumb_no, 'function');
    assert.equal(typeof proxy.choice, 'function');
    // 2B pattern method still present alongside.
    assert.equal(typeof proxy.append, 'function');
  });

  it('routes through .append (UUID key) and auto-injects kind', async () => {
    let captured = null;
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-decision%231':
        metaResponse(appendOnlyVariantPayload()),
      '/api/graph/setting': (path, opts) => {
        captured = JSON.parse(opts.body);
        return okResponse({ ok: true });
      },
    });
    Schema._setFetchOverride(stub);

    const proxy = await Schema.of('dashboard.coordinator-decision');
    await proxy.choice({ tile_id: 'auto-foo', choice: 'Ship it' });

    assert.equal(captured.set_id, 'dashboard.coordinator-decision');
    assert.equal(captured.payload.kind, 'choice');
    assert.equal(captured.payload.tile_id, 'auto-foo');
    assert.equal(captured.payload.choice, 'Ship it');
    // UUID key from append_only_log + uuid_v4.
    assert.match(captured.key,
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  });

  it('user-supplied kind is overridden by variant slug', async () => {
    let captured = null;
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-decision%231':
        metaResponse(appendOnlyVariantPayload()),
      '/api/graph/setting': (path, opts) => {
        captured = JSON.parse(opts.body);
        return okResponse({ ok: true });
      },
    });
    Schema._setFetchOverride(stub);

    const proxy = await Schema.of('dashboard.coordinator-decision');
    await proxy.thumb_yes({ tile_id: 'auto-foo', kind: 'something_else' });

    assert.equal(captured.payload.kind, 'thumb_yes');  // slug wins
  });
});


// ── singleton + variants → variantName(payload) → fixed key ──

describe('variant methods on singleton schema', () => {
  it('attaches variant methods that route through .set with the fixed key', async () => {
    let captured = null;
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/x.singleton-with-variants%231':
        metaResponse(singletonVariantPayload()),
      '/api/graph/setting': (path, opts) => {
        captured = JSON.parse(opts.body);
        return okResponse({ ok: true });
      },
    });
    Schema._setFetchOverride(stub);

    const proxy = await Schema.of('x.singleton-with-variants');
    assert.equal(typeof proxy.a, 'function');
    assert.equal(typeof proxy.b, 'function');

    await proxy.a({ field: 'value' });
    assert.equal(captured.key, 'default');
    assert.equal(captured.payload.kind, 'a');
    assert.equal(captured.payload.field, 'value');
  });
});


// ── keyed_per_entity + variants → variantName(key, payload) ──

describe('variant methods on keyed_per_entity schema', () => {
  it('takes (key, payload), auto-injects kind, routes through .upsert', async () => {
    let captured = null;
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/x.keyed-with-variants%231':
        metaResponse(keyedVariantPayload()),
      '/api/graph/setting': (path, opts) => {
        captured = JSON.parse(opts.body);
        return okResponse({ ok: true });
      },
    });
    Schema._setFetchOverride(stub);

    const proxy = await Schema.of('x.keyed-with-variants');
    assert.equal(typeof proxy.first, 'function');
    assert.equal(typeof proxy.second, 'function');

    await proxy.first('my-key', { field: 'value' });
    assert.equal(captured.key, 'my-key');
    assert.equal(captured.payload.kind, 'first');
    assert.equal(captured.payload.field, 'value');
  });
});


// ── Nested namespaces ────────────────────────────────────────

describe('nested namespaces (capability_contract pattern)', () => {
  it('intermediate variants become sub-objects (not callable)', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/autonomy.capability-contract%231':
        metaResponse(nestedNamespacePayload()),
    });
    Schema._setFetchOverride(stub);

    const proxy = await Schema.of('autonomy.capability-contract');

    // Top-level: branch_status is a leaf (callable); review and gates
    // are sub-namespaces (objects).
    assert.equal(typeof proxy.branch_status, 'function');
    assert.equal(typeof proxy.review, 'object');
    assert.equal(typeof proxy.gates, 'object');

    // review is not directly callable.
    assert.notEqual(typeof proxy.review, 'function');
  });

  it('exposes leaf variants as methods on the sub-namespace', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/autonomy.capability-contract%231':
        metaResponse(nestedNamespacePayload()),
    });
    Schema._setFetchOverride(stub);

    const proxy = await Schema.of('autonomy.capability-contract');
    assert.equal(typeof proxy.review.review_read, 'function');
    assert.equal(typeof proxy.review.review_refresh, 'function');
    assert.equal(typeof proxy.gates.gates_snapshot, 'function');
    assert.equal(typeof proxy.gates.gates_watch_set, 'function');
  });

  it('leaf method stamps kind = leaf slug (not the full path)', async () => {
    let captured = null;
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/autonomy.capability-contract%231':
        metaResponse(nestedNamespacePayload()),
      '/api/graph/setting': (path, opts) => {
        captured = JSON.parse(opts.body);
        return okResponse({ ok: true });
      },
    });
    Schema._setFetchOverride(stub);

    // No access pattern on this contract — leaf methods take (key, payload).
    const proxy = await Schema.of('autonomy.capability-contract');
    await proxy.review.review_read('contract-key', { branch: 'main' });

    assert.equal(captured.key, 'contract-key');
    assert.equal(captured.payload.kind, 'review_read');
    assert.equal(captured.payload.branch, 'main');
  });
});


// ── Non-variant schemas ──────────────────────────────────────

describe('non-variant schema', () => {
  it('gains no per-variant methods', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/plain.example%231':
        metaResponse(nonVariantPayload()),
    });
    Schema._setFetchOverride(stub);
    const proxy = await Schema.of('plain.example');

    // Only the fields named in the payload should not be variant-methods.
    // We can't test "no variants" directly without enumerating, so verify
    // that no obvious variant slugs were attached and the generic /
    // pattern methods are intact.
    assert.equal(typeof proxy.read, 'function');
    assert.equal(typeof proxy.write, 'function');
    assert.equal(typeof proxy.set, 'function');  // singleton pattern method present
    // Sanity: no random slug-shaped property got attached.
    assert.equal(typeof proxy.thumb_yes, 'undefined');
    assert.equal(typeof proxy.review, 'undefined');
  });
});


// ── Pattern + variant interaction (2B+2C composed) ───────────

describe('2B and 2C compose cleanly', () => {
  it('proxy retains pattern methods alongside variant methods', async () => {
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-decision%231':
        metaResponse(appendOnlyVariantPayload()),
    });
    Schema._setFetchOverride(stub);
    const proxy = await Schema.of('dashboard.coordinator-decision');

    // Pattern method (.append) and variant methods (.thumb_yes etc.)
    // both present — they are NOT mutually exclusive.
    assert.equal(typeof proxy.append, 'function');
    assert.equal(typeof proxy.thumb_yes, 'function');
    assert.equal(typeof proxy.choice, 'function');
  });

  it('after _clearExtensions, neither layer attaches', async () => {
    Schema._clearExtensions();  // intentional: no patterns, no variants
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-decision%231':
        metaResponse(appendOnlyVariantPayload()),
    });
    Schema._setFetchOverride(stub);
    const proxy = await Schema.of('dashboard.coordinator-decision');
    assert.equal(typeof proxy.append, 'undefined');
    assert.equal(typeof proxy.thumb_yes, 'undefined');
  });

  it('exposed _variantExtension can be re-registered selectively', async () => {
    // Drop both, re-register variants only — pattern methods stay absent
    // but variant methods fall back to .write({key, payload}).
    Schema._clearExtensions();
    Schema._registerExtension(Schema._variantExtension);
    let captured = null;
    const stub = makeFetchStub({
      '/api/graph/settings/autonomy.schema/dashboard.coordinator-decision%231':
        metaResponse(appendOnlyVariantPayload()),
      '/api/graph/setting': (path, opts) => {
        captured = JSON.parse(opts.body);
        return okResponse({ ok: true });
      },
    });
    Schema._setFetchOverride(stub);
    const proxy = await Schema.of('dashboard.coordinator-decision');
    // No .append (pattern extension absent).
    assert.equal(typeof proxy.append, 'undefined');
    // Variant method present, falls back to .write — caller supplies key.
    assert.equal(typeof proxy.thumb_yes, 'function');
    await proxy.thumb_yes('my-key', { tile_id: 'foo' });
    assert.equal(captured.key, 'my-key');
    assert.equal(captured.payload.kind, 'thumb_yes');
  });
});
