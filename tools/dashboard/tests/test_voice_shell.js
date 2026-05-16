const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const VOICE_SHELL_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/voice-shell.js');

function loadVoiceShell(opts) {
  const docListeners = {};
  const winListeners = {};
  const dataFns = {};
  const viewerPage = !!(opts && opts.viewerPage);
  const width = (opts && opts.width) || 390;
  const height = (opts && opts.height) || 844;

  const voiceStore = Object.assign({
    enabled: true,
    boundSessionId: 'session-a',
    micMode: 'listening',
    bufferText: '',
    capsulePosition: null,
    pendingRebindTarget: '',
    sheetOpen: false,
    sheetMode: 'partial',
    sheetError: '',
    _sendCalls: 0,
    _openSheetCalls: 0,
    _setCapsuleCalls: [],
    openSheet() {
      this._openSheetCalls += 1;
      this.sheetOpen = true;
      return true;
    },
    dismissSheet() {
      this.sheetOpen = false;
      return true;
    },
    expandSheet() {
      this.sheetMode = 'full';
      return true;
    },
    collapseSheet() {
      this.sheetMode = 'partial';
      return true;
    },
    clearBuffer() {
      this.bufferText = '';
      this.sheetError = '';
    },
    async sendBuffer() {
      this._sendCalls += 1;
      return true;
    },
    confirmRebind() {
      this.boundSessionId = this.pendingRebindTarget;
      this.pendingRebindTarget = '';
      return true;
    },
    cancelRebind() {
      this.pendingRebindTarget = '';
    },
    setCapsulePosition(position) {
      const plain = JSON.parse(JSON.stringify(position));
      this.capsulePosition = plain;
      this._setCapsuleCalls.push(plain);
      return true;
    },
  }, (opts && opts.voiceStore) || {});

  const flagsStore = {
    get(name) {
      if (name === 'voice.client_enabled') return true;
      if (name === 'voice.responsive_collapse_enabled') return false;
      return false;
    },
  };
  Object.assign(flagsStore, (opts && opts.flagsStore) || {});

  const document = {
    addEventListener(name, cb) {
      (docListeners[name] ||= []).push(cb);
    },
    querySelector(selector) {
      if (selector === '.session-viewer[data-mode="page"]') {
        return viewerPage ? { nodeType: 1 } : null;
      }
      return null;
    },
  };

  const visualViewport = {
    width,
    height,
    addEventListener(name, cb) {
      (winListeners['visual:' + name] ||= []).push(cb);
    },
    removeEventListener(name, cb) {
      winListeners['visual:' + name] = (winListeners['visual:' + name] || []).filter((fn) => fn !== cb);
    },
  };

  const windowObj = {
    Autonomy: { voice: {} },
    innerWidth: width,
    innerHeight: height,
    visualViewport,
    addEventListener(name, cb) {
      (winListeners[name] ||= []).push(cb);
    },
    removeEventListener(name, cb) {
      winListeners[name] = (winListeners[name] || []).filter((fn) => fn !== cb);
    },
  };

  const Alpine = {
    _stores: {
      voice: voiceStore,
      flags: flagsStore,
    },
    store(name) {
      return this._stores[name];
    },
    data(name, factory) {
      dataFns[name] = factory;
    },
  };

  const sandbox = {
    window: windowObj,
    document,
    Alpine,
    console,
    setTimeout,
    clearTimeout,
    JSON,
    Math,
    Number,
    String,
    Boolean,
    Object,
    Array,
    Promise,
  };
  sandbox.window.document = document;
  sandbox.window.Alpine = Alpine;

  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(VOICE_SHELL_JS, 'utf8'), sandbox, { filename: 'voice-shell.js' });

  for (const cb of (docListeners['alpine:init'] || [])) cb();

  const component = dataFns.voiceShell();
  component.$refs = {
    capsule: {
      getBoundingClientRect() {
        return { left: 262, top: 706, width: 114, height: 56 };
      },
    },
    sheetInput: {
      focusCalls: 0,
      focus() {
        this.focusCalls += 1;
      },
    },
  };
  component.init();

  return {
    component,
    voiceStore,
    flagsStore,
    winListeners,
    window: windowObj,
  };
}

function actionTarget(action) {
  const node = {
    getAttribute(name) {
      return name === 'data-voice-action' ? action : '';
    },
  };
  return {
    closest(selector) {
      return selector === '[data-voice-action]' ? node : null;
    },
  };
}

describe('voice shell helpers', () => {
  it('hides the inline composer only on mobile when the sheet is open and responsive collapse is enabled', () => {
    const h = loadVoiceShell({
      viewerPage: true,
      voiceStore: { sheetOpen: true },
      flagsStore: {
        get(name) {
          if (name === 'voice.client_enabled') return true;
          if (name === 'voice.responsive_collapse_enabled') return true;
          return false;
        },
      },
    });
    assert.equal(h.window.Autonomy.voice.shell.hideInlineComposer(), true);

    const off = loadVoiceShell({
      viewerPage: true,
      voiceStore: { sheetOpen: true },
    });
    assert.equal(off.window.Autonomy.voice.shell.hideInlineComposer(), false);
  });

  it('keeps the capsule hidden on the viewer route until responsive collapse is enabled', () => {
    const h = loadVoiceShell({
      viewerPage: true,
      voiceStore: {
        boundSessionId: 'session-a',
        sheetOpen: false,
      },
    });
    assert.equal(h.component.showCapsule, false);
  });

  it('shows the capsule on non-viewer mobile surfaces when voice is bound and the sheet is closed', () => {
    const h = loadVoiceShell({
      viewerPage: false,
      voiceStore: {
        boundSessionId: 'session-a',
        sheetOpen: false,
      },
    });
    assert.equal(h.component.showCapsule, true);
  });

  it('shows the sheet on non-viewer mobile surfaces when the sheet state is open', () => {
    const h = loadVoiceShell({
      viewerPage: false,
      voiceStore: {
        boundSessionId: 'session-a',
        sheetOpen: true,
      },
    });
    assert.equal(h.component.showSheet, true);
  });

  it('routes the Type capsule action through voice.openSheet()', () => {
    const h = loadVoiceShell();
    assert.equal(h.component.runCapsuleAction('type'), true);
    assert.equal(h.voiceStore._openSheetCalls, 1);
    assert.equal(h.voiceStore.sheetOpen, true);
  });

  it('drags the capsule from the send button without firing send', () => {
    const h = loadVoiceShell();
    const down = {
      clientX: 320,
      clientY: 730,
      target: actionTarget('send'),
      preventDefault() {},
    };
    assert.equal(h.component.onCapsulePointerDown(down), true);
    h.winListeners.pointermove[0]({ clientX: 350, clientY: 690 });
    h.winListeners.pointerup[0]({ clientX: 350, clientY: 690 });
    assert.equal(h.voiceStore._sendCalls, 0);
    assert.equal(h.voiceStore._setCapsuleCalls.length, 1);
    assert.deepEqual(h.voiceStore._setCapsuleCalls[0], { x: 268, y: 716 });
  });

  it('sheet handle drag expands a partial sheet to full mode', () => {
    const h = loadVoiceShell({
      voiceStore: {
        sheetOpen: true,
        sheetMode: 'partial',
      },
    });
    const handleEvent = {
      clientY: 520,
      target: {
        closest(selector) {
          return selector === '.voice-sheet__handle' ? { nodeType: 1 } : null;
        },
      },
    };
    assert.equal(h.component.startSheetGesture(handleEvent), true);
    h.winListeners.pointerup[0]({ clientY: 460 });
    assert.equal(h.voiceStore.sheetMode, 'full');
  });
});
