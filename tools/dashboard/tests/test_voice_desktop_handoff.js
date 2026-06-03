const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const VIEWER_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/pages/session-viewer.js');

function loadViewer(opts) {
  const docListeners = {};
  const components = {};
  const previewCalls = [];
  const execCalls = [];
  const sessionStores = {};
  const width = (opts && opts.width) || 1280;
  const height = (opts && opts.height) || 900;
  const selection = {
    removeAllRanges() {},
    addRange(range) {
      this.range = range;
    },
  };

  const document = {
    activeElement: null,
    addEventListener(name, cb) {
      (docListeners[name] ||= []).push(cb);
    },
    createRange() {
      return {
        selectNodeContents(node) {
          this.node = node;
        },
        collapse(value) {
          this.collapsed = value;
        },
      };
    },
    execCommand(command, _ui, value) {
      execCalls.push({ command, value });
      if (command === 'insertText' && document.activeElement) {
        document.activeElement.innerText = value;
        return true;
      }
      return false;
    },
  };

  const voiceStore = Object.assign({
    enabled: true,
    boundSessionId: 'session-a',
    bufferText: 'Voice import text ready for the desktop handoff.',
    micMode: 'listening',
    sheetResumeListeningOnDismiss: false,
    clearBuffer() {
      this.bufferText = '';
    },
    setBufferText(value) {
      this.bufferText = value;
    },
    setMicMode(mode) {
      this.micMode = mode;
      return true;
    },
  }, (opts && opts.voiceStore) || {});

  const stores = {
    sessions: {
      'session-a': {
        entries: [],
        isLive: true,
        loaded: true,
        resolved: true,
      },
    },
    voice: voiceStore,
  };

  const composerStore = {
    draftText: (opts && opts.initialDraftText) || '',
  };
  sessionStores['session-a'] = composerStore;

  const windowObj = {
    SessionRenderer: {},
    innerWidth: width,
    innerHeight: height,
    visualViewport: { width, height },
    addEventListener() {},
    removeEventListener() {},
    getSelection() {
      return selection;
    },
    getSessionStore(sessionId) {
      return sessionStores[sessionId] || null;
    },
    Autonomy: {
      voice: {
        shell: {
          previewWords(text, limit) {
            previewCalls.push({ text, limit });
            return String(text || '').trim().split(/\s+/).filter(Boolean).slice(-limit).join(' ');
          },
        },
      },
    },
  };

  const Alpine = {
    store(name, obj) {
      if (obj !== undefined) {
        stores[name] = obj;
        return obj;
      }
      return stores[name];
    },
    data(name, factory) {
      components[name] = factory;
    },
  };

  const sandbox = {
    window: windowObj,
    document,
    Alpine,
    console,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    Promise,
    JSON,
    Object,
    Array,
    Math,
    Number,
    String,
    Boolean,
    Date,
    Error,
  };
  sandbox.window.document = document;
  sandbox.window.Alpine = Alpine;
  sandbox.getSelection = windowObj.getSelection;

  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(VIEWER_JS, 'utf8'), sandbox, { filename: 'session-viewer.js' });

  for (const cb of (docListeners['alpine:init'] || [])) cb();

  const viewer = components.sessionViewerPage({});
  viewer.$watch = function() { return function() {}; };
  viewer.$nextTick = function(fn) { if (typeof fn === 'function') fn(); };
  viewer.sessionKey = 'session-a';
  viewer.$refs = {
    messageInput: {
      innerText: (opts && opts.initialEditorText) || composerStore.draftText || '',
      focusCalls: 0,
      focus() {
        document.activeElement = this;
        this.focusCalls += 1;
      },
    },
  };

  return {
    viewer,
    voiceStore,
    composerStore,
    previewCalls,
    execCalls,
  };
}

describe('desktop voice composer handoff (retired)', () => {
  // The desktop "preview strip + import-to-box -> send -> unmute" flow is gone.
  // Desktop now uses the SAME voice-first capsule + dictation tile as mobile
  // (one-click send, mic stays live). These tests pin the retirement so the
  // clunky strip can't silently return.
  it('never shows the desktop import pill, on any viewport or buffer state', () => {
    assert.equal(loadViewer().viewer.showDesktopVoiceImport, false);
    assert.equal(loadViewer({ width: 390 }).viewer.showDesktopVoiceImport, false);
    assert.equal(
      loadViewer({ voiceStore: { boundSessionId: 'session-b' } }).viewer.showDesktopVoiceImport,
      false
    );
    assert.equal(
      loadViewer({ voiceStore: { bufferText: '   ' } }).viewer.showDesktopVoiceImport,
      false
    );
  });

  it('importVoiceBufferToComposer no-ops (returns false) and touches nothing', () => {
    const h = loadViewer({
      initialDraftText: '   ',
      initialEditorText: '   ',
      voiceStore: { bufferText: 'Imported from voice', micMode: 'vad_paused' },
    });
    assert.equal(h.viewer.importVoiceBufferToComposer(), false);
    // Composer untouched, voice buffer preserved, mic mode unchanged.
    assert.equal(h.viewer.$refs.messageInput.innerText, '   ');
    assert.equal(h.voiceStore.bufferText, 'Imported from voice');
    assert.equal(h.voiceStore.micMode, 'vad_paused');
    assert.equal(h.execCalls.length, 0);
  });

  it('still derives the shared preview helper (used by other surfaces)', () => {
    const h = loadViewer({
      voiceStore: {
        bufferText: 'one two three four five six seven eight nine ten eleven twelve thirteen',
      },
    });
    assert.equal(
      h.viewer.desktopVoicePreview,
      'two three four five six seven eight nine ten eleven twelve thirteen'
    );
  });
});
