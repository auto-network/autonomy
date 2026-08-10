/**
 * auto-wldnv: Diag client-side counter tests.
 *
 * Verifies the JS-side counters added for /api/diag/sessions:
 *   - dedup_collisions increments when a duplicate identity is seen
 *   - entries_via_sse_count vs entries_via_fetch_count provenance routing
 *   - entries_with_null_seq_count surfaces entries with no seq
 *   - _diagSnapshotSessions exposes the new tail_10 + counters
 *
 * The gap_replays_count counter lives in events.js. We seed
 * window._diagGapReplaysCount() so the snapshot picks it up; the
 * end-to-end behaviour is already covered by the gap-replay tests.
 *
 * Run: node --test tools/dashboard/tests/test_diag_client_counters.js
 */
const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const STORE_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/session-store.js');

function makeHarness() {
  const docListeners = {};
  const doc = {
    addEventListener(name, cb) {
      (docListeners[name] ||= []).push(cb);
    },
    visibilityState: 'visible',
    referrer: '',
  };

  const stores = {};
  const alpine = {
    store(name, obj) {
      if (obj !== undefined) { stores[name] = obj; return obj; }
      return stores[name];
    },
  };

  const fetchFn = () => Promise.resolve({ json: () => Promise.resolve([]) });

  const sandbox = {
    window: {},
    document: doc,
    Alpine: alpine,
    fetch: fetchFn,
    console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    Promise, JSON, Object, Array, Map, Set, Date, Error,
    parseInt, parseFloat,
  };
  sandbox.window.document = doc;
  sandbox.window.Alpine = alpine;
  sandbox.window.fetch = fetchFn;
  sandbox.window.registerHandler = function() {};
  sandbox.window.unregisterHandler = function() {};
  sandbox.registerHandler = sandbox.window.registerHandler;
  sandbox.unregisterHandler = sandbox.window.unregisterHandler;
  vm.createContext(sandbox);

  let storeSrc = fs.readFileSync(STORE_JS, 'utf8');
  storeSrc = storeSrc.replace(
    'setTimeout(ensureSessionMessages, 0);',
    'setTimeout(window.ensureSessionMessages, 0);'
  );
  vm.runInContext(storeSrc, sandbox, { filename: 'session-store.js' });
  for (const cb of (docListeners['alpine:init'] || [])) cb();

  return { win: sandbox.window, alpine };
}

describe('diag client-side counters (auto-wldnv)', () => {
  it('merge_dropped_duplicate increments when the same entry_ref is merged twice', () => {
    const h = makeHarness();
    const store = h.win.getSessionStore('sess-1');
    function entry() {
      return {
        type: 'tool_use', tool_id: 'call_X', tool_name: 'Read',
        timestamp: '2026-04-28T00:00:00Z',
        entry_ref: { file: 'f', off: 10, sub: 0 },
      };
    }
    h.win.mergeSessionEntries(store, { chain: ['f'], entries: [entry()] }, 'sse');
    assert.equal(store._counters.merge_dropped_duplicate, 0);

    // Re-deliver the exact same entry (gap-replay scenario).
    h.win.mergeSessionEntries(store, { chain: ['f'], entries: [entry()] }, 'sse');
    assert.equal(store._counters.merge_dropped_duplicate, 1, 'duplicate drop must be tracked');

    // And again.
    h.win.mergeSessionEntries(store, { chain: ['f'], entries: [entry()] }, 'sse');
    assert.equal(store._counters.merge_dropped_duplicate, 2);
    assert.equal(store.entries.length, 1, 'replays never grow the buffer');
  });

  it('entries_via_fetch_count vs entries_via_sse_count routes by provenance', () => {
    const h = makeHarness();
    const store = h.win.getSessionStore('sess-2');
    h.win.mergeSessionEntries(store, {
      seq: 1, entries: [
        { type: 'user', content: 'a', timestamp: '2026-04-28T00:00:00Z' },
      ],
    }, 'fetch');
    h.win.mergeSessionEntries(store, {
      seq: 2, entries: [
        { type: 'assistant_text', content: 'b', timestamp: '2026-04-28T00:00:01Z' },
        { type: 'assistant_text', content: 'c', timestamp: '2026-04-28T00:00:02Z' },
      ],
    }, 'sse');
    assert.equal(store._entriesViaFetchCount, 1);
    assert.equal(store._entriesViaSSECount, 2);
  });

  it('_diagSnapshotSessions surfaces tail_10 + counters', () => {
    const h = makeHarness();
    const store = h.win.getSessionStore('sess-3');
    // 12 entries → tail_10 should keep 10.
    const entries = [];
    for (let i = 0; i < 12; i++) {
      entries.push({
        type: 'assistant_text', content: 'msg-' + i,
        timestamp: '2026-04-28T00:00:' + String(i).padStart(2, '0') + 'Z',
      });
    }
    h.win.mergeSessionEntries(store, { seq: 12, entries }, 'sse');
    const snap = h.win._diagSnapshotSessions(['sess-3']);
    assert.ok(snap['sess-3'], 'session must be in snapshot');
    const block = snap['sess-3'];
    assert.equal(block.tail_10.length, 10, 'tail_10 keeps 10 entries');
    assert.equal(block.tail_3.length, 3, 'tail_3 still backwards-compat');
    assert.equal(block.entries_via_sse_count, 12);
    assert.equal(block.entries_via_fetch_count, 0);
    assert.equal(block.dedup_collisions, 0);
    assert.equal(typeof block.last_render_ms, 'number');
    assert.ok(block.counters, 'merge counters must be published');
    assert.equal(block.counters.merge_inserted, 12);
  });

  it('entries_with_null_seq_count counts entries without a seq', () => {
    const h = makeHarness();
    const store = h.win.getSessionStore('sess-null');
    h.win.mergeSessionEntries(store, {
      seq: 1, entries: [
        { type: 'assistant_text', content: 'no-seq', timestamp: '2026-04-28T00:00:00Z' },
        { type: 'user', content: 'has-seq', seq: 7, timestamp: '2026-04-28T00:00:01Z' },
      ],
    }, 'sse');
    const snap = h.win._diagSnapshotSessions(['sess-null']);
    assert.equal(snap['sess-null'].entries_with_null_seq_count, 1,
      'entries with no seq field counted');
  });

  it('_diagSnapshotSessions reads gap_replays_count + ts via global hooks', () => {
    const h = makeHarness();
    h.win.getSessionStore('sess-4');
    h.win._diagGapReplaysCount = function() { return 5; };
    h.win._diagLastGapReplayTs = function() { return 1745859600123; };
    const snap = h.win._diagSnapshotSessions(['sess-4']);
    assert.equal(snap['sess-4'].gap_replays_count, 5);
    assert.equal(snap['sess-4'].last_gap_replay_ts, 1745859600123);
  });
});
