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

describe('desktop voice composer handoff', () => {
  it('shows the desktop import pill only for the bound desktop viewer with a non-empty voice buffer', () => {
    const ready = loadViewer();
    assert.equal(ready.viewer.showDesktopVoiceImport, true);

    const mobile = loadViewer({ width: 390 });
    assert.equal(mobile.viewer.showDesktopVoiceImport, false);

    const unbound = loadViewer({ voiceStore: { boundSessionId: 'session-b' } });
    assert.equal(unbound.viewer.showDesktopVoiceImport, false);

    const empty = loadViewer({ voiceStore: { bufferText: '   ' } });
    assert.equal(empty.viewer.showDesktopVoiceImport, false);
  });

  it('derives the desktop preview from the shared shell preview helper', () => {
    const h = loadViewer({
      voiceStore: {
        bufferText: 'one two three four five six seven eight nine ten eleven twelve thirteen',
      },
    });
    assert.equal(
      h.viewer.desktopVoicePreview,
      'two three four five six seven eight nine ten eleven twelve thirteen'
    );
    assert.deepEqual(h.previewCalls, [{
      text: 'one two three four five six seven eight nine ten eleven twelve thirteen',
      limit: 12,
    }]);
  });

  it('replaces a whitespace-only draft, mutes active capture, and clears the shared buffer after import', () => {
    const h = loadViewer({
      initialDraftText: '   ',
      initialEditorText: '   ',
      voiceStore: {
        bufferText: 'Imported from voice',
        micMode: 'vad_paused',
      },
    });
    assert.equal(h.viewer.importVoiceBufferToComposer(), true);
    assert.equal(h.composerStore.draftText, 'Imported from voice');
    assert.equal(h.viewer.$refs.messageInput.innerText, 'Imported from voice');
    assert.equal(h.voiceStore.bufferText, '');
    assert.equal(h.voiceStore.micMode, 'muted');
    assert.equal(h.voiceStore.sheetResumeListeningOnDismiss, false);
    assert.equal(h.viewer.$refs.messageInput.focusCalls > 0, true);
    assert.deepEqual(h.execCalls[0], {
      command: 'insertText',
      value: 'Imported from voice',
    });
  });

  it('appends the voice snapshot to an existing draft with a blank-line separator', () => {
    const h = loadViewer({
      initialDraftText: 'Existing desktop draft',
      initialEditorText: 'Existing desktop draft',
      voiceStore: {
        bufferText: 'Fresh voice buffer',
        micMode: 'muted',
      },
    });
    assert.equal(h.viewer.importVoiceBufferToComposer(), true);
    assert.equal(h.composerStore.draftText, 'Existing desktop draft\n\nFresh voice buffer');
    assert.equal(h.viewer.$refs.messageInput.innerText, 'Existing desktop draft\n\nFresh voice buffer');
    assert.equal(h.voiceStore.bufferText, '');
    assert.equal(h.voiceStore.micMode, 'muted');
    assert.deepEqual(h.execCalls[0], {
      command: 'insertText',
      value: 'Existing desktop draft\n\nFresh voice buffer',
    });
  });
});
