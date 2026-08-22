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

const SID = 'auto-inject';
const TAIL = `/api/session/autonomy/${SID}/tail`;

// ── Fixture session: the file set the server truth derives from ─────
//
// Round-1 review S4 (fixture realism): lines have REAL variable byte
// lengths (the serialized entry payload itself), files can carry a
// trailing PARTIAL line the way a mid-write JSONL does, and the reverse/
// forward handlers clamp to the last complete newline exactly like the
// fixed server. Offsets are true cumulative byte positions.

class FixtureSession {
  constructor() {
    this.files = [];
    this.rollover('A');
  }
  get current() { return this.files[this.files.length - 1]; }
  get chain() { return this.files.map((f) => f.stem); }

  rollover(stem) {
    this.files.push({ stem, lines: [], completeSize: 0, partial: null });
  }

  // content === null → noise line (unrenderable but real bytes).
  appendLine(content, extraBytes) {
    const f = this.current;
    const start = f.completeSize;
    const body = content === null
      ? '{"type":"noise","pad":"' + 'n'.repeat(17 + (f.lines.length % 13)) + '"}'
      : JSON.stringify({ type: 'assistant_text', content });
    const len = body.length + 1 + (extraBytes || 0);   // + newline
    f.lines.push({ content, start, len });
    f.completeSize = start + len;
    return { stem: f.stem, idx: f.lines.length - 1 };
  }

  // A fully-typed entry line (tool_use / tool_result / user …) — the
  // agent-transcript shape (tool-heavy, long carrier runs) the anchor
  // rule is exercised against.
  appendTyped(payload) {
    const f = this.current;
    const start = f.completeSize;
    const len = JSON.stringify(payload).length + 1;
    f.lines.push({ content: undefined, typed: payload, start, len });
    f.completeSize = start + len;
    return { stem: f.stem, idx: f.lines.length - 1 };
  }

  // A writer mid-line: bytes exist past completeSize with no newline.
  appendPartial(content) {
    this.current.partial = { content, bytes: Math.floor(JSON.stringify(content).length / 2) };
  }
  completePartial() {
    const f = this.current;
    if (!f.partial) throw new Error('fixture: no partial to complete');
    const content = f.partial.content;
    f.partial = null;
    return this.appendLine(content);
  }

  physicalSize(f) { return f.completeSize + (f.partial ? f.partial.bytes : 0); }

  entryFor(stem, line) {
    if (line.content === null) return null;
    if (line.typed) {
      return Object.assign({}, line.typed, {
        timestamp: line.typed.timestamp || '2026-08-10T12:00:00Z',
        entry_ref: { file: stem, off: line.start, sub: 0 },
      });
    }
    return {
      type: 'assistant_text',
      role: 'assistant',
      content: line.content,
      timestamp: '2026-08-10T12:00:00Z',
      entry_ref: { file: stem, off: line.start, sub: 0 },
    };
  }
  // The oracle: every renderable COMPLETE entry, in tuple order.
  expectedRefs() {
    const out = [];
    for (const f of this.files) {
      for (const line of f.lines) {
        if (line.content !== null) out.push(`${f.stem}:${line.start}:0`);
      }
    }
    return out;
  }
  spanFor(stem, idx) {
    const f = this.files.find((x) => x.stem === stem);
    const line = f.lines[idx];
    return { file: stem, from: line.start, to: line.start + line.len };
  }
  payloadFor(stem, idx) {
    const f = this.files.find((x) => x.stem === stem);
    const entry = this.entryFor(stem, f.lines[idx]);
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
      offset: this.physicalSize(this.current),
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

  _lineIndexAt(f, off) {
    // Number of complete lines strictly before byte `off`.
    let i = 0;
    while (i < f.lines.length && f.lines[i].start + f.lines[i].len <= off) i++;
    return i;
  }

  _serveReverse(n, beforeFile, beforeOff) {
    // Walk backward until n RENDERABLE entries collect (noise lines are
    // skipped but consume window bytes) or the chain start is reached.
    // Windows/spans NEVER extend past completeSize — the partial tail is
    // invisible, like the fixed server (B1).
    let fi = this.files.length - 1;
    let limit = null;   // line-count limit within the file
    if (beforeFile !== null && beforeFile !== undefined) {
      const at = this.files.findIndex((f) => f.stem === beforeFile);
      if (at !== -1) { fi = at; limit = this._lineIndexAt(this.files[at], beforeOff); }
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
        const e = this.entryFor(f.stem, f.lines[lo]);
        if (e) got.unshift(e);
      }
      if (hi > lo) {
        entries.unshift(...got);
        spans.unshift({
          file: f.stem,
          from: f.lines[lo].start,
          to: f.lines[hi - 1].start + f.lines[hi - 1].len,
        });
        older = { file: f.stem, off: f.lines[lo].start };
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
    let startIdx = null;
    if (fi === -1) { fi = this.files.length - 1; startIdx = 0; }
    else startIdx = this._lineIndexAt(this.files[fi], afterOff);
    const entries = [];
    const spans = [];
    let cursor = { file: afterFile, off: afterOff };
    for (; fi < this.files.length; fi++) {
      const f = this.files[fi];
      const lo = startIdx;
      startIdx = 0;
      if (f.lines.length > lo) {
        for (let i = lo; i < f.lines.length; i++) {
          const e = this.entryFor(f.stem, f.lines[i]);
          if (e) entries.push(e);
        }
        spans.push({
          file: f.stem,
          from: f.lines[lo].start,
          to: f.completeSize,
        });
      }
      cursor = { file: f.stem, off: f.completeSize };
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
    deliver(stem, idx, busSeq, entryCount) {
      const payload = fixture.payloadFor(stem, idx);
      if (entryCount !== undefined) payload.entry_count = entryCount;
      FakeEventSource.instance.emit(busSeq, 'session:messages', payload);
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

// Live message metadata updates the count immediately. Because reconnect
// replay can deliver older payloads, the cumulative value is monotonic.
async function testN_liveEntryCount() {
  console.log('\n── (n) live entry count ──');
  const fixture = new FixtureSession();
  fixture.appendLine('seed');

  const client = makeClient(fixture);
  const viewer = await mountViewer(client);
  const store = client.win.getSessionStore(SID);
  store.entryCount = 10;

  const live = fixture.appendLine('live');
  client.deliver(live.stem, live.idx, 1, 11);
  checkEqual(store.entryCount, 11, 'live broadcast advances the cumulative count');

  client.deliver(live.stem, live.idx, 2, 9);
  checkEqual(store.entryCount, 11, 'stale replay cannot lower the cumulative count');

  client.FakeEventSource.instance.emit(3, 'session:registry', [{
    session_id: SID,
    entry_count: 10,
    is_live: true,
  }]);
  checkEqual(store.entryCount, 11, 'stale registry hydration cannot lower the live count');
  viewer.destroy();
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
  const aComplete = fixture.files[0].completeSize;
  checkEqual({ ...store.committed }, { file: 'A', off: aComplete },
    'committed anchored at file A\'s complete end');

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
  checkEqual({ ...store.committed },
    { file: 'B', off: fixture.files[1].completeSize },
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

// (f) B1 shape: cold-open during a partial write. committed must anchor
//     at the last complete newline; the line, once finished, must arrive.
async function testF_partialLineColdOpen() {
  console.log('\n── (f) cold-open into a partial write (B1) ──');
  const fixture = new FixtureSession();
  fixture.appendLine('done-0');
  fixture.appendLine('done-1');
  fixture.appendPartial('finished-later');   // writer mid-line at cold-open

  const client = makeClient(fixture);
  const viewer = await mountViewer(client);
  const store = client.win.getSessionStore(SID);
  const completeAtOpen = fixture.files[0].completeSize;
  checkEqual({ ...store.committed }, { file: 'A', off: completeAtOpen },
    'committed anchors at the last COMPLETE newline, not physical EOF');

  // The writer finishes the line + one more while every broadcast is
  // missed (the B1 timeline). Wake must recover BOTH.
  fixture.completePartial();
  fixture.appendLine('after');
  viewer._setupResumeRecovery();
  client.emitDocument('visibilitychange');
  await client.flush();

  checkEqual(bufferRefs(client), fixture.expectedRefs(),
    'the once-partial line is recovered — never permanently skipped');
  checkEqual(store.entries.map((e) => e.content),
    ['done-0', 'done-1', 'finished-later', 'after'], 'contents complete');
  assertNoLies(client, '(f)');
  viewer.destroy();

  // Round-2 RB1 shape 1: the partial line is HUGE (>64KiB — production
  // tool-result sizes). The anchor must still sit before it.
  const fx2 = new FixtureSession();
  fx2.appendLine('small-complete');
  fx2.appendPartial('X'.repeat(140000));
  const c2 = makeClient(fx2);
  const v2 = await mountViewer(c2);
  const s2 = c2.win.getSessionStore(SID);
  checkEqual({ ...s2.committed }, { file: 'A', off: fx2.files[0].completeSize },
    '>64KiB partial: committed still anchors at the last complete newline');
  fx2.completePartial();
  v2._setupResumeRecovery();
  c2.emitDocument('visibilitychange');
  await c2.flush();
  checkEqual(bufferRefs(c2), fx2.expectedRefs(),
    '>64KiB once-partial line recovered on wake');
  assertNoLies(c2, '(f/rb1a)');
  v2.destroy();

  // Round-2 RB1 shape 2: the file is ONLY a giant partial line.
  const fx3 = new FixtureSession();
  fx3.appendPartial('Y'.repeat(140000));
  const c3 = makeClient(fx3);
  const v3 = await mountViewer(c3);
  const s3 = c3.win.getSessionStore(SID);
  checkEqual(s3.entries.length, 0, 'sole >64KiB partial: cold-open serves nothing');
  check(!s3.committed || s3.committed.off === 0,
    'sole partial: committed never anchors at physical EOF');
  fx3.completePartial();
  v3._setupResumeRecovery();
  c3.emitDocument('visibilitychange');
  await c3.flush();
  checkEqual(bufferRefs(c3), fx3.expectedRefs(),
    'sole once-partial line recovered on wake');
  assertNoLies(c3, '(f/rb1b)');
  v3.destroy();
}

// (g) B7 shape: forced merge failure — the ack must NOT advance past
//     unapplied content, and recovery must refetch it.
async function testG_mergeFailureBeforeCommit() {
  console.log('\n── (g) forced merge failure before commit (B7) ──');
  const fixture = new FixtureSession();
  fixture.appendLine('base-0');

  // Hold the recovery catch-up so the invariant is observable before the
  // (correct) recovery advances committed again.
  const client = makeClient(fixture, {
    holdMatcher: (url) => url.includes('after_file='),
  });
  const viewer = await mountViewer(client);
  const store = client.win.getSessionStore(SID);
  const committedBefore = { ...store.committed };

  // Poison exactly one merge call (the next SSE delivery).
  const realMerge = client.win.mergeSessionEntries;
  let poisoned = true;
  client.win.mergeSessionEntries = function (s, data, prov) {
    if (poisoned && prov === 'sse') {
      poisoned = false;
      throw new Error('injected merge failure');
    }
    return realMerge(s, data, prov);
  };

  const at = fixture.appendLine('poisoned-delivery');
  client.deliver(at.stem, at.idx, 2);
  await client.flush(2);

  checkEqual({ ...store.committed }, committedBefore,
    'a failed merge must NOT advance the committed high-water (apply-before-ack)');
  check(client.held.length === 1,
    'the merge failure triggered a recovery catch-up');

  // Release the recovery: the entry must land without a wake, and only
  // then may committed advance.
  client.held.forEach((hh) => hh.release());
  await client.flush();
  checkEqual(bufferRefs(client), fixture.expectedRefs(),
    'the dropped delivery is refetched and applied');
  checkEqual({ ...store.committed },
    { file: 'A', off: fixture.files[0].completeSize },
    'committed advances only after successful application');
  assertNoLies(client, '(g)');
  viewer.destroy();
}

// (h) B2 client shape: a stalling forward response (has_more_forward with
//     an unmoved cursor) must back off, never hot-loop at zero delay.
async function testH_stalledContinuationBacksOff() {
  console.log('\n── (h) stalled continuation backs off (B2 client) ──');
  const fixture = new FixtureSession();
  fixture.appendLine('only');

  const client = makeClient(fixture);
  const viewer = await mountViewer(client);
  const store = client.win.getSessionStore(SID);

  // Malicious server: forward responses claim more data but never move.
  const realServe = fixture.serveTail.bind(fixture);
  let forwardCalls = 0;
  fixture.serveTail = (url) => {
    if (url.includes('after_file=')) {
      forwardCalls++;
      return {
        chain: fixture.chain, is_live: true, resolved: true,
        type: 'container', role: 'builder', activity_state: 'thinking',
        pending_tool_ids: [], offset: 0,
        entries: [], window_spans: [],
        cursor: { ...store.committed },
        has_more_forward: true,
      };
    }
    return realServe(url);
  };

  viewer._setupResumeRecovery();
  client.emitDocument('visibilitychange');
  // 300ms of wall-clock: a zero-delay hot loop would rack up hundreds of
  // fetches; bounded backoff allows only the wake call + a couple of
  // short-delay retries.
  await new Promise((r) => setTimeout(r, 300));
  check(forwardCalls <= 4,
    `stalled continuation is rate-limited (saw ${forwardCalls} forward calls in 300ms)`);
  const d = client.diag();
  check(d.counters.catchup_stalls >= 1, 'stall counted in diag');
  assertNoLies(client, '(h)');
  viewer.destroy();
}

// (i) S2 shape: a server that pruned a predecessor sends a shorter
//     chain — adoption must never reverse the retained history's order.
async function testI_prunedPredecessorChain() {
  console.log('\n── (i) pruned-predecessor chain adoption (S2) ──');
  const fixture = new FixtureSession();
  fixture.appendLine('m1-old');
  fixture.rollover('B');
  fixture.appendLine('m2-new');

  const client = makeClient(fixture);
  const viewer = await mountViewer(client);
  const store = client.win.getSessionStore(SID);
  // Scroll back so file A's history is retained client-side.
  while (store.hasMoreHistory) { await viewer.loadOlder(); await client.flush(2); }
  checkEqual(Array.from(store.chain), ['A', 'B'], 'full chain retained');
  const refsBefore = bufferRefs(client);

  // The server prunes A (dead predecessor) — its responses now carry
  // chain ['B'] only. Deliver a live line under the pruned chain.
  fixture.files.splice(0, 1);
  const at = fixture.appendLine('m2-newer');
  client.deliver(at.stem, at.idx, 9);
  await client.flush(2);

  checkEqual(Array.from(store.chain), ['A', 'B'],
    'subsequence adoption keeps retained history order (no [B, A] reversal)');
  checkEqual(bufferRefs(client).slice(0, refsBefore.length), refsBefore,
    'retained entries keep their order and identity');
  assertNoLies(client, '(i)');
  viewer.destroy();
}

// (j) addendum item 2: two racing catch-ups — the stale response
//     resolving LAST must never regress the committed high-water.
async function testJ_racingCatchupsMonotonic() {
  console.log('\n── (j) racing catch-ups keep committed monotonic ──');
  const fixture = new FixtureSession();
  fixture.appendLine('base-0');
  fixture.appendLine('base-1');

  // Hold every forward response; we release them out of order.
  const client = makeClient(fixture, {
    holdMatcher: (url) => url.includes('after_file='),
  });
  const viewer = await mountViewer(client);
  const store = client.win.getSessionStore(SID);

  // Race: wake catch-up 1 issued at cursor X (held)…
  viewer._setupResumeRecovery();
  client.emitDocument('visibilitychange');
  await client.flush(2);
  check(client.held.length === 1, 'catch-up 1 held');
  const held1 = client.held.splice(0, 1)[0];

  // …file grows; the span-gap path fires catch-up 2 (also held), which
  // will see the LONGER file.
  const at = fixture.appendLine('late');
  client.deliver(at.stem, at.idx + 1 === fixture.files[0].lines.length ? at.idx : at.idx, 5);
  await client.flush(2);
  // The SSE delivery merged contiguously — no second fetch needed; force
  // one via the heartbeat wake instead.
  viewer._resumeHeartbeatAt = 1;
  viewer._checkResumeHeartbeat(999999);
  await client.flush(2);
  const later = client.held.splice(0, client.held.length);

  // Resolve the NEWER catch-up(s) first, then the stale one.
  later.forEach((hh) => hh.release());
  await client.flush(2);
  const committedHigh = { ...store.committed };
  held1.release();
  await client.flush(2);

  check(store.committed.off >= committedHigh.off &&
        store.committed.file === committedHigh.file,
    `stale catch-up must not regress committed (${JSON.stringify(committedHigh)} → ${JSON.stringify(store.committed)})`);
  checkEqual(bufferRefs(client), fixture.expectedRefs(), 'buffer equals the oracle');
  assertNoLies(client, '(j)');
  viewer.destroy();
}

// (k) auto-64nx3 acceptance — the anchor rule over an agent-transcript
//     shape (tool-heavy, long carrier runs; the IMG_2110 window class):
//     a mobile-small cold-open window landing mid-carrier-run renders
//     NOTHING above its first anchor — no dangling fragment can appear
//     as an empty USER tile or as ASSISTANT prose — and scroll-up makes
//     the held entries render exactly once under their owning flow,
//     with attribution identical to a full-buffer build.
async function testK_anchorRuleAgentTranscript() {
  console.log('\n── (k) anchor rule: agent-transcript scroll-back (auto-64nx3) ──');
  const fixture = new FixtureSession();
  fixture.appendTyped({ type: 'user', role: 'user', content: 'kick off the review' });
  fixture.appendLine('starting the sweep');           // assistant_text anchor
  const carrierIds = [];
  for (let i = 0; i < 6; i++) {
    fixture.appendTyped({ type: 'tool_use', role: 'assistant', tool_name: 'Bash',
      tool_id: 'T' + i, input: { command: 'run ' + i } });
    fixture.appendTyped({ type: 'tool_result', role: 'tool', tool_id: 'T' + i,
      content: 'TLC suite results chunk ' + i, result_kind: 'exec_command',
      status: 'completed' });
    carrierIds.push('T' + i);
  }
  fixture.appendLine('verdict: green');               // later anchor
  fixture.appendTyped({ type: 'tool_use', role: 'assistant', tool_name: 'Read',
    tool_id: 'T9', input: { file_path: '/x' } });
  fixture.appendTyped({ type: 'tool_result', role: 'tool', tool_id: 'T9',
    content: 'tail read', result_kind: 'exec_command', status: 'completed' });

  const client = makeClient(fixture);
  const viewer = client.makeViewer();
  // Mobile-small window: the page boundary lands mid-carrier-run,
  // ABOVE the 'verdict' anchor.
  viewer._initialTailUrl = function () { return TAIL + '?tail_entries=6'; };
  await viewer.configure({ sessionId: SID, project: 'autonomy' });
  await client.flush();
  const store = client.win.getSessionStore(SID);
  check(store.entries.length >= 6, 'cold window holds the mid-run fragments');

  function resolvedTypes() {
    return client.win.SessionDisplay
      .buildAll(store.entries, store.localEntries)
      .map((d) => {
        const e = client.win.SessionDisplay.resolve(d, store.entries, store.localEntries);
        return e && (d.type === 'group' ? 'tool_group' : e.type);
      });
  }
  const page1 = resolvedTypes();
  check(page1.indexOf('user') === -1,
    'no dangling fragment renders as a USER tile', page1);
  const firstRendered = client.win.SessionDisplay
    .buildAll(store.entries, store.localEntries)[0];
  const firstEntry = client.win.SessionDisplay
    .resolve(firstRendered, store.entries, store.localEntries);
  checkEqual(firstEntry && firstEntry.content, 'verdict: green',
    'rendering starts at the window\'s first anchor');
  const fetchesBefore = client.fetchLog.length;
  await client.flush(2);
  checkEqual(client.fetchLog.length, fetchesBefore,
    'held entries trigger NO fetch of their own');

  // Scroll up: the owning flow arrives; held entries render exactly once.
  while (store.hasMoreHistory) { await viewer.loadOlder(); await client.flush(2); }
  const fullDisplay = client.win.SessionDisplay.buildAll(store.entries, store.localEntries);
  const keys = fullDisplay.map((d) => d.key);
  checkEqual(keys.length, new Set(keys).size, 'every entry renders exactly once');
  const types = resolvedTypes();
  checkEqual(types[0], 'user', 'full flow renders from the true first anchor');
  checkEqual(bufferRefs(client), fixture.expectedRefs(),
    'buffer equals the file-set oracle');
  // Attribution identity: the merged scroll-back display equals a
  // fresh full-buffer build entry-for-entry (byte-identical attribution
  // to the live rendering of the same lines).
  const oracleDisplay = client.win.SessionDisplay.buildAll(store.entries, store.localEntries);
  checkEqual(fullDisplay.map((d) => d.key), oracleDisplay.map((d) => d.key),
    'scroll-back display identical to the full-buffer build');
  assertNoLies(client, '(k)');
  viewer.destroy();
}

// (l) forced descriptor race — the transient display desync
//     (operator screenshots IMG_2111-2114): a mid-buffer merge lands
//     BETWEEN descriptor build and paint. Paint-time resolution must be
//     identity-checked: every stale descriptor resolves BY REF to the
//     CORRECT entry wherever it moved (never the entry that slid into
//     its index), and any unresolvable target paints as MISSING, never
//     wrong — including group-slice membership.
async function testL_forcedDescriptorRace() {
  console.log('\n── (l) forced race: merge between descriptor build and paint ──');
  const fixture = new FixtureSession();
  fixture.appendTyped({ type: 'user', role: 'user', content: 'start' });
  fixture.appendLine('assistant flow');
  for (let i = 0; i < 3; i++) {
    fixture.appendTyped({ type: 'tool_use', role: 'assistant', tool_name: 'Bash',
      tool_id: 'L' + i, input: { command: 'x' + i } });
  }
  fixture.appendLine('tail message');

  const client = makeClient(fixture);
  const viewer = await mountViewer(client);
  const store = client.win.getSessionStore(SID);
  const SD = client.win.SessionDisplay;

  // Paint 1: descriptors built against the current buffer.
  const display = SD.buildAll(store.entries, store.localEntries);
  const expectByKey = {};
  for (const d of display) {
    const e = SD.resolve(d, store.entries, store.localEntries, store._byRef);
    expectByKey[d.key] = d.type === 'group' ? 'tool_group' : (e && e.type);
  }

  // THE RACE: a mid-buffer merge (an older page arriving) shifts every
  // index under the already-built descriptors — no rebuild yet.
  const older = [];
  for (let i = 0; i < 4; i++) {
    older.push({ type: 'assistant_text', role: 'assistant',
      content: 'older-' + i, timestamp: '2026-08-10T11:00:0' + i + 'Z',
      entry_ref: { file: 'A', off: -1000 + i * 10, sub: 0 } });
  }
  client.win.mergeSessionEntries(store, { chain: ['A'], entries: older }, 'fetch');

  // Paint 2: the STALE descriptors paint against the shifted buffer.
  // Singles must resolve BY REF to their exact entry; groups may degrade
  // to MISSING (their index window shifted) but never to WRONG.
  let wrong = 0, singleMissing = 0, groupMissing = 0;
  for (const d of display) {
    const e = SD.resolve(d, store.entries, store.localEntries, store._byRef);
    if (!e || e.type === '__stale__') {
      if (d.type === 'group') groupMissing++; else singleMissing++;
      continue;
    }
    const got = e.type;
    if (got !== expectByKey[d.key]) {
      wrong++;
      if (wrong <= 3) console.log('   WRONG TILE:', d.key, 'expected', expectByKey[d.key], 'got', got);
    }
    if (d.type !== 'group' && e.entry_ref) {
      const k = e.entry_ref.file + ':' + e.entry_ref.off + ':' + (e.entry_ref.sub || 0);
      if (k !== d.key) wrong++;
    }
  }
  checkEqual(wrong, 0,
    'stale descriptors resolve to their CORRECT entries after the mid-buffer shift');
  checkEqual(singleMissing, 0,
    'no single tile goes missing while its entry exists (byRef fallback)');
  checkEqual(groupMissing, 0,
    'groups resolve their immutable membership by ref even mid-shift');

  // Group membership: after the shift, the group's IMMUTABLE ref list
  // resolves to exactly its original members — never an index-range
  // re-read.
  const groupD = display.find((d) => d.type === 'group');
  check(!!groupD, 'fixture produced a tool group');
  if (groupD) {
    const resolved = SD.resolve(groupD, store.entries, store.localEntries, store._byRef);
    checkEqual(resolved.type, 'tool_group', 'group resolves mid-shift');
    const memberKeys = (resolved.items || []).map((it) =>
      it.entry_ref.file + ':' + it.entry_ref.off + ':' + (it.entry_ref.sub || 0));
    checkEqual(memberKeys, groupD.refs || [], 'group items are exactly the built membership refs');
  }

  // Unknown-key descriptor (target gone entirely) → STALE sentinel.
  const phantom = SD.resolve({ idx: 2, key: 'A:999999:0' }, store.entries, store.localEntries, store._byRef);
  checkEqual(phantom.type, '__stale__', 'a vanished target paints as MISSING, never as whatever holds its index');

  assertNoLies(client, '(l)');
  viewer.destroy();
}

// (m) round-3 codex pin, their exact shape: two SAME-TOOL groups (X and
//     G) separated by user/assistant boundaries; a same-tool intruder
//     merges between descriptor build and paint, landing inside G's old
//     numeric interval. Membership must be asserted BY MEMBER REFS, not
//     item types — the interval semantics painted the intruder under
//     G's chip (WRONG, invisible to any type/tool filter).
async function testM_sameToolGroupIntruder() {
  console.log('\n── (m) same-tool intruder vs immutable group membership ──');
  const fixture = new FixtureSession();
  fixture.appendTyped({ type: 'user', role: 'user', content: 'flow one' });
  const x0 = fixture.appendTyped({ type: 'tool_use', role: 'assistant', tool_name: 'Bash',
    tool_id: 'X0', input: { command: 'x0' } });
  fixture.appendTyped({ type: 'tool_use', role: 'assistant', tool_name: 'Bash',
    tool_id: 'X1', input: { command: 'x1' } });
  fixture.appendLine('between the flows');
  fixture.appendTyped({ type: 'user', role: 'user', content: 'flow two' });
  const g0 = fixture.appendTyped({ type: 'tool_use', role: 'assistant', tool_name: 'Bash',
    tool_id: 'G0', input: { command: 'g0' } });
  fixture.appendTyped({ type: 'tool_use', role: 'assistant', tool_name: 'Bash',
    tool_id: 'G1', input: { command: 'g1' } });
  fixture.appendLine('tail');

  const client = makeClient(fixture);
  const viewer = await mountViewer(client);
  const store = client.win.getSessionStore(SID);
  const SD = client.win.SessionDisplay;

  // Paint 1: two Bash groups with distinct membership.
  const display = SD.buildAll(store.entries, store.localEntries);
  const groups = display.filter((d) => d.type === 'group');
  checkEqual(groups.length, 2, 'two same-tool groups built');
  const [gX, gG] = groups;
  checkEqual((gX.refs || []).length, 2, 'X group has exactly its two members');
  checkEqual((gG.refs || []).length, 2, 'G group has exactly its two members');

  // THE RACE: a same-tool Bash intruder from ANOTHER flow merges in,
  // positioned just before G0 — under interval semantics it lands
  // inside gG's stale start..end and the tool filter cannot reject it.
  const g0Entry = store.entries.find((e) => e.tool_id === 'G0');
  const intruderRef = { file: 'A', off: g0Entry.entry_ref.off - 1, sub: 0 };
  client.win.mergeSessionEntries(store, { chain: ['A'], entries: [{
    type: 'tool_use', role: 'assistant', tool_name: 'Bash', tool_id: 'INTRUDER',
    input: { command: 'evil' }, timestamp: '2026-08-10T12:30:00Z',
    entry_ref: intruderRef,
  }] }, 'sse');

  // Paint 2 with the STALE descriptors: assert MEMBER REFS, not types.
  for (const [label, gd] of [['X', gX], ['G', gG]]) {
    const resolved = SD.resolve(gd, store.entries, store.localEntries, store._byRef);
    checkEqual(resolved.type, 'tool_group', label + ' group still resolves');
    const memberKeys = (resolved.items || []).map((it) =>
      it.entry_ref.file + ':' + it.entry_ref.off + ':' + (it.entry_ref.sub || 0));
    checkEqual(memberKeys, gd.refs || [],
      label + ' group paints EXACTLY its built membership refs (intruder excluded)');
    check(!(resolved.items || []).some((it) => it.tool_id === 'INTRUDER'),
      label + ' group never paints the same-tool intruder');
  }
  assertNoLies(client, '(m)');
  viewer.destroy();
}

(async () => {
  try {
    await testA_withheldBroadcasts();
    await testB_zombieStream();
    await testC_supersededCursor();
    await testD_scrollUpDuringCatchup();
    await testE_noiseRegionPaging();
    await testF_partialLineColdOpen();
    await testG_mergeFailureBeforeCommit();
    await testH_stalledContinuationBacksOff();
    await testI_prunedPredecessorChain();
    await testJ_racingCatchupsMonotonic();
    await testK_anchorRuleAgentTranscript();
    await testL_forcedDescriptorRace();
    await testM_sameToolGroupIntruder();
    await testN_liveEntryCount();
  } catch (e) {
    console.error('HARNESS ERROR:', e);
    process.exit(2);
  }
  console.log(`\n${_passed} passed, ${_failed} failed`);
  process.exit(_failed === 0 ? 0 : 1);
})();
