// Tests for the substrate.B Surface Presence JS helper at
// ``tools/dashboard/static/js/surface-presence.js``.
//
// Exercises:
//   * ``Presence.participantColor`` — deterministic HSL hash, parity
//     with ``tools/graph/surface.py::Presence.participant_color``.
//   * ``OperatorActivity.isIdle`` / ``lastUserInput`` / ``activeWithin``
//     / ``inputsLastHour`` — substrate.D stubs (singleton row, no id).
//   * ``Presence.alpine`` — wrapping shape, init() pre-fetch +
//     proxy attachment, pingAgent + acknowledgePing wire-format.
//
// Bead: substrate.B (auto-7t98r). Signpost: graph://dff97eec-c59.

const { describe, it, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const Schema = require('../static/js/schemas.js');
const Presence = require('../static/js/surface-presence.js');
const { OperatorActivity } = Presence;


// ── Test fixtures ──────────────────────────────────────────────

const SURFACE_PRESENCE_KEY = 'autonomy.schema/dashboard.surface.presence%231';
const SURFACE_PING_KEY = 'autonomy.schema/dashboard.surface.ping%231';

function presenceMetaPayload() {
  return {
    set_id: 'dashboard.surface.presence',
    schema_revision: 1,
    type: 'object',
    properties: {
      surface_id: { type: 'string' },
      participant_kind: { type: 'string' },
      participant_id: { type: 'string' },
      participant_label: { type: 'string' },
      accepts_pings: { type: 'boolean' },
      state: { type: 'string' },
      position_kind: { type: 'string' },
      position_value: { type: 'string' },
      intent: { type: 'string' },
      heartbeat_at: { type: 'string' },
      last_ping_id: { type: 'string' },
    },
    required: [
      'surface_id', 'participant_kind', 'participant_id',
      'participant_label', 'state', 'heartbeat_at',
    ],
    access_pattern: 'keyed_per_entity',
    key_strategy: null,
    variants: {},
  };
}

function pingMetaPayload() {
  return {
    set_id: 'dashboard.surface.ping',
    schema_revision: 1,
    type: 'object',
    properties: {
      surface_id: { type: 'string' },
      from_participant_id: { type: 'string' },
      to_participant_id: { type: 'string' },
      position_kind: { type: 'string' },
      position_value: { type: 'string' },
      message: { type: 'string' },
      sent_at: { type: 'string' },
    },
    required: [
      'surface_id', 'from_participant_id', 'to_participant_id',
      'position_kind', 'position_value', 'sent_at',
    ],
    access_pattern: 'append_only_log',
    key_strategy: 'uuid_v4',
    variants: {},
  };
}

function metaResponse(payload) {
  return { ok: true, status: 200, json: async () => ({ payload: payload }) };
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

function defaultSchemaRoutes(extras) {
  const routes = {
    ['/api/graph/settings/' + SURFACE_PRESENCE_KEY]: metaResponse(presenceMetaPayload()),
    ['/api/graph/settings/' + SURFACE_PING_KEY]: metaResponse(pingMetaPayload()),
  };
  if (extras) Object.assign(routes, extras);
  return routes;
}


beforeEach(() => {
  Schema._clearCache();
  Schema._clearExtensions();
  // Re-register the pattern extension so .upsert / .append exist on
  // the proxies we build during tests (mirrors what schemas.js wires
  // up at module load in the browser).
  Schema._registerExtension(Schema._patternExtension);
  Schema._registerExtension(Schema._variantExtension);
  Schema._clearFetchOverride();
  globalThis.Schema = Schema;
});

afterEach(() => {
  Schema._clearCache();
  Schema._clearExtensions();
  Schema._clearFetchOverride();
  delete globalThis.Schema;
});


// ── participantColor ──────────────────────────────────────────

describe('Presence.participantColor', () => {
  it('returns a stable HSL string for a given id', () => {
    const a = Presence.participantColor('ea2cef72-ed4');
    const b = Presence.participantColor('ea2cef72-ed4');
    assert.equal(a, b);
    // Format is ``hsl(<deg> 70% 60%)`` per the Python helper.
    assert.match(a, /^hsl\(\d{1,3} 70% 60%\)$/);
  });

  it('matches the Python reference values from tools/graph/surface.py', () => {
    // Mirror parity check: same id should produce the same HSL hue
    // on the Python and JS sides so the displayed color is identical
    // across operator (Python tools) and viewer (JS dashboard) sees.
    assert.equal(Presence.participantColor('ea2cef72-ed4'), 'hsl(55 70% 60%)');
    assert.equal(Presence.participantColor('jeremy'),       'hsl(262 70% 60%)');
    assert.equal(Presence.participantColor('auto-7t98r'),   'hsl(96 70% 60%)');
    assert.equal(Presence.participantColor('a'),            'hsl(97 70% 60%)');
    assert.equal(Presence.participantColor(''),             'hsl(0 70% 60%)');
  });

  it('coerces non-string ids to strings without throwing', () => {
    assert.match(Presence.participantColor(12345), /^hsl\(\d{1,3} 70% 60%\)$/);
    assert.equal(Presence.participantColor(null),      'hsl(0 70% 60%)');
    assert.equal(Presence.participantColor(undefined), 'hsl(0 70% 60%)');
  });
});


// ── Static stubs (substrate.D will make them real) ────────────

describe('OperatorActivity.isIdle / lastUserInput / activeWithin / inputsLastHour stubs', () => {
  it('isIdle returns false (consumers default to "not idle" until D ships)', async () => {
    assert.equal(await OperatorActivity.isIdle({ minutes: 30 }), false);
    assert.equal(await OperatorActivity.isIdle(), false);
  });

  it('lastUserInput returns null', async () => {
    assert.equal(await OperatorActivity.lastUserInput(), null);
  });

  it('activeWithin returns true (consumers default to "active" until D ships)', async () => {
    assert.equal(await OperatorActivity.activeWithin({ hours: 1 }), true);
  });

  it('inputsLastHour returns 0', async () => {
    assert.equal(await OperatorActivity.inputsLastHour(), 0);
  });
});


// ── Presence.alpine — wrapping shape (no init) ────────────────

describe('Presence.alpine — wrapping shape', () => {
  it('returns a state with init/destroy wired and presence fields seeded', () => {
    const state = Presence.alpine(
      { surfaceId: 'test-surface' },
      { tab: 'primary' },
    );

    // User-supplied state preserved.
    assert.equal(state.tab, 'primary');
    // Bead-specified surface API.
    assert.equal(typeof state.init, 'function');
    assert.equal(typeof state.destroy, 'function');
    assert.deepEqual(state.participants, []);
    assert.equal(state.amHere, false);
    assert.equal(typeof state.pingAgent, 'function');
    assert.equal(typeof state.acknowledgePing, 'function');
    assert.equal(typeof state.setPresenceState, 'function');
  });

  it('preserves user-defined fields and methods on the state', () => {
    function userMethod() { return 'user-result'; }
    const state = Presence.alpine(
      { surfaceId: 'test' },
      { counter: 7, doThing: userMethod },
    );
    assert.equal(state.counter, 7);
    assert.equal(state.doThing(), 'user-result');
  });

  it('throws on missing opts.surfaceId', () => {
    assert.throws(
      () => Presence.alpine({}, {}),
      /surfaceId is required/,
    );
    assert.throws(
      () => Presence.alpine({ surfaceId: '' }, {}),
      /surfaceId is required/,
    );
  });

  it('throws on non-object opts or state', () => {
    assert.throws(
      () => Presence.alpine(null, {}),
      /requires an opts object/,
    );
    assert.throws(
      () => Presence.alpine({ surfaceId: 'x' }, null),
      /requires a state object/,
    );
  });
});


// ── Presence.alpine — init() pre-fetch + load ────────────────

describe('Presence.alpine — init() lifecycle', () => {
  it('attaches presence + ping schema proxies and loads participants on init', async () => {
    const aliceRow = {
      key: 'test-surface:alice',
      payload: {
        surface_id: 'test-surface',
        participant_kind: 'agent',
        participant_id: 'alice',
        participant_label: 'Alice',
        state: 'present',
        position_kind: 'none',
        position_value: '',
        intent: '',
        heartbeat_at: '2026-05-02T22:00:00Z',
        accepts_pings: true,
        last_ping_id: '',
      },
    };
    const otherSurfaceRow = {
      key: 'other:bob',
      payload: { surface_id: 'other', participant_id: 'bob', participant_kind: 'agent',
                 participant_label: 'Bob', state: 'present', heartbeat_at: '2026-05-02T22:00:00Z' },
    };

    const stub = makeFetchStub(defaultSchemaRoutes({
      '/api/graph/settings/dashboard.surface.presence': {
        ok: true, status: 200,
        json: async () => ({ members: [aliceRow, otherSurfaceRow] }),
      },
    }));
    Schema._setFetchOverride(stub);

    const state = Presence.alpine(
      { surfaceId: 'test-surface' },
      {},
    );
    await state.init();

    // Filtered to the surface.
    assert.equal(state.participants.length, 1);
    assert.equal(state.participants[0].participant_id, 'alice');
    // No operator id supplied → no row write, amHere stays false.
    assert.equal(state.amHere, false);
  });

  it('writes the operator presence row when participantId is supplied', async () => {
    let captured = null;
    const stub = makeFetchStub(defaultSchemaRoutes({
      '/api/graph/settings/dashboard.surface.presence': {
        ok: true, status: 200, json: async () => ({ members: [] }),
      },
      '/api/graph/setting': (path, opts) => {
        captured = { path: path, opts: opts };
        return { ok: true, status: 200, json: async () => ({ ok: true }) };
      },
    }));
    Schema._setFetchOverride(stub);

    const state = Presence.alpine(
      {
        surfaceId: 'test-surface',
        participantId: 'jeremy',
        participantLabel: 'Jeremy',
        participantKind: 'operator',
        heartbeatMs: 0,  // disable interval — keeps the test deterministic
      },
      {},
    );
    await state.init();

    assert.equal(state.amHere, true);
    assert.ok(captured, 'expected a write to /api/graph/setting');
    const body = JSON.parse(captured.opts.body);
    assert.equal(body.set_id, 'dashboard.surface.presence');
    assert.equal(body.schema_revision, 1);
    assert.equal(body.key, 'test-surface:jeremy');
    assert.equal(body.payload.surface_id, 'test-surface');
    assert.equal(body.payload.participant_id, 'jeremy');
    assert.equal(body.payload.participant_kind, 'operator');
    assert.equal(body.payload.participant_label, 'Jeremy');
    assert.equal(body.payload.state, 'present');
    assert.equal(body.payload.accepts_pings, true);
    assert.match(body.payload.heartbeat_at,
                 /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
  });

  it('runs the user-supplied init() AFTER schema attachment', async () => {
    const events = [];
    const stub = makeFetchStub(defaultSchemaRoutes({
      '/api/graph/settings/dashboard.surface.presence': {
        ok: true, status: 200, json: async () => ({ members: [] }),
      },
    }));
    Schema._setFetchOverride(stub);

    const state = Presence.alpine(
      { surfaceId: 'test-surface' },
      {
        async init() {
          // By the time we arrive here, the proxies are attached and
          // participants array is hydrated.
          events.push('user-init participants=' + this.participants.length);
          events.push('proxy-attached=' + (this._presencePresenceProxy != null));
        },
      },
    );
    await state.init();
    assert.deepEqual(events, [
      'user-init participants=0',
      'proxy-attached=true',
    ]);
  });

  it('init() degrades gracefully when the Schema runtime is missing', async () => {
    const origSchema = globalThis.Schema;
    delete globalThis.Schema;
    const origWarn = console.warn;
    console.warn = () => {};
    try {
      const state = Presence.alpine({ surfaceId: 'test' }, {});
      await state.init();
      // No throw, no proxies attached, amHere stays false.
      assert.equal(state.amHere, false);
      assert.equal(state._presencePresenceProxy, null);
    } finally {
      console.warn = origWarn;
      globalThis.Schema = origSchema;
    }
  });
});


// ── pingAgent + acknowledgePing wire format ───────────────────

describe('Presence.alpine — pingAgent', () => {
  it('POSTs a SurfacePing row with explicit to_participant_id', async () => {
    const writes = [];
    const stub = makeFetchStub(defaultSchemaRoutes({
      '/api/graph/settings/dashboard.surface.presence': {
        ok: true, status: 200, json: async () => ({ members: [] }),
      },
      '/api/graph/setting': (path, opts) => {
        writes.push(JSON.parse(opts.body));
        return { ok: true, status: 200, json: async () => ({ ok: true }) };
      },
    }));
    Schema._setFetchOverride(stub);

    const state = Presence.alpine(
      { surfaceId: 'test-surface', participantId: 'jeremy', heartbeatMs: 0 },
      {},
    );
    await state.init();

    // First write was the operator's presence row; reset and ping.
    writes.length = 0;
    await state.pingAgent('alice', { kind: 'tile', value: 't-42' }, 'wake');

    assert.equal(writes.length, 1);
    const body = writes[0];
    assert.equal(body.set_id, 'dashboard.surface.ping');
    assert.equal(body.schema_revision, 1);
    // append_only_log → key is a UUID, not the to_participant_id —
    // the targeting goes in the payload, not the key.
    assert.match(body.key, /[0-9a-f-]{36}/);
    assert.equal(body.payload.surface_id, 'test-surface');
    assert.equal(body.payload.from_participant_id, 'jeremy');
    assert.equal(body.payload.to_participant_id, 'alice');
    assert.equal(body.payload.position_kind, 'tile');
    assert.equal(body.payload.position_value, 't-42');
    assert.equal(body.payload.message, 'wake');
    assert.match(body.payload.sent_at,
                 /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
  });

  it('throws without a target id', async () => {
    const stub = makeFetchStub(defaultSchemaRoutes({
      '/api/graph/settings/dashboard.surface.presence': {
        ok: true, status: 200, json: async () => ({ members: [] }),
      },
      '/api/graph/setting': { ok: true, status: 200, json: async () => ({ ok: true }) },
    }));
    Schema._setFetchOverride(stub);

    const state = Presence.alpine(
      { surfaceId: 'test', participantId: 'jeremy', heartbeatMs: 0 },
      {},
    );
    await state.init();

    await assert.rejects(() => state.pingAgent(''), /targetId is required/);
    await assert.rejects(() => state.pingAgent(null), /targetId is required/);
  });

  it('throws when called before init() (no proxy attached)', async () => {
    const state = Presence.alpine(
      { surfaceId: 'test', participantId: 'jeremy' },
      {},
    );
    await assert.rejects(
      () => state.pingAgent('alice', { kind: 'tile', value: 't' }),
      /ping proxy not attached/,
    );
  });

  it('throws when no operator participantId is configured', async () => {
    const stub = makeFetchStub(defaultSchemaRoutes({
      '/api/graph/settings/dashboard.surface.presence': {
        ok: true, status: 200, json: async () => ({ members: [] }),
      },
    }));
    Schema._setFetchOverride(stub);

    const state = Presence.alpine({ surfaceId: 'test' }, {});
    await state.init();
    await assert.rejects(
      () => state.pingAgent('alice', { kind: 'tile', value: 't' }),
      /cannot ping without an operator participant id/,
    );
  });
});


describe('Presence.alpine — acknowledgePing', () => {
  it('writes a presence row with last_ping_id set', async () => {
    const writes = [];
    const stub = makeFetchStub(defaultSchemaRoutes({
      '/api/graph/settings/dashboard.surface.presence': {
        ok: true, status: 200, json: async () => ({ members: [] }),
      },
      '/api/graph/setting': (path, opts) => {
        writes.push(JSON.parse(opts.body));
        return { ok: true, status: 200, json: async () => ({ ok: true }) };
      },
    }));
    Schema._setFetchOverride(stub);

    const state = Presence.alpine(
      { surfaceId: 'test-surface', participantId: 'jeremy', heartbeatMs: 0 },
      {},
    );
    await state.init();
    writes.length = 0;

    await state.acknowledgePing('ping-uuid-123');
    assert.equal(writes.length, 1);
    assert.equal(writes[0].set_id, 'dashboard.surface.presence');
    assert.equal(writes[0].key, 'test-surface:jeremy');
    assert.equal(writes[0].payload.last_ping_id, 'ping-uuid-123');
  });

  it('is a no-op for falsy ids', async () => {
    const writes = [];
    const stub = makeFetchStub(defaultSchemaRoutes({
      '/api/graph/settings/dashboard.surface.presence': {
        ok: true, status: 200, json: async () => ({ members: [] }),
      },
      '/api/graph/setting': (path, opts) => {
        writes.push(JSON.parse(opts.body));
        return { ok: true, status: 200, json: async () => ({ ok: true }) };
      },
    }));
    Schema._setFetchOverride(stub);

    const state = Presence.alpine(
      { surfaceId: 'test', participantId: 'jeremy', heartbeatMs: 0 },
      {},
    );
    await state.init();
    writes.length = 0;

    await state.acknowledgePing('');
    await state.acknowledgePing(null);
    assert.equal(writes.length, 0);
  });
});


// ── setPresenceState ─────────────────────────────────────────

describe('Presence.alpine — setPresenceState', () => {
  it('updates state/position/intent and writes the row', async () => {
    const writes = [];
    const stub = makeFetchStub(defaultSchemaRoutes({
      '/api/graph/settings/dashboard.surface.presence': {
        ok: true, status: 200, json: async () => ({ members: [] }),
      },
      '/api/graph/setting': (path, opts) => {
        writes.push(JSON.parse(opts.body));
        return { ok: true, status: 200, json: async () => ({ ok: true }) };
      },
    }));
    Schema._setFetchOverride(stub);

    const state = Presence.alpine(
      { surfaceId: 'test-surface', participantId: 'jeremy', heartbeatMs: 0 },
      {},
    );
    await state.init();
    writes.length = 0;

    await state.setPresenceState('working', {
      kind: 'tile', value: 'redesign-thinking',
      intent: 'reading reply',
    });

    assert.equal(writes.length, 1);
    assert.equal(writes[0].payload.state, 'working');
    assert.equal(writes[0].payload.position_kind, 'tile');
    assert.equal(writes[0].payload.position_value, 'redesign-thinking');
    assert.equal(writes[0].payload.intent, 'reading reply');
  });
});


// ── destroy() composition ────────────────────────────────────

describe('Presence.alpine — destroy()', () => {
  it('clears the heartbeat interval and unsubscribes from changes', async () => {
    const stub = makeFetchStub(defaultSchemaRoutes({
      '/api/graph/settings/dashboard.surface.presence': {
        ok: true, status: 200, json: async () => ({ members: [] }),
      },
      '/api/graph/setting': { ok: true, status: 200, json: async () => ({ ok: true }) },
    }));
    Schema._setFetchOverride(stub);

    let unsubCalled = false;
    // Inject a fake onChange that returns a tracking unsub. The proxy
    // is built fresh each test, so monkey-patching here is safe.
    const origRegister = Schema._registerExtension;
    Schema._clearExtensions();
    Schema._registerExtension(Schema._patternExtension);
    Schema._registerExtension(function(proxy) {
      proxy.onChange = function() { return function() { unsubCalled = true; }; };
    });

    const state = Presence.alpine(
      { surfaceId: 'test', participantId: 'jeremy', heartbeatMs: 50 },
      {},
    );
    await state.init();
    assert.notEqual(state._presenceHeartbeat, null);

    state.destroy();
    assert.equal(state._presenceHeartbeat, null);
    assert.equal(unsubCalled, true);
  });

  it('runs the user-supplied destroy() after teardown', async () => {
    const events = [];
    const state = Presence.alpine(
      { surfaceId: 'test' },
      { destroy() { events.push('user-destroy'); } },
    );
    state.destroy();
    assert.deepEqual(events, ['user-destroy']);
  });
});
