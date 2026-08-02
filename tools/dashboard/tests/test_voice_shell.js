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
  const effects = [];
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
    deliveryMode: 'session',
    voiceoverEnabled: true,
    get voiceoverActive() {
      return this.voiceoverEnabled === true && this.deliveryMode === 'voiceover';
    },
    voiceoverBusy: false,
    voiceoverReply: '',
    _sendCalls: 0,
    _voiceoverCalls: 0,
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
    async askVoiceover() {
      this._voiceoverCalls += 1;
      return true;
    },
    setDeliveryMode(mode) {
      this.deliveryMode = mode;
      return true;
    },
    speakVoiceover() { return true; },
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
      if (name === 'voice.voiceover_enabled') return true;
      if (name === 'voice.responsive_collapse_enabled') return false;
      return false;
    },
  };
  Object.assign(flagsStore, (opts && opts.flagsStore) || {});

  const _bodyClasses = new Set();
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
    body: {
      dataset: {},
      classList: {
        add: (c) => _bodyClasses.add(c),
        remove: (c) => _bodyClasses.delete(c),
        toggle: (c, on) => { if (on) _bodyClasses.add(c); else _bodyClasses.delete(c); },
        contains: (c) => _bodyClasses.has(c),
      },
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
    effect(fn) {
      effects.push(fn);
      fn();
      return fn;
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
    document,
    effects,
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
  it('routes commit to Voiceover only when the operator selects it', async () => {
    const h = loadVoiceShell();
    assert.equal(await h.component.sendBuffer(), true);
    assert.equal(h.voiceStore._sendCalls, 1);
    assert.equal(h.voiceStore._voiceoverCalls, 0);

    h.component.setVoiceDelivery('voiceover');
    assert.equal(h.component.voiceoverMode, true);
    assert.equal(await h.component.sendBuffer(), true);
    assert.equal(h.voiceStore._sendCalls, 1);
    assert.equal(h.voiceStore._voiceoverCalls, 1);
  });

  it('hides and disables the speaker control when the Voiceover feature flag is off', () => {
    const h = loadVoiceShell({ voiceStore: { voiceoverEnabled: false } });
    assert.equal(h.component.voiceoverEnabled, false);
    assert.equal(h.component.toggleVoiceover(), false);
    assert.equal(h.voiceStore.deliveryMode, 'session');
  });

  it('speaker action toggles Voiceover mode without opening the sheet', () => {
    const h = loadVoiceShell();
    const inactiveIcon = h.component.capsuleIcon('voiceover');
    assert.match(inactiveIcon, /M15 9\.25a4 4/);
    assert.doesNotMatch(inactiveIcon, /M18 6\.5a8 8/);
    assert.equal(h.component.runCapsuleAction('voiceover'), true);
    assert.equal(h.component.voiceoverMode, true);
    const activeIcon = h.component.capsuleIcon('voiceover');
    assert.match(activeIcon, /M15 9\.25a4 4/);
    assert.match(activeIcon, /M18 6\.5a8 8/);
    assert.equal(h.component.runCapsuleAction('voiceover'), true);
    assert.equal(h.component.voiceoverMode, false);
  });

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

  it('uses an empty caption placeholder when the buffer is empty', () => {
    const h = loadVoiceShell({
      voiceStore: {
        bufferText: '',
      },
    });
    assert.equal(h.component.hasCaptionText, false);
    assert.equal(h.component.captionPlaceholder, '');
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

  it('ends PTT on release even when the capsule was dragged during the hold (#38)', async () => {
    const setMicModeCalls = [];
    const h = loadVoiceShell({
      voiceStore: {
        micMode: 'muted',
        setMicMode(mode) { this.micMode = mode; setMicModeCalls.push(mode); },
      },
    });
    const down = { clientX: 320, clientY: 730, target: actionTarget('mic'), preventDefault() {} };
    assert.equal(h.component.onCapsulePointerDown(down), true);
    // Let the hold timer fire → push-to-talk engages (gray → orange / listening).
    await new Promise((r) => setTimeout(r, 400));
    assert.equal(h.component.capsulePttActive, true, 'hold engaged PTT');
    assert.equal(h.voiceStore.micMode, 'listening');
    // Drag the capsule while STILL holding, then release.
    h.winListeners.pointermove[0]({ clientX: 360, clientY: 690 });
    h.winListeners.pointerup[0]({ clientX: 360, clientY: 690 });
    // Bug was: the dragging branch returned before ending PTT → stuck orange.
    assert.equal(h.component.capsulePttActive, false, 'release ends PTT despite the drag');
    assert.equal(h.voiceStore.micMode, 'muted');
    assert.deepEqual(setMicModeCalls, ['listening', 'muted']);
    assert.equal(h.voiceStore._setCapsuleCalls.length, 1, 'the drag still persisted the new position');
  });

  it('long-press on the violet (cross-session) Send claims dictation to the viewed session', async () => {
    const bindCalls = [];
    const h = loadVoiceShell({ voiceStore: { bindSession(id) { bindCalls.push(id); } } });
    h.document.body.classList.add('sv-cross-session-dictation');
    h.document.body.dataset.svComposerSession = 'auto-viewed';
    const down = { clientX: 320, clientY: 730, target: actionTarget('send'), preventDefault() {} };
    assert.equal(h.component.onCapsulePointerDown(down), true);
    await new Promise((r) => setTimeout(r, 650));   // past the CAPSULE_CLEAR_MS hold
    h.winListeners.pointerup[0]({ clientX: 320, clientY: 730 });
    assert.deepEqual(bindCalls, ['auto-viewed'], 'claimed to the viewed session');
    assert.equal(h.voiceStore._sendCalls, 0, 'claim does not also send');
  });

  it('a quick tap on the violet Send still sends to the remote session (no claim)', () => {
    const bindCalls = [];
    const h = loadVoiceShell({ voiceStore: { bindSession(id) { bindCalls.push(id); } } });
    h.document.body.classList.add('sv-cross-session-dictation');
    h.document.body.dataset.svComposerSession = 'auto-viewed';
    const down = { clientX: 320, clientY: 730, target: actionTarget('send'), preventDefault() {} };
    h.component.onCapsulePointerDown(down);
    h.winListeners.pointerup[0]({ clientX: 320, clientY: 730 });   // immediate release = tap
    assert.deepEqual(bindCalls, []);
    assert.equal(h.voiceStore._sendCalls, 1);
  });

  it('a hold on Send does NOT claim when not cross-session (no violet)', async () => {
    const bindCalls = [];
    const h = loadVoiceShell({ voiceStore: { bindSession(id) { bindCalls.push(id); } } });
    // no sv-cross-session-dictation class → Send hold must not arm a claim
    const down = { clientX: 320, clientY: 730, target: actionTarget('send'), preventDefault() {} };
    h.component.onCapsulePointerDown(down);
    await new Promise((r) => setTimeout(r, 650));
    h.winListeners.pointerup[0]({ clientX: 320, clientY: 730 });
    assert.deepEqual(bindCalls, [], 'no claim off the violet state');
    assert.equal(h.voiceStore._sendCalls, 1, 'falls through to a normal send');
  });

  it('voice Send uses the shared voice-store transition even when the viewer composer is active', async () => {
    const h = loadVoiceShell({
      voiceStore: {
        boundSessionId: 'session-a',
        bufferText: 'shared transition',
        async sendBuffer() {
          this._sendCalls += 1;
          this.bufferText = '';
          return true;
        },
      },
    });
    h.document.body.classList.add('sv-viewer-composer-active');
    h.document.body.dataset.svComposerSession = 'session-a';
    h.window.getSessionStore = function () {
      throw new Error('voice-shell must not own the Send transition');
    };

    assert.equal(await h.component.sendBuffer(), true);
    assert.equal(h.voiceStore._sendCalls, 1);
    assert.equal(h.voiceStore.bufferText, '');
  });

  it('syncs an existing voice buffer into a capturing outbox when the viewer composer is active', () => {
    const sessionStore = { outbox: null };
    const h = loadVoiceShell({
      voiceStore: { boundSessionId: 'session-a', bufferText: 'existing dictation' },
    });
    h.window.getSessionStore = function (sid) {
      return sid === 'session-a' ? sessionStore : null;
    };
    h.window.newOutboxId = function () { return 'ob_existing_buffer'; };

    h.document.body.classList.add('sv-viewer-composer-active');
    h.document.body.dataset.svComposerSession = 'session-a';

    assert.equal(h.window.Autonomy.voice.shell.syncViewerOutboxCapture(), true);
    assert.equal(sessionStore.outbox && sessionStore.outbox.state, 'capturing');
    assert.equal(sessionStore.outbox && sessionStore.outbox.text, 'existing dictation');
  });

  it('keeps Voiceover questions out of the coding session outbox', () => {
    const sessionStore = {
      outbox: { localId: 'ob_old', state: 'capturing', source: 'voice', text: 'private question' },
    };
    const h = loadVoiceShell({
      voiceStore: {
        boundSessionId: 'session-a',
        viewedSessionId: 'session-a',
        bufferText: 'What is the session doing?',
        deliveryMode: 'voiceover',
      },
    });
    h.window.getSessionStore = function () { return sessionStore; };
    h.document.body.classList.add('sv-viewer-composer-active');
    h.document.body.dataset.svComposerSession = 'session-a';

    assert.equal(h.window.Autonomy.voice.shell.syncViewerOutboxCapture(), true);
    assert.equal(sessionStore.outbox, null);
    assert.equal(h.window.Autonomy.voice.shell.syncViewerOutboxCapture(), false);
    assert.equal(sessionStore.outbox, null);
  });

  it('updates the capturing outbox as the voice buffer changes', () => {
    const sessionStore = {
      outbox: { localId: 'ob_capture', source: 'voice', state: 'capturing', text: 'first', ts: 1 },
    };
    const h = loadVoiceShell({
      voiceStore: { boundSessionId: 'session-a', bufferText: 'first and second' },
    });
    h.window.getSessionStore = function (sid) {
      return sid === 'session-a' ? sessionStore : null;
    };
    h.document.body.classList.add('sv-viewer-composer-active');
    h.document.body.dataset.svComposerSession = 'session-a';

    assert.equal(h.window.Autonomy.voice.shell.syncViewerOutboxCapture(), true);
    assert.equal(sessionStore.outbox.localId, 'ob_capture');
    assert.equal(sessionStore.outbox.text, 'first and second');
  });

  it('clears only a voice capturing outbox when the active voice buffer is empty', () => {
    const sessionStore = {
      outbox: { localId: 'ob_capture', source: 'voice', state: 'capturing', text: 'old text', ts: 1 },
    };
    const h = loadVoiceShell({
      voiceStore: { boundSessionId: 'session-a', bufferText: '' },
    });
    h.window.getSessionStore = function (sid) {
      return sid === 'session-a' ? sessionStore : null;
    };
    h.document.body.classList.add('sv-viewer-composer-active');
    h.document.body.dataset.svComposerSession = 'session-a';

    assert.equal(h.window.Autonomy.voice.shell.syncViewerOutboxCapture(), true);
    assert.equal(sessionStore.outbox, null);
  });

  it('does not overwrite a non-empty sending outbox while syncing capture', () => {
    const sending = { localId: 'ob_send', source: 'voice', state: 'sending', text: 'already sent', ts: 1 };
    const sessionStore = { outbox: sending };
    const h = loadVoiceShell({
      voiceStore: { boundSessionId: 'session-a', bufferText: 'new transcript' },
    });
    h.window.getSessionStore = function (sid) {
      return sid === 'session-a' ? sessionStore : null;
    };
    h.document.body.classList.add('sv-viewer-composer-active');
    h.document.body.dataset.svComposerSession = 'session-a';

    assert.equal(h.window.Autonomy.voice.shell.syncViewerOutboxCapture(), false);
    assert.equal(sessionStore.outbox, sending);
  });

  it('does not overwrite a non-empty unconfirmed outbox while syncing capture', () => {
    const unconfirmed = { localId: 'ob_retry', source: 'voice', state: 'unconfirmed', text: 'retry me', ts: 1 };
    const sessionStore = { outbox: unconfirmed };
    const h = loadVoiceShell({
      voiceStore: { boundSessionId: 'session-a', bufferText: 'new transcript' },
    });
    h.window.getSessionStore = function (sid) {
      return sid === 'session-a' ? sessionStore : null;
    };
    h.document.body.classList.add('sv-viewer-composer-active');
    h.document.body.dataset.svComposerSession = 'session-a';

    assert.equal(h.window.Autonomy.voice.shell.syncViewerOutboxCapture(), false);
    assert.equal(sessionStore.outbox, unconfirmed);
  });

  it('sheetCrossSession is true when bound != viewed, with the target title from the store', () => {
    const h = loadVoiceShell({
      voiceStore: { boundSessionId: 'auto-B', viewedSessionId: 'auto-A' },
      sessionsStore: { 'auto-B': { label: 'Enterprise NG' } },
    });
    assert.equal(h.component.sheetCrossSession, true);
    assert.equal(h.component.sheetCrossTargetTitle, 'Enterprise NG');
  });

  it('sheetCrossSession is false when viewing the bound session', () => {
    const h = loadVoiceShell({ voiceStore: { boundSessionId: 'auto-A', viewedSessionId: 'auto-A' } });
    assert.equal(h.component.sheetCrossSession, false);
  });

  it('sheetCrossSession is false when not in any viewer (no viewedSessionId)', () => {
    const h = loadVoiceShell({ voiceStore: { boundSessionId: 'auto-B', viewedSessionId: '' } });
    assert.equal(h.component.sheetCrossSession, false);
  });

  it('sheetCrossTargetTitle falls back to the bound id when the session has no label', () => {
    const h = loadVoiceShell({ voiceStore: { boundSessionId: 'auto-B', viewedSessionId: 'auto-A' } });
    assert.equal(h.component.sheetCrossTargetTitle, 'auto-B');
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

  it('Clear resets capture after clearing the buffer and the capturing outbox', () => {
    const events = [];
    const h = loadVoiceShell({
      voiceStore: {
        boundSessionId: 'session-a',
        bufferText: 'old dictation',
        clearBuffer() {
          events.push('clearBuffer');
          this.bufferText = '';
          this.sheetError = '';
        },
      },
    });
    const sessionStore = { outbox: { source: 'voice', state: 'capturing', text: 'old dictation' } };
    h.window.getSessionStore = function (sid) {
      return sid === 'session-a' ? sessionStore : null;
    };
    h.window.Autonomy.voiceCapture = {
      resetEpoch(reason) {
        events.push('reset:' + reason);
        assert.equal(reason, 'clear');
        assert.equal(h.voiceStore.bufferText, '');
        assert.equal(sessionStore.outbox, null);
        return true;
      },
    };

    h.component.clearBuffer();
    assert.deepEqual(events, ['clearBuffer', 'reset:clear']);
  });

  it('Clear leaves a non-capturing outbox (a pending send) intact', () => {
    const h = loadVoiceShell({ voiceStore: { boundSessionId: 'session-a' } });
    const sending = { source: 'voice', state: 'sending', text: 'in flight' };
    const sessionStore = { outbox: sending };
    h.window.getSessionStore = function () { return sessionStore; };
    h.component.clearBuffer();
    assert.equal(sessionStore.outbox, sending);
  });

  // ── 3-button capsule: mic control (#30) ───────────────────────────────
  it('capsuleMicClass: gray when muted, blue when listening, orange on PTT', () => {
    assert.equal(loadVoiceShell({ voiceStore: { micMode: 'muted' } })
      .component.capsuleMicClass['voice-capsule__mic--muted'], true);
    assert.equal(loadVoiceShell({ voiceStore: { micMode: 'listening' } })
      .component.capsuleMicClass['voice-capsule__mic--listening'], true);
    const ptt = loadVoiceShell({ voiceStore: { micMode: 'muted' } });
    ptt.component.capsulePttActive = true;
    assert.equal(ptt.component.capsuleMicClass['voice-capsule__mic--ptt'], true);
  });

  it('capsuleIcon(mic): slashed when muted, open when listening', () => {
    const slash = 'x1="3" y1="3"';   // the slash line is only in the muted glyph
    assert.ok(loadVoiceShell({ voiceStore: { micMode: 'muted' } }).component.capsuleIcon('mic').indexOf(slash) !== -1);
    assert.ok(loadVoiceShell({ voiceStore: { micMode: 'listening' } }).component.capsuleIcon('mic').indexOf(slash) === -1);
  });

  it('mic tap routes to voice.toggleMic()', () => {
    let calls = 0;
    const h = loadVoiceShell({ voiceStore: { toggleMic() { calls++; return true; } } });
    h.component.runCapsuleAction('mic');
    assert.equal(calls, 1);
  });

  it('PTT: begin-from-muted opens the mic + sets active; release mutes', () => {
    const modes = [];
    const h = loadVoiceShell({ voiceStore: { micMode: 'muted', setMicMode(m) { modes.push(m); this.micMode = m; return true; } } });
    h.component._beginCapsulePtt();
    assert.equal(h.component.capsulePttActive, true);
    assert.deepEqual(modes, ['listening']);
    h.component._endCapsulePtt();
    assert.equal(h.component.capsulePttActive, false);
    assert.deepEqual(modes, ['listening', 'muted']);
  });

  it('PTT: begin-from-listening is a no-op (hold while live does nothing)', () => {
    const modes = [];
    const h = loadVoiceShell({ voiceStore: { micMode: 'listening', setMicMode(m) { modes.push(m); return true; } } });
    h.component._beginCapsulePtt();
    assert.equal(h.component.capsulePttActive, false);
    assert.deepEqual(modes, []);
  });

  // ── auto-reconnect mic states (#32) ───────────────────────────────────
  it('capsuleMicClass: red while reconnecting / disconnected (overrides mic mode)', () => {
    const r = loadVoiceShell({ voiceStore: { micMode: 'listening', connState: 'reconnecting' } });
    assert.equal(r.component.capsuleMicClass['voice-capsule__mic--reconnecting'], true);
    assert.ok(!r.component.capsuleMicClass['voice-capsule__mic--listening']);
    const d = loadVoiceShell({ voiceStore: { micMode: 'listening', connState: 'disconnected' } });
    assert.equal(d.component.capsuleMicClass['voice-capsule__mic--disconnected'], true);
  });

  it('capsuleIcon(mic): slashed while reconnecting/disconnected (not actually capturing)', () => {
    const slash = 'x1="3" y1="3"';
    assert.ok(loadVoiceShell({ voiceStore: { micMode: 'listening', connState: 'reconnecting' } })
      .component.capsuleIcon('mic').indexOf(slash) !== -1);
  });

  it('tapping the red mic retries the connection (not toggle)', () => {
    let retries = 0, toggles = 0;
    const h = loadVoiceShell({ voiceStore: { connState: 'disconnected', toggleMic() { toggles++; return true; } } });
    h.window.Autonomy = h.window.Autonomy || {};
    h.window.Autonomy.voiceCapture = { retryReconnect() { retries++; return true; } };
    h.component.runCapsuleAction('mic');
    assert.equal(retries, 1);
    assert.equal(toggles, 0);
  });

  it('tapping the mic when connected toggles mute (not retry)', () => {
    let retries = 0, toggles = 0;
    const h = loadVoiceShell({ voiceStore: { connState: 'ok', toggleMic() { toggles++; return true; } } });
    h.window.Autonomy = { voice: {}, voiceCapture: { retryReconnect() { retries++; } } };
    h.component.runCapsuleAction('mic');
    assert.equal(toggles, 1);
    assert.equal(retries, 0);
  });
});
