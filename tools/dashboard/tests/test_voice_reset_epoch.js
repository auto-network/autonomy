// #43 — WhisperLive reset-suppression: client epoch-acceptance + resetEpoch() hook.
//
// Failing-tests-first per the locked contract with auto-0530-212439. Exercises the
// REAL voice-capture.js IIFE through a mock socket/store, with the feature flag
// `voice.reset_suppression` toggled via a mock Alpine.store('flags').
//
// Contract under test (flag ON):
//   1. setBufferText('') with no Send/Clear emits NO discard/reset control
//      (the bufferText==='' inference is gone in reset mode).
//   2. resetEpoch('clear') sends a 'reset' control immediately.
//   3. resetEpoch('send') sends a 'reset' control synchronously (on tap, before POST).
//   3c. after resetEpoch, an OLD-epoch re-emit frame does NOT call setBufferText,
//       while a NEW-epoch frame (genuine post-reset speech) DOES — proving the
//       immediate reset distinguishes new speech from re-emit during the POST window.
//   4. a stale-epoch transcript/buffer_state frame does NOT call setBufferText.
//   5. resetEpoch() never mutates any session outbox (it only touches the voice WS).
// Flag OFF (reversibility): the legacy bufferText==='' -> discard inference still fires.

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const CAPTURE_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/voice-capture.js');
const RESET_FLAG = 'voice.reset_suppression';

function makeHarness(opts) {
  opts = opts || {};
  const resetMode = opts.resetMode === true;
  const sockets = [];
  class FakeWS {
    constructor(url) { this.url = url; this.readyState = 0; this._l = {}; this.sent = []; sockets.push(this); }
    addEventListener(t, c) { (this._l[t] ||= []).push(c); }
    send(d) { this.sent.push(d); }
    close() { this.readyState = 3; this._fire('close', {}); }
    _fire(t, e) { for (const c of (this._l[t] || [])) c(e); }
    fireOpen() { this.readyState = 1; this._fire('open', {}); }
    _msg(obj) { this._fire('message', { data: JSON.stringify(obj) }); }
    fireFinal(text, epoch) { this._msg({ type: 'transcript', kind: 'final', text, epoch: epoch == null ? 0 : epoch }); }
    firePartial(text, epoch) { this._msg({ type: 'transcript', kind: 'partial', text, epoch: epoch == null ? 0 : epoch }); }
    fireBufferState(text, epoch) { this._msg({ type: 'buffer_state', text, epoch: epoch == null ? 0 : epoch }); }
    controls() {
      return this.sent.filter((d) => typeof d === 'string')
        .map((d) => { try { return JSON.parse(d).type; } catch (_e) { return null; } })
        .filter(Boolean);
    }
  }
  FakeWS.OPEN = 1;

  const effects = [];
  const stores = {};
  const alpine = {
    effect(fn) { effects.push(fn); fn(); },
    store(n, o) { if (o !== undefined) { stores[n] = o; return o; } return stores[n]; },
  };
  const docListeners = {};
  const document = {
    visibilityState: 'visible',
    addEventListener(name, cb) { (docListeners[name] ||= []).push(cb); },
    removeEventListener() {},
  };
  const renders = [];
  const voice = {
    boundSessionId: '', micMode: 'idle', bufferText: '', enabled: true, sheetError: '',
    setBufferText(t) { this.bufferText = t; renders.push(t); },
    setConnState() {},
  };
  stores.voice = voice;
  // Mock the feature-flags store: reset mode reads `voice.reset_suppression`.
  stores.flags = { isLoaded: true, get(name) { return name === RESET_FLAG ? resetMode : false; } };

  const sandbox = {
    console, setTimeout, clearTimeout, setInterval, clearInterval, Promise, JSON, Math, Date, Object, Array, String,
    WebSocket: FakeWS,
    location: { protocol: 'https:', host: 'localhost:8080' },
    navigator: {},
    document,
    window: { console, Autonomy: {}, addEventListener() {}, removeEventListener() {} },
  };
  sandbox.window.document = document;
  sandbox.window.Alpine = alpine;
  sandbox.Alpine = alpine;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(CAPTURE_JS, 'utf8'), sandbox, { filename: 'voice-capture.js' });
  for (const cb of (docListeners['alpine:init'] || [])) cb();

  function runEffects() { for (const fn of effects) fn(); }
  const cap = sandbox.window.Autonomy.voiceCapture;
  return {
    voice, sockets, renders, runEffects, cap,
    resetEpoch(reason) { return cap.resetEpoch(reason); },
    clearBox() { voice.setBufferText(''); renders.length = 0; runEffects(); },
    startListening() {
      voice.boundSessionId = 'auto-A';
      voice.micMode = 'listening';
      runEffects();
      sockets[0].fireOpen();
      return sockets[0];
    },
  };
}

describe('#43 client epoch acceptance + resetEpoch (flag ON)', () => {
  it('1. setBufferText("") with no Send/Clear emits NO discard/reset control', () => {
    const h = makeHarness({ resetMode: true });
    const ws = h.startListening();
    ws.fireFinal('hello world', 0);
    assert.equal(h.voice.bufferText, 'hello world');
    h.clearBox();                       // buffer goes empty WITHOUT an explicit Send/Clear hook
    assert.ok(!ws.controls().includes('discard'), 'no legacy discard inference in reset mode');
    assert.ok(!ws.controls().includes('reset'), 'empty buffer alone must not trigger a reset');
  });

  it('2. resetEpoch("clear") sends a reset control immediately', () => {
    const h = makeHarness({ resetMode: true });
    const ws = h.startListening();
    ws.fireFinal('thank you', 0);
    h.resetEpoch('clear');
    assert.ok(ws.controls().includes('reset'), 'Clear path resets immediately');
  });

  it('3. resetEpoch("send") sends a reset control synchronously (on tap, before POST)', () => {
    const h = makeHarness({ resetMode: true });
    const ws = h.startListening();
    ws.fireFinal('order coffee please', 0);
    const before = ws.controls().filter((c) => c === 'reset').length;
    h.resetEpoch('send');               // fired on tap; no POST awaited here
    const after = ws.controls().filter((c) => c === 'reset').length;
    assert.equal(after, before + 1, 'reset is on the wire on tap, before any POST');
  });

  it('3c. after reset: NEW-epoch speech renders, OLD-epoch re-emit is dropped', () => {
    const h = makeHarness({ resetMode: true });
    const ws = h.startListening();
    ws.fireFinal('send me', 0);                  // serverEpoch now 0
    h.resetEpoch('send');                        // acceptEpoch -> 1
    h.voice.setBufferText('');                   // handler cleared the buffer post-snapshot
    h.renders.length = 0;

    ws.fireFinal('send me', 0);                  // OLD-epoch re-emit of the sent text
    assert.deepEqual(h.renders, [], 're-emit at the old epoch is dropped (no setBufferText)');

    ws.firePartial('brand new thought', 1);      // genuine new speech after the reset
    assert.ok(h.renders.some((r) => /brand new thought/.test(r)),
      'new-epoch speech during the POST window is accepted');
  });

  it('4. a stale-epoch transcript/buffer_state frame does not call setBufferText', () => {
    const h = makeHarness({ resetMode: true });
    const ws = h.startListening();
    ws.fireFinal('first', 2);                    // serverEpoch -> 2
    h.resetEpoch('clear');                       // acceptEpoch -> 3
    h.renders.length = 0;
    ws.firePartial('stale partial', 2);          // below acceptEpoch
    ws.fireBufferState('stale buffer', 1);       // below acceptEpoch
    assert.deepEqual(h.renders, [], 'all sub-acceptEpoch frames dropped');
    ws.fireFinal('fresh', 3);                    // at acceptEpoch
    assert.ok(h.renders.some((r) => /fresh/.test(r)), 'frame at acceptEpoch is accepted');
  });

  it('5. resetEpoch never mutates a session outbox (only the voice WS)', () => {
    const h = makeHarness({ resetMode: true });
    const ws = h.startListening();
    const sentinel = { state: 'sending', source: 'voice', text: 'pending' };
    h.voice.boundOutbox = sentinel;              // any unrelated store field
    h.resetEpoch('send');
    assert.equal(h.voice.boundOutbox, sentinel, 'outbox object identity untouched');
    assert.equal(h.voice.boundOutbox.text, 'pending', 'outbox contents untouched');
  });
});

describe('#43 reversibility (flag OFF = legacy behavior)', () => {
  it('legacy bufferText==="" -> discard inference still fires when the flag is OFF', () => {
    const h = makeHarness({ resetMode: false });
    const ws = h.startListening();
    ws.fireFinal('legacy text', 0);
    h.clearBox();
    assert.ok(ws.controls().includes('discard'),
      'flag OFF preserves the legacy empty-buffer discard inference');
  });
});
