// Node harness that reproduces the SSE gap-replay dedup bug in
// session-store.js's appendSessionEntries().
//
// Loads the real events.js and session-store.js (unmodified) inside a stub
// DOM + stub EventSource, drives three scenarios, asserts store state.
//
// Scenarios:
//   A — clean gap/replay through events.js: regression guard (passes today).
//   B — real-world race: store.seq advanced out-of-band before the replay
//       response processes. Silent drop (FAILS today).
//   C — isolated dedup: seq-guarded drop when store.seq is ahead (FAILS today).
//
// Usage: node harness_gap_dedup.js
//   exit 0 on all tests passing, exit 1 on any failure.
//
// Implementer notes:
//   Test A must continue to pass after the fix — do NOT regress in-order merge.
//   Tests B and C demonstrate the bug; both should pass after implementing
//   entry-identity dedup (keyed on tool_id) in appendSessionEntries.

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || '/workspace/repo';
const EVENTS_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/events.js');
const STORE_JS  = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/session-store.js');

// ── Harness ────────────────────────────────────────────────────────

function makeHarness() {
  const docListeners = {};
  const doc = {
    addEventListener(name, cb) {
      (docListeners[name] ||= []).push(cb);
    },
  };

  const _alpineStores = {};
  const alpine = {
    store(name, obj) {
      if (obj !== undefined) { _alpineStores[name] = obj; return obj; }
      return _alpineStores[name];
    },
  };

  class FakeEventSource {
    constructor(url) {
      this.url = url;
      this.namedListeners = {};
      FakeEventSource.instance = this;
    }
    addEventListener(topic, cb) { this.namedListeners[topic] = cb; }
    close() { this._closed = true; }
    emit(busSeq, epoch, topic, data) {
      const cb = this.namedListeners[topic];
      if (!cb) return;
      cb({
        lastEventId: `${busSeq}:${epoch}`,
        data: JSON.stringify(data),
      });
    }
  }

  const replayResponses = new Map();

  const fetchFn = (url) => {
    if (url.startsWith('/api/events/replay')) {
      const m = url.match(/from=(\d+)&to=(\d+)/);
      const key = `${m[1]}-${m[2]}`;
      const body = replayResponses.get(key) || { events: [], complete: false };
      return Promise.resolve({ json: () => Promise.resolve(body) });
    }
    if (url === '/api/dao/active_sessions') {
      return Promise.resolve({ json: () => Promise.resolve([]) });
    }
    return Promise.resolve({ json: () => Promise.resolve({}) });
  };

  const win = { _sseCache: {} };

  const sandbox = {
    window: win, document: doc, Alpine: alpine,
    EventSource: FakeEventSource, fetch: fetchFn,
    console, setTimeout, clearTimeout, setInterval, clearInterval,
    Promise, JSON, Object, Array, Set, Map, Date, parseInt, parseFloat, Error,
  };
  sandbox.window.Alpine = alpine;
  sandbox.window.document = doc;
  sandbox.window.fetch = fetchFn;
  sandbox.window.setTimeout = setTimeout;
  vm.createContext(sandbox);

  const eventsSrc = fs.readFileSync(EVENTS_JS, 'utf8');
  let storeSrc = fs.readFileSync(STORE_JS, 'utf8');
  // In real browser, bare `ensureSessionMessages` resolves via window global
  // scope fallback. vm.Context has no such fallback — patch to window.*.
  storeSrc = storeSrc.replace(
    'setTimeout(ensureSessionMessages, 0);',
    'setTimeout(window.ensureSessionMessages, 0);'
  );
  vm.runInContext(eventsSrc, sandbox, { filename: 'events.js' });
  vm.runInContext(storeSrc, sandbox, { filename: 'session-store.js' });
  for (const cb of (docListeners['alpine:init'] || [])) cb();

  return {
    sandbox, win, alpine, doc, FakeEventSource, replayResponses,
    async flush() {
      for (let i = 0; i < 5; i++) {
        await new Promise((r) => setTimeout(r, 10));
      }
    },
  };
}

// ── Test fixtures ──────────────────────────────────────────────────

function makeEntry(id) {
  return {
    type: 'tool_use',
    tool_id: id,
    tool_name: 'Bash',
    timestamp: '2026-04-20T11:51:00Z',
  };
}

function sessionMessagesPayload(sessionId, payloadSeq, entries) {
  return {
    session_id: sessionId,
    seq: payloadSeq,
    entries,
    activity_state: 'thinking',
    context_tokens: 0,
    is_live: true,
    pending_tool_ids: [],
    size_bytes: 1000,
  };
}

// Build a replay chain matching real log shape: bus seqs 219-227 cover 9
// events; 7 are session:messages for our session (payload seqs 124-130)
// and 2 are nav events the session handler ignores.
function buildReplayChain(sessionId, busNavSet = new Set([224, 226])) {
  const events = [];
  let payloadSeq = 124;
  for (let busSeq = 219; busSeq <= 227; busSeq++) {
    if (busNavSet.has(busSeq)) {
      events.push({ seq: busSeq, topic: 'nav', data: { running_agents: 0 } });
    } else {
      events.push({
        seq: busSeq,
        topic: 'session:messages',
        data: sessionMessagesPayload(sessionId, payloadSeq, [makeEntry(`E${payloadSeq}`)]),
      });
      payloadSeq++;
    }
  }
  return { events, complete: true };
}

// ── Test runner ────────────────────────────────────────────────────

let _failed = 0, _passed = 0;

function assertEqual(actual, expected, msg) {
  const aJson = JSON.stringify(actual);
  const eJson = JSON.stringify(expected);
  if (aJson !== eJson) {
    console.error(`✗ FAIL: ${msg}\n    expected: ${eJson}\n    actual:   ${aJson}`);
    _failed++;
    return false;
  }
  console.log(`✓ ${msg}`);
  _passed++;
  return true;
}

// ── Tests ──────────────────────────────────────────────────────────

async function testA_clean_gap_replay() {
  console.log('\n── Test A: clean events.js gap/replay ────────────────────');
  const h = makeHarness();
  await h.flush();

  const sessionId = 'auto-test';
  const store = h.win.getSessionStore(sessionId);

  // Pre-state: 3 entries applied, store.seq=123, _lastSeq=218.
  h.win.appendSessionEntries(store, sessionMessagesPayload(
    sessionId, 123, [makeEntry('E1'), makeEntry('E2'), makeEntry('E3')]
  ));
  h.win._lastSeq = 218;

  // Pre-stage replay response.
  h.replayResponses.set('219-227', buildReplayChain(sessionId));

  // Fire trigger (bus 228, payload 131). events.js should hold, fetch replay,
  // dispatch replay 219-227 first, then dispatch held trigger.
  h.FakeEventSource.instance.emit(228, 1, 'session:messages',
    sessionMessagesPayload(sessionId, 131, [makeEntry('E131')])
  );
  await h.flush();

  const ids = store.entries.map((e) => e.tool_id);
  assertEqual(
    ids,
    ['E1','E2','E3','E124','E125','E126','E127','E128','E129','E130','E131'],
    'A: replay + held trigger merge in order, no gaps, no dupes'
  );
}

async function testB_out_of_band_store_seq_advance() {
  console.log('\n── Test B: store.seq advanced out-of-band before replay ──');
  const h = makeHarness();
  await h.flush();

  const sessionId = 'auto-test';
  const store = h.win.getSessionStore(sessionId);

  // Pre-state.
  h.win.appendSessionEntries(store, sessionMessagesPayload(
    sessionId, 123, [makeEntry('E1'), makeEntry('E2'), makeEntry('E3')]
  ));
  h.win._lastSeq = 218;

  // OUT-OF-BAND: something (iOS native EventSource reconnect replay, or a
  // _fetchBacklog firing on page re-init during app-resume) delivered E131
  // to the store BEFORE the gap replay events run through events.js.
  h.win.appendSessionEntries(store, sessionMessagesPayload(
    sessionId, 131, [makeEntry('E131')]
  ));

  // Now fire the trigger that reveals the gap to events.js. Replay returns
  // bus 219-227 → payload 124-130. With the current seq-based dedup, every
  // replay dispatch is silently dropped because data.seq (124..130) ≤ store.seq (131).
  // Fix (entry-identity dedup) must allow the new entries to append.
  h.replayResponses.set('219-227', buildReplayChain(sessionId));
  h.FakeEventSource.instance.emit(228, 1, 'session:messages',
    sessionMessagesPayload(sessionId, 131, [makeEntry('E131')])  // duplicate of pre-seed
  );
  await h.flush();

  const ids = store.entries.map((e) => e.tool_id);
  // Expected after fix: the 7 replay entries land; the duplicate E131 does not
  // re-append (entry-identity dedup).
  assertEqual(
    ids,
    ['E1','E2','E3','E131','E124','E125','E126','E127','E128','E129','E130'],
    'B: replay entries land even when store.seq is ahead; duplicate held trigger is deduped by tool_id'
  );
}

async function testC_dedup_silent_drop_isolated() {
  console.log('\n── Test C: isolated dedup — store.seq ahead of replay payloads ──');
  const h = makeHarness();
  await h.flush();

  const sessionId = 'auto-test';
  const store = h.win.getSessionStore(sessionId);

  // Pre-state: single entry at store.seq=131.
  h.win.appendSessionEntries(store, sessionMessagesPayload(
    sessionId, 131, [makeEntry('E131')]
  ));

  // Apply 7 "replay-shaped" payloads with per-session seqs 124..130.
  for (let pseq = 124; pseq <= 130; pseq++) {
    h.win.appendSessionEntries(store, sessionMessagesPayload(
      sessionId, pseq, [makeEntry(`E${pseq}`)]
    ));
  }

  const ids = store.entries.map((e) => e.tool_id);
  // Current code: returns 0 for each (data.seq ≤ store.seq), E124-E130 dropped.
  // Fix: entries append (entry-identity dedup doesn't care about seq order).
  assertEqual(
    ids,
    ['E131','E124','E125','E126','E127','E128','E129','E130'],
    'C: replay-shaped payloads with lower seq land (entry-identity dedup)'
  );
}

// ── Main ────────────────────────────────────────────────────────────

(async () => {
  try {
    await testA_clean_gap_replay();
    await testB_out_of_band_store_seq_advance();
    await testC_dedup_silent_drop_isolated();
  } catch (e) {
    console.error('HARNESS ERROR:', e);
    process.exit(2);
  }
  console.log(`\n${_passed} passed, ${_failed} failed`);
  process.exit(_failed === 0 ? 0 : 1);
})();
