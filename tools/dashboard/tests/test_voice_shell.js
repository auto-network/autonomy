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
      sessions: Object.assign({}, (opts && opts.sessionsStore) || {}),
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
  it('hides the inline composer on mobile whenever voice is bound (voice-first viewer)', () => {
    // Pinned behavior: a bound voice session on mobile always replaces the
    // keyboard composer with the voice UI — no longer gated on sheetOpen +
    // the (now-vestigial) voice.responsive_collapse_enabled flag.
    const bound = loadVoiceShell({
      viewerPage: true,
      voiceStore: { boundSessionId: 'session-a' },
    });
    assert.equal(bound.window.Autonomy.voice.shell.hideInlineComposer(), true);

    // Not bound -> the keyboard composer stays (still a way to type when voice
    // is idle).
    const unbound = loadVoiceShell({
      viewerPage: true,
      voiceStore: { boundSessionId: '' },
    });
    assert.equal(unbound.window.Autonomy.voice.shell.hideInlineComposer(), false);
  });

  it('shows the capsule + caption on the viewer route when voice is bound on mobile', () => {
    // Pinned behavior: the capsule/caption appear in the viewer when voice is
    // bound on mobile, regardless of the responsive_collapse flag (vestigial).
    const h = loadVoiceShell({
      viewerPage: true,
      voiceStore: {
        boundSessionId: 'session-a',
        sheetOpen: false,
      },
    });
    assert.equal(h.component.showCapsule, true);
    assert.equal(h.component.showCaption, true);
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
    assert.equal(h.component.showCaption, true);
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
    assert.equal(h.component.showCaption, false);
  });

  it('shows the caption on the viewer route once responsive collapse is enabled', () => {
    const h = loadVoiceShell({
      viewerPage: true,
      voiceStore: {
        boundSessionId: 'session-a',
        sheetOpen: false,
      },
      flagsStore: {
        get(name) {
          if (name === 'voice.client_enabled') return true;
          if (name === 'voice.responsive_collapse_enabled') return true;
          return false;
        },
      },
    });
    assert.equal(h.component.showCaption, true);
    assert.equal(h.component.showCapsule, true);
  });

  it('uses the Live captions placeholder when the buffer is empty', () => {
    const h = loadVoiceShell({
      voiceStore: {
        bufferText: '',
      },
    });
    assert.equal(h.component.hasCaptionText, false);
    assert.equal(h.component.captionPlaceholder, 'Live captions');
    assert.equal(h.component.captionText, '');
  });

  it('caps the caption preview to the last twelve words of the shared buffer', () => {
    const h = loadVoiceShell({
      voiceStore: {
        bufferText: 'Use the actual session viewer control so the voice surfaces land on the real chrome with no more bubble drift.',
      },
    });
    assert.equal(
      h.component.captionText,
      'voice surfaces land on the real chrome with no more bubble drift.'
    );
  });

  it('brightens Send only when vad_paused and the shared buffer is non-empty', () => {
    const h = loadVoiceShell({
      voiceStore: {
        micMode: 'vad_paused',
        bufferText: 'Keep the current transcript visible while silence holds.',
      },
    });
    assert.equal(h.component.sendPulse, true);

    const muted = loadVoiceShell({
      voiceStore: {
        micMode: 'muted',
        bufferText: 'Keep the current transcript visible while silence holds.',
      },
    });
    assert.equal(muted.component.sendPulse, false);

    const empty = loadVoiceShell({
      voiceStore: {
        micMode: 'vad_paused',
        bufferText: '   ',
      },
    });
    assert.equal(empty.component.sendPulse, false);
  });

  it('exposes the shared previewWords helper for cross-surface reuse', () => {
    const h = loadVoiceShell();
    assert.equal(typeof h.window.Autonomy.voice.shell.previewWords, 'function');
    assert.equal(
      h.window.Autonomy.voice.shell.previewWords(
        'one two three four five six seven eight nine ten eleven twelve thirteen',
        12
      ),
      'two three four five six seven eight nine ten eleven twelve thirteen'
    );
  });

  it('rebindCopy includes the bound session label when one is available', () => {
    const h = loadVoiceShell({
      sessionsStore: {
        'session-a': { label: 'My session' },
      },
      voiceStore: {
        boundSessionId: 'session-a',
        pendingRebindTarget: 'session-b',
      },
    });
    assert.equal(h.component.rebindCopy, 'End voice on My session?');
  });

  it('rebindCopy falls back to the bound session id when no label is available', () => {
    const h = loadVoiceShell({
      voiceStore: {
        boundSessionId: 'auto-0515-163913',
        pendingRebindTarget: 'session-b',
      },
    });
    assert.equal(h.component.rebindCopy, 'End voice on auto-0515-163913?');
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
    assert.deepEqual(h.voiceStore._setCapsuleCalls[0], { x: 268, y: 664 });
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

  it('lifts the default capsule position above the reserved caption gutter', () => {
    const h = loadVoiceShell();
    const position = h.component._currentCapsulePosition(h.component.$refs.capsule);
    assert.equal(position.x, 262);
    assert.equal(position.y, 704);
  });

  it('canSendVoice mirrors the store canSend flag (drives the sheet Send button)', () => {
    const yes = loadVoiceShell({ voiceStore: { canSend: true } });
    assert.equal(yes.component.canSendVoice, true);
    const no = loadVoiceShell({ voiceStore: { canSend: false } });
    assert.equal(no.component.canSendVoice, false);
  });

  it('onVoicePickFiles forwards picked files to the store and resets the input', () => {
    const calls = [];
    const h = loadVoiceShell({
      voiceStore: { addAttachmentFiles(files) { calls.push(files); } },
    });
    const input = { files: ['f1', 'f2'], value: 'C:/fakepath' };
    assert.equal(h.component.onVoicePickFiles({ target: input }), true);
    assert.deepEqual(calls, [['f1', 'f2']]);
    assert.equal(input.value, '');   // reset so the same file can be re-picked
  });

  it('removeVoiceAttachment forwards the id to the store', () => {
    const removed = [];
    const h = loadVoiceShell({
      voiceStore: { removeAttachment(id) { removed.push(id); } },
    });
    assert.equal(h.component.removeVoiceAttachment(7), true);
    assert.deepEqual(removed, [7]);
  });

  it('shows the voice-first surfaces on DESKTOP too when bound (no viewport gate)', () => {
    // The operator wants desktop to behave exactly like mobile: capsule +
    // caption + hidden inline composer when voice is bound, regardless of the
    // 1280px-wide viewport (the old min-width:768 gate is gone).
    const h = loadVoiceShell({
      viewerPage: true,
      width: 1280,
      voiceStore: { boundSessionId: 'session-a', sheetOpen: false },
    });
    assert.equal(h.component.showCapsule, true);
    assert.equal(h.component.showCaption, true);
    assert.equal(h.window.Autonomy.voice.shell.hideInlineComposer(), true);

    const sheet = loadVoiceShell({
      width: 1280,
      voiceStore: { boundSessionId: 'session-a', sheetOpen: true },
    });
    assert.equal(sheet.component.showSheet, true);
  });

  it('Clear collapses the capturing dictation tile (clear-tile bug)', () => {
    const h = loadVoiceShell({
      voiceStore: { boundSessionId: 'session-a', bufferText: 'old dictation' },
    });
    const sessionStore = { outbox: { source: 'voice', state: 'capturing', text: 'old dictation' } };
    h.window.getSessionStore = function (sid) {
      return sid === 'session-a' ? sessionStore : null;
    };
    h.component.clearBuffer();
    assert.equal(sessionStore.outbox, null);
  });

  it('Clear leaves a non-capturing outbox (a pending send) intact', () => {
    const h = loadVoiceShell({ voiceStore: { boundSessionId: 'session-a' } });
    const sending = { source: 'voice', state: 'sending', text: 'in flight' };
    const sessionStore = { outbox: sending };
    h.window.getSessionStore = function () { return sessionStore; };
    h.component.clearBuffer();
    assert.equal(sessionStore.outbox, sending);
  });
});
