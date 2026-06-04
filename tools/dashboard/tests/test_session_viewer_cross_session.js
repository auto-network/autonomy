// #23 — cross-session dictation awareness.
// When the operator views session A but voice is bound to session B, the viewer
// must flag it: _crossSessionDictation true, _crossSessionText mirrors the live
// buffer, _crossSessionTargetTitle names B, and the body class is set so the
// violet send-fill / cross tile light up. Same-session or unbound => false.

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const VIEWER_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/pages/session-viewer.js');

function makeViewer() {
  const components = {};
  const stores = {};
  const bodyClasses = new Set();

  const alpine = {
    data(name, factory) { components[name] = factory; },
    store(name, obj) {
      if (obj !== undefined) { stores[name] = obj; return obj; }
      return stores[name];
    },
    effect() {},
  };
  const docListeners = {};
  const document = {
    addEventListener(name, cb) { (docListeners[name] ||= []).push(cb); },
    removeEventListener() {},
    body: {
      dataset: {},
      classList: {
        add: (c) => bodyClasses.add(c),
        remove: (c) => bodyClasses.delete(c),
        toggle: (c, on) => { if (on) bodyClasses.add(c); else bodyClasses.delete(c); },
        contains: (c) => bodyClasses.has(c),
      },
    },
  };

  const sandbox = {
    console, setTimeout, clearTimeout, Promise, JSON, Object, Array, Map, Set, Date,
    window: { SessionRenderer: {}, Autonomy: {}, addEventListener() {}, removeEventListener() {} },
  };
  sandbox.window.document = document;
  sandbox.window.Alpine = alpine;
  sandbox.globalThis = sandbox;
  sandbox.Alpine = alpine;
  sandbox.document = document;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(VIEWER_JS, 'utf8'), sandbox, { filename: 'session-viewer.js' });
  for (const cb of (docListeners['alpine:init'] || [])) cb();

  const viewer = components.sessionViewerPage({});
  viewer.$watch = () => () => {};
  viewer.$nextTick = (fn) => { if (typeof fn === 'function') fn(); };
  viewer.$refs = {};

  // _tmuxSession is sessionKey; isLive comes from the sessions store. Set up so
  // _composerActive is true: live, non-host, has a tmux session.
  viewer.sessionKey = 'auto-A';
  viewer.showTerminal = false;
  viewer.sessionType = 'tmux';

  stores.voice = { enabled: true, boundSessionId: '', bufferText: '' };
  stores.sessions = { 'auto-A': { isLive: true } };

  return { viewer, stores, bodyClasses };
}

describe('#23 cross-session dictation getters', () => {
  it('is false when voice is bound to the SAME session being viewed', () => {
    const { viewer, stores } = makeViewer();
    stores.voice.boundSessionId = 'auto-A';
    stores.voice.bufferText = 'hello';
    assert.equal(viewer._crossSessionDictation, false);
  });

  it('is false when voice is unbound', () => {
    const { viewer, stores } = makeViewer();
    stores.voice.boundSessionId = '';
    assert.equal(viewer._crossSessionDictation, false);
  });

  it('is true when bound to a DIFFERENT live session, with text + target title', () => {
    const { viewer, stores } = makeViewer();
    stores.voice.boundSessionId = 'auto-B';
    stores.voice.bufferText = 'route this elsewhere';
    stores.sessions['auto-B'] = { label: 'Enterprise NG' };

    assert.equal(viewer._crossSessionDictation, true);
    assert.equal(viewer._crossSessionText, 'route this elsewhere');
    assert.equal(viewer._crossSessionTargetTitle, 'Enterprise NG');
  });

  it('falls back to the bound id when the target session has no label', () => {
    const { viewer, stores } = makeViewer();
    stores.voice.boundSessionId = 'auto-B';
    assert.equal(viewer._crossSessionTargetTitle, 'auto-B');
  });

  it('is false when voice is disabled even if bound elsewhere', () => {
    const { viewer, stores } = makeViewer();
    stores.voice.enabled = false;
    stores.voice.boundSessionId = 'auto-B';
    assert.equal(viewer._crossSessionDictation, false);
  });

  it('_syncComposerSignal mirrors the cross-session flag onto the body class', () => {
    const { viewer, stores, bodyClasses } = makeViewer();
    stores.voice.boundSessionId = 'auto-B';
    stores.voice.bufferText = 'x';
    viewer._syncComposerSignal();
    assert.equal(bodyClasses.has('sv-cross-session-dictation'), true);

    stores.voice.boundSessionId = 'auto-A';
    viewer._syncComposerSignal();
    assert.equal(bodyClasses.has('sv-cross-session-dictation'), false);
  });
});
