// auto-16g9t commit C — the five failure-injection integration tests.
//
// Loads the REAL client stack (events.js, session-store.js,
// session-display.js, session-renderer.js, session-viewer.js) inside a
// stub DOM + fake EventSource, drives it against an in-memory fixture
// "file set" implementing the chain-tail contract, INJECTS the five
// production failure classes, and reads the /api/diag counter snapshot
// to prove recovery:
//
//   (a) withheld SSE broadcasts → span gap detected on the fly,
//       exactly the withheld entries fetched, buffer equals a control
//       client, zero duplicate display keys
//   (b) silent connection kill (iOS zombie: readyState stays OPEN) →
//       wake rebuilds the stream unconditionally + ranged catch-up
//   (c) wake with the cursor in a superseded (rolled-over) file →
//       remainder + successors served, committed lands in the new file
//   (d) scroll-up racing a live catch-up → buffer converges to the
//       sorted-by-tuple set, no duplicates
//   (e) empty-window scroll-up page over a noise region → paging never
//       dead-ends (the renderable-entries rule, client side)
//
// Correctness oracle: entry_refs make the expected buffer computable
// from the fixture files alone — after ANY interleaving of pages, gaps,
// and live events, the buffer must equal the sorted-by-tuple set of
// served entries. Every test also asserts conclusion_contradicted === 0
// (the viewer never believed a caught-up lie).
//
// Usage: node viewer_catchup_harness.js   (exit 0 = all pass)

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || '/workspace/repo';
const JS = (p) => path.join(REPO_ROOT, 'tools/dashboard/static/js', p);
const SOURCES = [
  ['events.js', JS('events.js')],
  ['session-store.js', JS('lib/session-store.js')],
  ['session-display.js', JS('lib/session-display.js')],
  ['session-renderer.js', JS('lib/session-renderer.js')],
  ['session-viewer.js', JS('pages/session-viewer.js')],
];

const LINE_BYTES = 100;   // fixed synthetic line size — offsets are idx*100
const SID = 'auto-inject';
const TAIL = `/api/session/autonomy/${SID}/tail`;

// ── Fixture session: the file set the server truth derives from ─────

class FixtureSession {
  constructor() {
    this.files = [{ stem: 'A', lines: [] }];
  }
  get current() { return this.files[this.files.length - 1]; }
  get chain() { return this.files.map((f) => f.stem); }
  // content === null → noise line (parses to nothing / unrenderable)
  appendLine(content) {
    const f = this.current;
    f.lines.push({ content });
    return { stem: f.stem, idx: f.lines.length - 1 };
  }
  rollover(stem) {
    this.files.push({ stem, lines: [] });
  }
  entryFor(stem, idx, line) {
    if (line.content === null) return null;
    return {
      type: 'assistant_text',
      role: 'assistant',
      content: line.content,
      timestamp: '2026-08-10T12:00:00Z',
      entry_ref: { file: stem, off: idx * LINE_BYTES, sub: 0 },
    };
  }
  // The oracle: every renderable entry, in tuple order.
  expectedRefs() {
    const out = [];
    for (const f of this.files) {
      f.lines.forEach((line, idx) => {
        if (line.content !== null) out.push(`${f.stem}:${idx * LINE_BYTES}:0`);
      });
    }
    return out;
  }
  spanFor(stem, idx) {
    return { file: stem, from: idx * LINE_BYTES, to: (idx + 1) * LINE_BYTES };
  }
  payloadFor(stem, idx) {
    const f = this.files.find((x) => x.stem === stem);
    const entry = this.entryFor(stem, idx, f.lines[idx]);
    return {
      session_id: SID,
      entries: entry ? [entry] : [],
      chain: this.chain,
      span: this.spanFor(stem, idx),
      is_live: true,
      activity_state: 'thinking',
      pending_tool_ids: [],
      context_tokens: 0,
      size_bytes: 0,
      seq: idx + 1,
    };
  }

  // ── The chain-tail contract over the fixture files ────────────────
  serveTail(url) {
    const q = new URLSearchParams(url.split('?')[1] || '');
    const base = {
      chain: this.chain,
      is_live: true,
      resolved: true,
      type: 'container',
      role: 'builder',
      activity_state: 'thinking',
      pending_tool_ids: [],
      offset: this.current.lines.length * LINE_BYTES,
    };
    if (q.has('tail_entries')) {
      return { ...base, ...this._serveReverse(
        parseInt(q.get('tail_entries'), 10),
        q.get('before_file'),
        q.has('before') ? parseInt(q.get('before'), 10) : null,
      ) };
    }
    if (q.has('after_file')) {
      return { ...base, ...this._serveForward(
        q.get('after_file'), parseInt(q.get('after'), 10)) };
    }
    throw new Error('fixture: unexpected tail query ' + url);
  }

  _serveReverse(n, beforeFile, beforeOff) {
    // Walk backward until n RENDERABLE entries collect (noise lines are
    // skipped but consume window bytes) or the chain start is reached.
    let fi = this.files.length - 1;
    let limit = null;
    if (beforeFile !== null && beforeFile !== undefined) {
      const at = this.files.findIndex((f) => f.stem === beforeFile);
      if (at !== -1) { fi = at; limit = Math.floor(beforeOff / LINE_BYTES); }
    }
    const entries = [];
    const spans = [];
    let older = null;
    let hasMore = false;
    while (fi >= 0 && entries.length < n) {
      const f = this.files[fi];
      let hi = limit === null ? f.lines.length : Math.min(limit, f.lines.length);
      let lo = hi;
      const got = [];
      while (lo > 0 && got.length < n - entries.length) {
        lo--;
        const e = this.entryFor(f.stem, lo, f.lines[lo]);
        if (e) got.unshift(e);
      }
      if (hi > lo) {
        entries.unshift(...got);
        spans.unshift({ file: f.stem, from: lo * LINE_BYTES, to: hi * LINE_BYTES });
        older = { file: f.stem, off: lo * LINE_BYTES };
      }
      if (lo > 0) { hasMore = true; break; }
      older = { file: f.stem, off: 0 };
      fi--;
      limit = null;
      hasMore = fi >= 0;
    }
    return { entries, window_spans: spans, older_cursor: older, has_more: hasMore };
  }

  _serveForward(afterFile, afterOff) {
    let fi = this.files.findIndex((f) => f.stem === afterFile);
    let startIdx = Math.floor(afterOff / LINE_BYTES);
    if (fi === -1) { fi = this.files.length - 1; startIdx = 0; }
    const entries = [];
    const spans = [];
    let cursor = { file: afterFile, off: afterOff };
    for (; fi < this.files.length; fi++) {
      const f = this.files[fi];
      const lo = startIdx;
      startIdx = 0;
      if (f.lines.length > lo) {
        for (let i = lo; i < f.lines.length; i++) {
          const e = this.entryFor(f.stem, i, f.lines[i]);
          if (e) entries.push(e);
        }
        spans.push({ file: f.stem, from: lo * LINE_BYTES, to: f.lines.length * LINE_BYTES });
      }
      cursor = { file: f.stem, off: f.lines.length * LINE_BYTES };
    }
    return { entries, window_spans: spans, cursor, has_more_forward: false };
  }
}

// ── Client harness: the real stack in a sandbox ─────────────────────

function makeClient(fixture, opts = {}) {
  const docListeners = {};
  const winListeners = {};
  const components = {};
  const stores = {};
  const fetchLog = [];
  // Requests held while a test sequences a race (test d).
  const held = [];

  const doc = {
    visibilityState: 'visible',
    addEventListener(n, cb) { (docListeners[n] ||= []).push(cb); },
    removeEventListener(n, cb) {
      const l = docListeners[n] || []; const i = l.indexOf(cb);
      if (i >= 0) l.splice(i, 1);
    },
  };

  class FakeEventSource {
    constructor(url) {
      this.url = url;
      this.readyState = 1;      // OPEN — zombies stay here forever
      this.listeners = {};
      FakeEventSource.instances.push(this);
      FakeEventSource.instance = this;
    }
    addEventListener(topic, cb) { this.listeners[topic] = cb; }
    close() { this.readyState = 2; }
    emit(busSeq, topic, data) {
      const cb = this.listeners[topic];
      if (!cb) return;
      cb({ lastEventId: `${busSeq}:1`, data: JSON.stringify(data) });
    }
  }
  FakeEventSource.instances = [];

  const win = {
    _sseCache: {},
    addEventListener(n, cb) { (winListeners[n] ||= []).push(cb); },
    removeEventListener(n, cb) {
      const l = winListeners[n] || []; const i = l.indexOf(cb);
      if (i >= 0) l.splice(i, 1);
    },
  };

  const alpine = {
    data(n, f) { components[n] = f; },
    store(n, obj) {
      if (obj !== undefined) { stores[n] = obj; return obj; }
      return stores[n];
    },
    // Reactivity is irrelevant here — display state is asserted through
    // SessionDisplay.buildAll directly. Watchers get a no-op teardown.
    watch() { return () => {}; },
  };

  const fetchFn = (url, options) => {
    fetchLog.push(url);
    const respond = () => {
      if (url.startsWith(TAIL + '?')) {
        const body = fixture.serveTail(url);
        return { ok: true, status: 200, json: () => Promise.resolve(body) };
      }
      if (url === '/api/dao/active_sessions') {
        return { json: () => Promise.resolve([]) };
      }
      if (url === '/api/worktrees') {
        return { ok: true, json: () => Promise.resolve([]) };
      }
      if (url.startsWith('/api/events/replay')) {
        return { json: () => Promise.resolve({ events: [], complete: true }) };
      }
      if (url.startsWith('/api/session/')) {
        return { ok: true, json: () => Promise.resolve({}) };
      }
      return { ok: true, json: () => Promise.resolve({}) };
    };
    if (opts.holdMatcher && opts.holdMatcher(url)) {
      return new Promise((resolve) => held.push({ url, release: () => resolve(respond()) }));
    }
    return Promise.resolve(respond());
  };

  const sandbox = {
    window: win, document: doc, Alpine: alpine,
    EventSource: FakeEventSource, fetch: fetchFn,
    console, setTimeout, clearTimeout, setInterval, clearInterval,
    Promise, JSON, Object, Array, Map, Set, Date, Error, URLSearchParams,
    parseInt, parseFloat, isNaN, Math,
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
  };
  win.document = doc;
  win.Alpine = alpine;
  win.fetch = fetchFn;
  win.setTimeout = setTimeout;
  vm.createContext(sandbox);

  for (const [name, file] of SOURCES) {
    let src = fs.readFileSync(file, 'utf8');
    if (name === 'session-store.js') {
      src = src.replace(
        'setTimeout(ensureSessionMessages, 0);',
        'setTimeout(window.ensureSessionMessages, 0);',
      );
    }
    vm.runInContext(src, sandbox, { filename: name });
  }
  for (const cb of (docListeners['alpine:init'] || [])) cb();

  function makeViewer() {
    const viewer = components.sessionViewerPage({});
    viewer.$watch = () => () => {};
    viewer.$nextTick = (fn) => { if (typeof fn === 'function') fn(); };
    viewer.$refs = {};
    viewer._scrollToBottom = () => {};
    return viewer;
  }

  return {
    win, doc, stores, fetchLog, held, makeViewer, FakeEventSource,
    async flush(rounds = 8) {
      for (let i = 0; i < rounds; i++) await new Promise((r) => setTimeout(r, 10));
    },
    emitDocument(n, ev) { for (const cb of (docListeners[n] || [])) cb(ev); },
    // Deliver one line's session:messages broadcast to THIS client only.
    deliver(stem, idx, busSeq) {
      FakeEventSource.instance.emit(busSeq, 'session:messages', fixture.payloadFor(stem, idx));
    },
    diag() {
      return this.win._diagSnapshotSessions([SID])[SID];
    },
  };
}

async function mountViewer(client) {
  const viewer = client.makeViewer();
  await viewer.configure({ sessionId: SID, project: 'autonomy' });
  await client.flush();
  return viewer;
}

function bufferRefs(client) {
  const store = client.win.getSessionStore(SID);
  return store.entries.map((e) => `${e.entry_ref.file}:${e.entry_ref.off}:${e.entry_ref.sub}`);
}

function displayKeys(client, viewer) {
  const store = client.win.getSessionStore(SID);
  return client.win.SessionDisplay
    .buildAll(store.entries, store.localEntries)
    .map((d) => d.key);
}

// ── Runner ──────────────────────────────────────────────────────────

let _passed = 0, _failed = 0;
function check(cond, msg, detail) {
  if (cond) { console.log('  ✓ ' + msg); _passed++; }
  else {
    console.error('  ✗ FAIL: ' + msg + (detail !== undefined ? '\n      ' + JSON.stringify(detail) : ''));
    _failed++;
  }
}
function checkEqual(actual, expected, msg) {
  const a = JSON.stringify(actual); const e = JSON.stringify(expected);
  check(a === e, msg, { expected, actual });
}
function assertNoLies(client, label) {
  const d = client.diag();
  checkEqual(d.counters.conclusion_contradicted, 0,
    label + ': conclusion_contradicted stays zero');
}

// (a) withheld broadcasts — gap detected on the fly, exactly-N fetched,
//     buffer equals a control client, zero duplicate tiles.
async function testA_withheldBroadcasts() {
  console.log('\n── (a) withheld broadcasts ──');
  const fixture = new FixtureSession();
  for (let i = 0; i < 3; i++) fixture.appendLine('seed-' + i);

  const clientX = makeClient(fixture);
  const control = makeClient(fixture);
  const viewerX = await mountViewer(clientX);
  const viewerC = await mountViewer(control);

  // Live writes 3..6; broadcasts 3..5 are withheld from X only.
  for (let i = 3; i <= 6; i++) {
    const at = fixture.appendLine('live-' + i);
    if (i === 6) clientX.deliver(at.stem, at.idx, i);   // the gap-exposing event
    control.deliver(at.stem, at.idx, i);
  }
  await clientX.flush();
  await control.flush();

  const dX = clientX.diag();
  check(dX.counters.span_gaps_detected >= 1, 'span gap detected on the fly');
  check(dX.counters.on_the_fly_catchups >= 1, 'ranged catch-up fired without waiting for a wake');
  checkEqual(dX.counters.gap_entries_total, 3, 'exactly the 3 withheld entries were fetched');
  checkEqual(bufferRefs(clientX), fixture.expectedRefs(), 'X buffer equals the file-set oracle');
  checkEqual(bufferRefs(clientX), bufferRefs(control), 'X buffer equals the control client');
  const keys = displayKeys(clientX, viewerX);
  checkEqual(keys.length, new Set(keys).size, 'zero duplicate display tiles');
  assertNoLies(clientX, '(a)');
  viewerX.destroy(); viewerC.destroy();
}

// (b) silent connection kill — the zombie case. readyState stays OPEN,
//     nothing is delivered; the wake must rebuild + catch up anyway.
async function testB_zombieStream() {
  console.log('\n── (b) silent connection kill (zombie) ──');
  const fixture = new FixtureSession();
  for (let i = 0; i < 3; i++) fixture.appendLine('seed-' + i);

  const client = makeClient(fixture);
  const viewer = await mountViewer(client);
  const esCountBefore = client.FakeEventSource.instances.length;

  // The stream silently dies: lines are written, NOTHING is delivered,
  // and readyState keeps reporting OPEN (the iOS zombie).
  fixture.appendLine('missed-1');
  fixture.appendLine('missed-2');
  check(client.win._es.readyState === 1, 'zombie stream still claims OPEN');

  viewer._setupResumeRecovery();
  client.emitDocument('visibilitychange');
  await client.flush();

  const d = client.diag();
  check(client.FakeEventSource.instances.length > esCountBefore,
    'wake tore down and re-opened the SSE connection unconditionally');
  checkEqual(d.counters.stream_rebuilds, 1, 'stream rebuild counted');
  checkEqual(d.counters.stream_rebuilds_dead, 0, 'old connection was NOT provably dead (zombie)');
  checkEqual(d.counters.wake_gap, 1, 'wake classified as a gap wake');
  checkEqual(d.counters.gap_entries_total, 2, 'both missed entries recovered');
  checkEqual(bufferRefs(client), fixture.expectedRefs(), 'buffer equals the oracle');
  assertNoLies(client, '(b)');
  viewer.destroy();
}

// (c) wake with the cursor in a superseded file — rollover happened
//     while asleep; the catch-up must chain forward, never lie.
async function testC_supersededCursor() {
  console.log('\n── (c) wake with cursor in a superseded file ──');
  const fixture = new FixtureSession();
  for (let i = 0; i < 3; i++) fixture.appendLine('a-' + i);

  const client = makeClient(fixture);
  const viewer = await mountViewer(client);
  const store = client.win.getSessionStore(SID);
  checkEqual({ ...store.committed }, { file: 'A', off: 300 }, 'committed anchored in file A');

  // Asleep: A grows one more line, then rolls over to B which grows two.
  fixture.appendLine('a-3');
  fixture.rollover('B');
  fixture.appendLine('b-0');
  fixture.appendLine('b-1');

  viewer._setupResumeRecovery();
  client.emitDocument('visibilitychange');
  await client.flush();

  const d = client.diag();
  checkEqual(bufferRefs(client), fixture.expectedRefs(),
    'remainder of A + all of B recovered in order');
  checkEqual({ ...store.committed }, { file: 'B', off: 200 },
    'committed lands at the successor file\'s end');
  checkEqual(Array.from(store.chain), ['A', 'B'], 'chain adopted');
  checkEqual(d.counters.gap_entries_total, 3, 'the 3 slept-through entries fetched');
  assertNoLies(client, '(c)');
  viewer.destroy();
}

// (d) scroll-up during catch-up — two racing merges must converge to
//     the sorted-by-tuple set with zero duplicates.
async function testD_scrollUpDuringCatchup() {
  console.log('\n── (d) scroll-up during catch-up ──');
  const fixture = new FixtureSession();
  for (let i = 0; i < 30; i++) fixture.appendLine('old-' + i);

  // Hold forward catch-up responses so the scroll-up can race them.
  const client = makeClient(fixture, {
    holdMatcher: (url) => url.includes('after_file='),
  });
  // Fast-open with a small window so history remains to scroll into.
  client.win.getSessionStore(SID);
  const viewer = client.makeViewer();
  viewer._initialTailUrl = function () { return TAIL + '?tail_entries=10'; };
  await viewer.configure({ sessionId: SID, project: 'autonomy' });
  await client.flush();
  const store = client.win.getSessionStore(SID);
  checkEqual(store.entries.length, 10, 'fast-open window loaded');

  // New live lines the client hasn't seen; wake starts a catch-up whose
  // response we HOLD…
  fixture.appendLine('new-30');
  fixture.appendLine('new-31');
  viewer._setupResumeRecovery();
  client.emitDocument('visibilitychange');
  await client.flush(2);
  check(client.held.length === 1, 'catch-up response held in flight');

  // …while the operator scrolls up (this response is NOT held).
  const loadOlderP = viewer.loadOlder();
  await client.flush(2);

  // Release the held catch-up AFTER the scroll-up merged.
  client.held.forEach((hh) => hh.release());
  await loadOlderP;
  await client.flush();

  // The older page uses the standard 200-entry window, so it pulls the
  // whole remaining history — the converged buffer is the full oracle.
  checkEqual(bufferRefs(client), fixture.expectedRefs(),
    'buffer converges to the oracle across the race (window + older page + catch-up)');
  const keys = displayKeys(client, viewer);
  checkEqual(keys.length, new Set(keys).size, 'zero duplicate display tiles across the race');
  assertNoLies(client, '(d)');
  viewer.destroy();
}

// (e) empty-window scroll-up page — a pure-noise region must not
//     dead-end paging (the renderable-entries rule).
async function testE_noiseRegionPaging() {
  console.log('\n── (e) empty-window scroll-up over a noise region ──');
  const fixture = new FixtureSession();
  fixture.appendLine('ancient');
  for (let i = 0; i < 40; i++) fixture.appendLine(null);   // unrenderable noise
  fixture.appendLine('recent');

  const client = makeClient(fixture);
  const viewer = client.makeViewer();
  viewer._initialTailUrl = function () { return TAIL + '?tail_entries=1'; };
  await viewer.configure({ sessionId: SID, project: 'autonomy' });
  await client.flush();
  const store = client.win.getSessionStore(SID);
  checkEqual(store.entries.map((e) => e.content), ['recent'], 'fast-open got the newest entry');
  check(store.hasMoreHistory === true, 'more history advertised');

  let pages = 0;
  while (store.hasMoreHistory && pages < 10) {
    await viewer.loadOlder();
    await client.flush(2);
    pages++;
  }
  checkEqual(store.entries.map((e) => e.content), ['ancient', 'recent'],
    'paging reached the content behind 40 noise lines without dead-ending');
  check(pages < 10, 'paging terminated (has_more went false at chain start)');
  assertNoLies(client, '(e)');
  viewer.destroy();
}

(async () => {
  try {
    await testA_withheldBroadcasts();
    await testB_zombieStream();
    await testC_supersededCursor();
    await testD_scrollUpDuringCatchup();
    await testE_noiseRegionPaging();
  } catch (e) {
    console.error('HARNESS ERROR:', e);
    process.exit(2);
  }
  console.log(`\n${_passed} passed, ${_failed} failed`);
  process.exit(_failed === 0 ? 0 : 1);
})();
