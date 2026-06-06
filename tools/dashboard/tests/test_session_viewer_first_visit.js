/**
 * auto-cq7yd: Session-viewer first-visit head/tail inversion regression.
 *
 * SPA boot creates a session store with `_loading=false`, so SSE deltas
 * for sessions whose viewer hasn't mounted yet land directly in
 * `store.entries[]`. When the viewer later mounts and runs its initial
 * `_fetchBacklog`, the chronological fetch batch was being appended
 * *after* the pre-existing SSE entries, inverting head and tail —
 * `entries[0]` carried the latest SSE delivery while `entries[N-1]`
 * carried an older fetched entry.
 *
 * Fix: the first-visit branch of session-viewer.js clears
 * `store.entries`, `_seenIdentities`, `toolMap`, `resultMap`, and
 * `_pendingSSE` before flipping `_loading=true` and starting the fetch.
 *
 * These tests verify:
 *   1. Pre-fetch SSE entries do not invert head/tail of the rendered list.
 *   2. SSE entries that arrive *during* the fetch (buffered into
 *      `_pendingSSE`) drain afterwards and stay newer than the fetch's
 *      last entry — chronology preserved end-to-end.
 *   3. `_seenIdentities` is reset, so a tool_use entry that the fetch
 *      re-delivers is not silently deduped against a pre-clear SSE write.
 *
 * Run: node --test tools/dashboard/tests/test_session_viewer_first_visit.js
 */
const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const STORE_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/session-store.js');
const RENDERER_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/session-renderer.js');
const VIEWER_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/pages/session-viewer.js');

function flush() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function makeHarness(fetchHandlers) {
  const docListeners = {};
  const winListeners = {};
  const components = {};
  const stores = {};
  const fetchCalls = [];

  const document = {
    visibilityState: 'visible',
    addEventListener(name, cb) {
      (docListeners[name] ||= []).push(cb);
    },
    removeEventListener(name, cb) {
      const list = docListeners[name] || [];
      const idx = list.indexOf(cb);
      if (idx >= 0) list.splice(idx, 1);
    },
  };

  const handlers = {};
  const windowObj = {
    SessionRenderer: {},
    addEventListener(name, cb) {
      (winListeners[name] ||= []).push(cb);
    },
    removeEventListener(name, cb) {
      const list = winListeners[name] || [];
      const idx = list.indexOf(cb);
      if (idx >= 0) list.splice(idx, 1);
    },
    ensureSessionMessages() {},
    reconnectEvents() {},
    registerHandler(name, cb) { handlers[name] = cb; },
    unregisterHandler(name) { delete handlers[name]; },
    _es: { readyState: 1 },
  };

  const alpine = {
    data(name, factory) {
      components[name] = factory;
    },
    store(name, obj) {
      if (obj !== undefined) {
        stores[name] = obj;
        return obj;
      }
      return stores[name];
    },
  };

  const fetchFn = (url, options) => {
    fetchCalls.push({ url, options: options || null });
    if (fetchHandlers && fetchHandlers[url]) {
      return Promise.resolve(fetchHandlers[url]());
    }
    if (fetchHandlers) {
      for (const key of Object.keys(fetchHandlers)) {
        if (url.startsWith(key)) {
          return Promise.resolve(fetchHandlers[key](url, options));
        }
      }
    }
    if (url === '/api/dao/active_sessions') {
      return Promise.resolve({ json: () => Promise.resolve([]) });
    }
    if (url.startsWith('/api/session/') && !url.includes('/tail?')) {
      // Org-backfill probe (line 286 of session-viewer.js): be permissive.
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    }
    throw new Error('unexpected fetch ' + url);
  };

  const sandbox = {
    window: windowObj,
    document,
    Alpine: alpine,
    fetch: fetchFn,
    console,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    Promise,
    JSON,
    Object,
    Array,
    Map,
    Set,
    Date,
    Error,
    parseInt,
    parseFloat,
  };
  sandbox.window.document = document;
  sandbox.window.Alpine = alpine;
  sandbox.window.fetch = fetchFn;
  vm.createContext(sandbox);

  let storeSrc = fs.readFileSync(STORE_JS, 'utf8');
  storeSrc = storeSrc.replace(
    'setTimeout(ensureSessionMessages, 0);',
    'setTimeout(window.ensureSessionMessages, 0);'
  );
  vm.runInContext(storeSrc, sandbox, { filename: 'session-store.js' });
  vm.runInContext(fs.readFileSync(RENDERER_JS, 'utf8'), sandbox, { filename: 'session-renderer.js' });
  vm.runInContext(fs.readFileSync(VIEWER_JS, 'utf8'), sandbox, { filename: 'session-viewer.js' });

  for (const cb of (docListeners['alpine:init'] || [])) cb();

  function makeViewer() {
    const factory = components.sessionViewerPage;
    assert.equal(typeof factory, 'function');
    const viewer = factory({});
    viewer.$watch = function() { return function() {}; };
    viewer.$nextTick = function(fn) { if (typeof fn === 'function') fn(); };
    viewer.$refs = {};
    viewer._rebuildDisplay = function() {};
    viewer._scrollToBottom = function() {};
    viewer._setupWatchers = function() {};
    return viewer;
  }

  return {
    window: windowObj,
    document,
    stores,
    fetchCalls,
    makeViewer,
    handlers,
    emitDocument(name, event) {
      for (const cb of (docListeners[name] || [])) cb(event);
    },
    emitWindow(name, event) {
      for (const cb of (winListeners[name] || [])) cb(event);
    },
  };
}

function entryAt(ts, content) {
  return { type: 'assistant_text', content, timestamp: ts };
}

describe('session viewer first-visit head/tail inversion (auto-cq7yd)', () => {
  it('clears pre-fetch SSE entries so the chronological backlog renders in order', async () => {
    // Mock fetch to return entries chronologically older than the SSE one.
    const tailUrl = '/api/session/autonomy/auto-test/tail?tail_lines=200';
    const fetchedEntries = [
      entryAt('2026-01-01T09:55:00Z', 'fetch-old-1'),
      entryAt('2026-01-01T09:56:00Z', 'fetch-old-2'),
      entryAt('2026-01-01T09:58:00Z', 'fetch-old-last'),
    ];
    const h = makeHarness({
      [tailUrl]: () => ({
        ok: true,
        status: 200,
        json: () => Promise.resolve({
          entries: fetchedEntries,
          offset: 30,
          is_live: true,
          seq: 3,
          resolved: true,
          type: 'container',
          role: 'builder',
        }),
      }),
    });

    // SPA-boot path: store is created by getSessionStore (or active_sessions
    // seed). _loading defaults to false, so SSE deltas land directly.
    const store = h.window.getSessionStore('auto-test');
    assert.equal(store._loading, false);
    assert.equal(store.loaded, false);

    // Simulate session:registry → session:messages SSE arriving before the
    // viewer mounts. The latest SSE timestamp is *newer* than the fetch's
    // last entry — this is the inversion trigger.
    h.window.appendSessionEntries(store, {
      seq: 42,
      entries: [entryAt('2026-01-01T10:00:00Z', 'sse-pre-nav-newer')],
    }, 'sse');
    assert.equal(store.entries.length, 1);
    assert.equal(store.entries[0].content, 'sse-pre-nav-newer');

    // Mount the viewer for the same session — first-visit branch runs.
    const viewer = h.makeViewer();
    await viewer.configure({ sessionId: 'auto-test', project: 'autonomy' });
    await flush();

    // After the fix:
    //   1. The pre-nav SSE entry is wiped before the fetch lands.
    //   2. entries are exactly the fetch batch in chronological order.
    //   3. entries[0].timestamp <= entries[N-1].timestamp (no inversion).
    assert.equal(store.entries.length, fetchedEntries.length,
      'pre-nav SSE entry should not survive the first-visit clear');
    assert.equal(store.entries[0].content, 'fetch-old-1');
    assert.equal(store.entries[store.entries.length - 1].content, 'fetch-old-last');
    assert.ok(
      store.entries[0].timestamp <= store.entries[store.entries.length - 1].timestamp,
      'first-visit clear must leave the rendered array chronological'
    );
    assert.equal(store.loaded, true);
    assert.equal(store._loading, false);

    viewer.destroy();
  });

  it('drains pendingSSE arriving during the fetch and keeps chronology', async () => {
    const tailUrl = '/api/session/autonomy/auto-test/tail?tail_lines=200';

    // We need to inject an SSE event *while_fetchBacklog is awaiting*. Stash
    // a hook on the harness window; the fake fetch resolves on the next tick
    // so we can fire the SSE event before the fetch promise settles.
    let sseDuringFetchFired = false;

    const h = makeHarness({
      [tailUrl]: () => ({
        ok: true,
        status: 200,
        json: () => new Promise((resolve) => {
          // Fire an SSE event while the viewer's fetchBacklog is awaiting.
          // _loading=true at this point, so it should land in _pendingSSE.
          setTimeout(() => {
            const data = {
              session_id: 'auto-test',
              seq: 100,
              entries: [entryAt('2026-01-01T10:05:00Z', 'sse-during-fetch')],
            };
            if (h.handlers['session:messages']) {
              h.handlers['session:messages'](data);
              sseDuringFetchFired = true;
            }
            resolve({
              entries: [
                entryAt('2026-01-01T09:55:00Z', 'fetch-1'),
                entryAt('2026-01-01T09:58:00Z', 'fetch-2'),
              ],
              offset: 20,
              is_live: true,
              seq: 2,
            });
          }, 0);
        }),
      }),
    });

    const store = h.window.getSessionStore('auto-test');
    // Pre-nav SSE delivery (will be wiped by first-visit clear).
    h.window.appendSessionEntries(store, {
      seq: 42,
      entries: [entryAt('2026-01-01T10:00:00Z', 'sse-pre-nav')],
    }, 'sse');
    assert.equal(store.entries.length, 1);

    const viewer = h.makeViewer();
    await viewer.configure({ sessionId: 'auto-test', project: 'autonomy' });
    await flush();

    assert.equal(sseDuringFetchFired, true, 'mid-fetch SSE event must fire');

    // Final order: 2 fetched chronological entries + 1 SSE event that
    // arrived during the fetch and drained at the end. No leakage from
    // the pre-nav SSE write (which was newer than fetch-2 and would have
    // inverted head/tail without the clear).
    assert.equal(store.entries.length, 3);
    assert.equal(store.entries[0].content, 'fetch-1');
    assert.equal(store.entries[1].content, 'fetch-2');
    assert.equal(store.entries[2].content, 'sse-during-fetch');
    assert.ok(
      store.entries[0].timestamp <= store.entries[2].timestamp,
      'array stays chronological after pendingSSE drain'
    );

    viewer.destroy();
  });

  it('resets _seenIdentities so post-fetch entries are not deduped against pre-clear SSE writes', async () => {
    const tailUrl = '/api/session/autonomy/auto-test/tail?tail_lines=200';
    const toolEntry = {
      type: 'tool_use',
      tool_id: 'tool_abc',
      tool_name: 'Bash',
      content: 'fetch-version',
      timestamp: '2026-01-01T09:55:00Z',
    };
    const h = makeHarness({
      [tailUrl]: () => ({
        ok: true,
        status: 200,
        json: () => Promise.resolve({
          entries: [toolEntry],
          offset: 10,
          is_live: true,
          seq: 1,
        }),
      }),
    });

    const store = h.window.getSessionStore('auto-test');
    // Pre-nav SSE delivers a tool_use with the same tool_id. Without the
    // first-visit identity reset, the fetch's copy of this tool_use would
    // be silently deduped — the user never sees the canonical fetch entry.
    h.window.appendSessionEntries(store, {
      seq: 5,
      entries: [{
        type: 'tool_use',
        tool_id: 'tool_abc',
        tool_name: 'Bash',
        content: 'sse-version',
        timestamp: '2026-01-01T10:00:00Z',
      }],
    }, 'sse');
    assert.equal(store.entries.length, 1);
    assert.ok(store._seenIdentities && store._seenIdentities['tu:tool_abc'],
      'pre-clear SSE write must register identity');

    const viewer = h.makeViewer();
    await viewer.configure({ sessionId: 'auto-test', project: 'autonomy' });
    await flush();

    // The fetch's tool_use must land — not be filtered as a dedup hit.
    assert.equal(store.entries.length, 1);
    assert.equal(store.entries[0].content, 'fetch-version',
      'fetched tool_use must not be silently merged into the cleared SSE entry');
    assert.equal(store._dedupCollisionsCount || 0, 0,
      'no dedup collisions expected after first-visit clear');

    viewer.destroy();
  });

  it('cached re-render path (store.loaded === true) does not clear entries', async () => {
    // Re-navigating to a session whose store is already fully loaded should
    // hit the cached path — no clear, no fetch.
    const h = makeHarness({});
    const store = h.window.getSessionStore('auto-test');
    h.window.appendSessionEntries(store, {
      seq: 10,
      entries: [
        entryAt('2026-01-01T09:00:00Z', 'cached-1'),
        entryAt('2026-01-01T09:05:00Z', 'cached-2'),
      ],
    }, 'fetch');
    store.loaded = true;
    const beforeLen = store.entries.length;
    const beforeFirst = store.entries[0].content;

    const viewer = h.makeViewer();
    await viewer.configure({ sessionId: 'auto-test', project: 'autonomy' });
    await flush();

    // No tail fetch should have been issued, and entries are untouched.
    const tailFetches = h.fetchCalls.filter((c) => c.url.includes('/tail?after='));
    assert.equal(tailFetches.length, 0, 'cached path must not refetch');
    assert.equal(store.entries.length, beforeLen);
    assert.equal(store.entries[0].content, beforeFirst);

    viewer.destroy();
  });

  it('hydrates uploaded screenshots into an already-loaded timeline chronologically', async () => {
    const h = makeHarness({});
    const store = h.window.getSessionStore('auto-test');
    h.window.appendSessionEntries(store, {
      seq: 10,
      entries: [
        entryAt('2026-01-01T09:00:00Z', 'older-turn'),
        entryAt('2026-01-01T10:00:00Z', 'newer-turn'),
      ],
    }, 'fetch');
    store.loaded = true;

    h.window.Schema = {
      of: async (setId) => {
        assert.equal(setId, 'dashboard.session.upload');
        return {
          all: async () => [
            {
              key: 'upload-a',
              payload: {
                target_session: 'auto-test',
                rel_path: '.uploads/a.png',
                filename: 'a.png',
                mime: 'image/png',
                size: 10,
                timestamp: '2026-01-01T09:30:00Z',
              },
            },
            {
              key: 'upload-b',
              payload: {
                target_session: 'auto-test',
                rel_path: '.uploads/b.png',
                filename: 'b.png',
                mime: 'image/png',
                size: 20,
                timestamp: '2026-01-01T09:30:00Z',
              },
            },
          ],
          onChange: () => () => {},
        };
      },
    };

    const viewer = h.makeViewer();
    viewer.sessionKey = 'auto-test';
    viewer._tmuxSession = 'auto-test';

    await viewer._initUploads();

    assert.equal(store.entries.length, 4);
    assert.equal(store.entries[0].content, 'older-turn');
    assert.equal(store.entries[1].rel_path, '.uploads/a.png');
    assert.equal(store.entries[2].rel_path, '.uploads/b.png');
    assert.equal(store.entries[3].content, 'newer-turn');
    assert.equal(store._pendingAttachments.length, 0);
    assert.equal(store._displayDirty, true,
      'mid-list upload insertion must force a full display rebuild');

    viewer.destroy();
  });

  it('hydrates corrections with no-store and retries after a new correction event', async () => {
    const h = makeHarness({
      '/api/session/auto-test/turn-corrections': (url, options) => ({
        ok: true,
        json: () => Promise.resolve({
          corrections: url.includes('?_=') ? [{
            target_message_id: 'msg-1',
            status: 'pending',
            corrected_text: 'fixed',
          }] : [],
        }),
      }),
    });

    const viewer = h.makeViewer();
    viewer.sessionKey = 'auto-test';

    await viewer._hydrateCorrections({ fresh: true });
    const correctionCalls = h.fetchCalls.filter((c) =>
      c.url.startsWith('/api/session/auto-test/turn-corrections')
    );
    assert.equal(correctionCalls.length, 1);
    assert.ok(
      correctionCalls[0].url.startsWith('/api/session/auto-test/turn-corrections?_='),
      'fresh correction hydration should bust the URL cache key'
    );
    assert.equal(correctionCalls[0].options.cache, 'no-store');
    assert.equal(correctionCalls[0].options.headers['Cache-Control'], 'no-cache');
    assert.equal(viewer._corrections['msg-1'].corrected_text, 'fixed');

    const callsBeforeRetry = correctionCalls.length;
    viewer._refreshCorrectionsForNewEvent();
    await new Promise((resolve) => setTimeout(resolve, 450));
    const correctionCallsAfterRetry = h.fetchCalls.filter((c) =>
      c.url.startsWith('/api/session/auto-test/turn-corrections')
    );
    assert.ok(
      correctionCallsAfterRetry.length >= callsBeforeRetry + 2,
      'new correction events should trigger an immediate refresh and one delayed retry'
    );

    viewer.destroy();
  });

  it('rehydrates to terminal state when a stale second viewer gets a 409 on accept', async () => {
    let correctionState = 'pending';
    const h = makeHarness({
      '/api/session/auto-test/turn-corrections': () => ({
        ok: true,
        json: () => Promise.resolve({
          corrections: [{
            target_message_id: 'msg-1',
            status: correctionState,
            original_sha256: 'sha-1',
            corrected_text: 'fixed',
          }],
        }),
      }),
      '/api/session/auto-test/turn-corrections/msg-1/accept': () => ({
        ok: false,
        status: 409,
        json: () => Promise.resolve({
          error: 'correction already terminal',
          correction: {
            target_message_id: 'msg-1',
            status: 'accepted',
            original_sha256: 'sha-1',
            corrected_text: 'fixed',
          },
        }),
      }),
    });

    const viewer = h.makeViewer();
    viewer.sessionKey = 'auto-test';
    await viewer._hydrateCorrections({ fresh: true });
    assert.equal(viewer._corrections['msg-1'].status, 'pending');

    correctionState = 'accepted';
    await viewer.acceptCorrection({ message_id: 'msg-1' });

    assert.equal(
      viewer._corrections['msg-1'].status,
      'accepted',
      'stale viewer should rehydrate to the terminal state instead of snapping back to pending'
    );

    viewer.destroy();
  });

  it('accepted marker toggles between corrected and original text locally', async () => {
    const h = makeHarness({});
    const viewer = h.makeViewer();
    const entry = {
      type: 'user',
      message_id: 'msg-accepted',
      content: 'raw original text',
    };

    viewer._corrections = {
      'msg-accepted': {
        target_message_id: 'msg-accepted',
        status: 'accepted',
        corrected_text: 'corrected display text',
      },
    };

    assert.equal(viewer.correctionDisplayText(entry), 'corrected display text');
    assert.equal(viewer.isShowingRawCorrection(entry), false);

    viewer.toggleAcceptedCorrectionPreview(entry);
    assert.equal(viewer.isShowingRawCorrection(entry), true);
    assert.equal(viewer.correctionDisplayText(entry), 'raw original text');

    viewer.toggleAcceptedCorrectionPreview(entry);
    assert.equal(viewer.isShowingRawCorrection(entry), false);
    assert.equal(viewer.correctionDisplayText(entry), 'corrected display text');

    viewer.destroy();
  });
});
