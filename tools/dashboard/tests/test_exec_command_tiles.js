const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const STORE_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/session-store.js');
const RENDERER_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/session-renderer.js');

function makeHarness() {
  const docListeners = {};
  const doc = {
    addEventListener(name, cb) {
      (docListeners[name] ||= []).push(cb);
    },
  };

  const stores = {};
  const alpine = {
    store(name, obj) {
      if (obj !== undefined) {
        stores[name] = obj;
        return obj;
      }
      return stores[name];
    },
  };

  const fetchFn = (url) => {
    if (url === '/api/dao/active_sessions') {
      return Promise.resolve({ json: () => Promise.resolve([]) });
    }
    return Promise.resolve({ json: () => Promise.resolve({}) });
  };

  const sandbox = {
    window: {},
    document: doc,
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
  vm.runInContext(fs.readFileSync(RENDERER_JS, 'utf8'), sandbox, { filename: 'session-renderer.js' });
  for (const cb of (docListeners['alpine:init'] || [])) cb();

  return {
    win: sandbox.window,
    alpine,
  };
}

function ref(off, sub) {
  return { file: 'f', off, sub: sub || 0 };
}

function makeExecUse(toolId, cmd, entryRef) {
  return {
    type: 'tool_use',
    role: 'assistant',
    tool_name: 'Bash',
    tool_id: toolId,
    input: {
      cmd,
      command: cmd,
      cwd: REPO_ROOT,
    },
    timestamp: '2026-04-23T20:00:00Z',
    ...(entryRef ? { entry_ref: entryRef } : {}),
  };
}

function makeExecResult(toolId, parsedCmd, content, extras = {}) {
  return {
    type: 'tool_result',
    role: 'tool',
    tool_id: toolId,
    result_kind: 'exec_command',
    content,
    parsed_cmd: parsedCmd,
    timestamp: '2026-04-23T20:00:01Z',
    exit_code: extras.exit_code === undefined ? 0 : extras.exit_code,
    status: extras.status || 'completed',
    duration_seconds: extras.duration_seconds === undefined ? 1.2 : extras.duration_seconds,
    command: extras.command || '',
    cwd: extras.cwd || REPO_ROOT,
    is_error: extras.is_error === undefined ? false : extras.is_error,
  };
}

function makeSemanticUse(toolId, toolName, input, extras = {}) {
  return {
    type: 'tool_use',
    role: 'assistant',
    tool_name: toolName,
    tool_id: toolId,
    input,
    timestamp: extras.timestamp || '',
    semantic_from_exec: true,
  };
}

function makeSemanticResult(toolId, content, extras = {}) {
  return {
    type: 'tool_result',
    role: 'tool',
    tool_id: toolId,
    result_kind: 'exec_command',
    content,
    timestamp: extras.timestamp || '2026-04-23T20:00:01Z',
    line_count: extras.line_count,
    semantic_from_exec: true,
  };
}

function makeRendererContext(win, entry, result) {
  const sessionId = 'session-test';
  win.getSessionStore(sessionId);
  return Object.assign({
    sessionKey: sessionId,
    entries: [],
    displayEntries: [],
    autoScroll: true,
    attachments: [],
    _expanded: {},
    _expandView: {},
    _groupExpanded: {},
    _groupExpandView: {},
    _resultMap: { [entry.tool_id]: result },
  }, win.SessionRenderer);
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

describe('semantic tool entry updates', () => {
  // The split-batch semantic upgrade rides the CALL line's entry_ref
  // (auto-16g9t: postprocess remembers use_refs, so the live path and a
  // cold re-read stamp the identical identity). These batches mirror the
  // exact refs the server emits.
  it('merges a duplicate tool_use update in place and marks a structural change when tool_name changes', () => {
    const h = makeHarness();
    const store = h.win.getSessionStore('sess-read-single');

    h.win.mergeSessionEntries(store, {
      chain: ['f'],
      entries: [
        makeExecUse('call_read', "sed -n '1,2p' tools/dashboard/server.py", ref(100)),
      ],
    });

    h.win.mergeSessionEntries(store, {
      chain: ['f'],
      entries: [
        Object.assign(makeSemanticUse('call_read', 'Read', { file_path: 'tools/dashboard/server.py' }), { entry_ref: ref(100) }),
        Object.assign(makeSemanticResult('call_read', 'line1\nline2\n', { line_count: 2 }), { entry_ref: ref(200) }),
      ],
    });

    assert.equal(store.entries.length, 2);
    assert.equal(store.entries[0].tool_name, 'Read');
    assert.deepStrictEqual(plain(store.entries[0].input), { file_path: 'tools/dashboard/server.py' });
    assert.equal(store.resultMap.call_read.line_count, 2);
    assert.equal(store._structureRev, store._mergeRev,
      'tool_name change must mark a structural display change');
  });

  it('appends extra semantic tool entries after an in-place update', () => {
    const h = makeHarness();
    const store = h.win.getSessionStore('sess-read-multi');

    h.win.mergeSessionEntries(store, {
      chain: ['f'],
      entries: [
        makeExecUse('call_multi', "sed -n '1,2p' tools/a.py && sed -n '5,7p' tools/b.py", ref(100)),
      ],
    });

    h.win.mergeSessionEntries(store, {
      chain: ['f'],
      entries: [
        Object.assign(makeSemanticUse('call_multi', 'Read', { file_path: 'tools/a.py' }), { entry_ref: ref(100, 0) }),
        Object.assign(makeSemanticUse('call_multi#2', 'Read', { file_path: 'tools/b.py' }, { timestamp: '2026-04-23T20:00:00Z' }), { entry_ref: ref(100, 1) }),
        Object.assign(makeSemanticResult('call_multi', 'a1\na2\n', { line_count: 2 }), { entry_ref: ref(200, 0) }),
        Object.assign(makeSemanticResult('call_multi#2', 'b1\nb2\nb3\n', { line_count: 3 }), { entry_ref: ref(200, 1) }),
      ],
    });

    const toolUses = store.entries.filter((entry) => entry.type === 'tool_use');
    const toolResults = store.entries.filter((entry) => entry.type === 'tool_result');

    assert.equal(toolUses.length, 2);
    assert.deepStrictEqual(Array.from(toolUses, (entry) => entry.tool_name), ['Read', 'Read']);
    assert.deepStrictEqual(Array.from(toolUses, (entry) => entry.input.file_path), ['tools/a.py', 'tools/b.py']);
    assert.equal(toolResults.length, 2);
    assert.equal(store.resultMap.call_multi.content, 'a1\na2\n');
    assert.equal(store.resultMap['call_multi#2'].content, 'b1\nb2\nb3\n');
    assert.equal(store.resultMap['call_multi#2'].line_count, 3);
    assert.equal(store.entries[0].timestamp, '2026-04-23T20:00:00Z');
  });

  it('updates an existing Bash tile into Grep without adding a duplicate tool_use', () => {
    const h = makeHarness();
    const store = h.win.getSessionStore('sess-grep');

    h.win.mergeSessionEntries(store, {
      chain: ['f'],
      entries: [
        makeExecUse('call_rg', "rg -n 'context_tokens' tools/dashboard -S", ref(100)),
      ],
    });

    h.win.mergeSessionEntries(store, {
      chain: ['f'],
      entries: [
        Object.assign(makeSemanticUse('call_rg', 'Grep', {
          pattern: 'context_tokens',
          path: 'tools/dashboard',
        }), { entry_ref: ref(100) }),
        Object.assign(makeSemanticResult('call_rg', 'tools/dashboard/server.py:1:context_tokens\n'), { entry_ref: ref(200) }),
      ],
    });

    assert.equal(store.entries.filter((entry) => entry.type === 'tool_use').length, 1);
    assert.equal(store.entries[0].tool_name, 'Grep');
    assert.deepStrictEqual(plain(store.entries[0].input), {
      pattern: 'context_tokens',
      path: 'tools/dashboard',
    });
  });
});

describe('exec_command meta badges', () => {
  it('shows a checkmark for Bash success', () => {
    const h = makeHarness();
    const entry = makeExecUse('call_ok', 'git status --short');
    const result = makeExecResult('call_ok', [], '', { exit_code: 0 });
    const ctx = makeRendererContext(h.win, entry, result);

    const badges = h.win.SessionRenderer.metaDisplay.call(ctx, entry);
    assert.equal(badges.some((badge) => badge.text === '\u2713'), true);
  });

  it('shows a red x for Bash failure', () => {
    const h = makeHarness();
    const entry = makeExecUse('call_fail', 'git diff --quiet');
    const result = makeExecResult('call_fail', [], '', { exit_code: 7, is_error: true });
    const ctx = makeRendererContext(h.win, entry, result);

    const badges = h.win.SessionRenderer.metaDisplay.call(ctx, entry);
    assert.equal(badges.some((badge) => badge.text === '\u2717'), true);
  });

  it('suppresses meaningless 0ms Bash durations', () => {
    const h = makeHarness();
    const entry = makeExecUse('call_zero', 'pwd');
    const result = makeExecResult('call_zero', [], '');
    result.timestamp = entry.timestamp;
    const ctx = makeRendererContext(h.win, entry, result);

    const badges = h.win.SessionRenderer.metaDisplay.call(ctx, entry);
    assert.equal(badges.some((badge) => badge.text === '0ms'), false);
  });

  it('uses explicit line_count for Read badges when semantic expansion has no output body', () => {
    const h = makeHarness();
    const entry = {
      type: 'tool_use',
      tool_id: 'read_empty',
      tool_name: 'Read',
      input: { file_path: 'tools/dashboard/server.py' },
      timestamp: '2026-04-23T20:00:00Z',
    };
    const result = {
      type: 'tool_result',
      tool_id: 'read_empty',
      content: '',
      line_count: 31,
      timestamp: '2026-04-23T20:00:01Z',
    };
    const ctx = makeRendererContext(h.win, entry, result);

    const badges = h.win.SessionRenderer.metaDisplay.call(ctx, entry);
    assert.deepStrictEqual(plain(badges), [{ text: '+31', cls: 'sc-meta-green' }]);
  });

  it('treats a running result as still running even after a progress update lands', () => {
    const h = makeHarness();
    const entry = makeExecUse('call_progress', 'pytest tools/dashboard/tests/test_worktrees.py -q');
    const result = makeExecResult('call_progress', [], 'bringing up nodes...\n.......', {
      status: 'running',
      duration_seconds: 5,
      exit_code: null,
    });
    const ctx = makeRendererContext(h.win, entry, result);
    const store = h.win.getSessionStore(ctx.sessionKey);
    store.activityState = 'tool_running';
    store.pendingToolIds = { call_progress: true };

    assert.equal(h.win.SessionRenderer.isToolRunning.call(ctx, entry), true);
    const badges = h.win.SessionRenderer.metaDisplay.call(ctx, entry);
    assert.equal(badges.some((badge) => badge.text === 'Running'), true);
    assert.equal(badges.some((badge) => badge.text === '2 lines'), true);
  });

  it('does not render a missing result as running when server activity is idle', () => {
    const h = makeHarness();
    const entry = makeExecUse('call_missing_result', 'rg isToolRunning tools/dashboard');
    const ctx = makeRendererContext(h.win, entry, undefined);
    const store = h.win.getSessionStore(ctx.sessionKey);
    store.activityState = 'idle';
    store.pendingToolIds = {};

    assert.equal(h.win.SessionRenderer.isToolRunning.call(ctx, entry), false);
    const badges = h.win.SessionRenderer.metaDisplay.call(ctx, entry);
    assert.equal(badges.some((badge) => badge.text === 'Running'), false);
    assert.equal(badges.some((badge) => badge.cls === 'sc-meta-running'), false);
  });

  it('renders a missing result as running only when the server marks that tool pending', () => {
    const h = makeHarness();
    const entry = makeExecUse('call_pending_result', 'rg isToolRunning tools/dashboard');
    const ctx = makeRendererContext(h.win, entry, undefined);
    const store = h.win.getSessionStore(ctx.sessionKey);
    store.activityState = 'tool_running';
    store.pendingToolIds = { call_pending_result: true };

    assert.equal(h.win.SessionRenderer.isToolRunning.call(ctx, entry), true);
    const badges = h.win.SessionRenderer.metaDisplay.call(ctx, entry);
    assert.equal(badges.some((badge) => badge.text === 'Running'), true);
  });

  it('does not allow a completed exec result to regress back to running', () => {
    const h = makeHarness();
    const store = h.win.getSessionStore('sess-exec-regression');

    h.win.mergeSessionEntries(store, {
      seq: 1,
      entries: [
        makeExecUse('call_exec_done', 'git status --short'),
        makeExecResult('call_exec_done', [], 'M tools/dashboard/session_harness.py\n', {
          status: 'completed',
          exit_code: 0,
        }),
      ],
    });

    h.win.mergeSessionEntries(store, {
      seq: 2,
      entries: [
        makeExecResult('call_exec_done', [], 'bringing up nodes...\n', {
          status: 'running',
          exit_code: null,
          duration_seconds: 5,
        }),
      ],
    });

    assert.equal(store.resultMap.call_exec_done.status, 'completed');
    assert.equal(store.resultMap.call_exec_done.exit_code, 0);
    const ctx = makeRendererContext(h.win, store.entries[0], store.resultMap.call_exec_done);
    assert.equal(h.win.SessionRenderer.isToolRunning.call(ctx, store.entries[0]), false);
  });

  it('renders a tool as finished when its local result is terminal', () => {
    const h = makeHarness();
    const entry = makeExecUse('call_terminal_wins', 'graph turn-correction suggest x --json');
    const result = makeExecResult('call_terminal_wins', [], '{"type":"turn_correction"}\n', {
      status: 'completed',
      exit_code: 0,
    });
    const ctx = makeRendererContext(h.win, entry, result);
    const store = h.win.getSessionStore(ctx.sessionKey);
    store.activityState = 'tool_running';
    store.pendingToolIds = { call_terminal_wins: true };

    assert.equal(h.win.SessionRenderer.isToolRunning.call(ctx, entry), false);
  });

  it('shows detached instead of running for a dead session with a lingering running result', () => {
    const h = makeHarness();
    const entry = makeExecUse('call_detached', 'python3 -m uvicorn tools.dashboard.server:app');
    const result = makeExecResult('call_detached', [], 'Uvicorn running\n', {
      status: 'running',
      exit_code: null,
      duration_seconds: 30,
    });
    const ctx = makeRendererContext(h.win, entry, result);
    h.win.getSessionStore(ctx.sessionKey).activityState = 'dead';

    assert.equal(h.win.SessionRenderer.isToolRunning.call(ctx, entry), false);
    const badges = h.win.SessionRenderer.metaDisplay.call(ctx, entry);
    assert.equal(badges.some((badge) => badge.text === 'Detached'), true);
    assert.equal(badges.some((badge) => badge.text === 'Running'), false);
  });
});
