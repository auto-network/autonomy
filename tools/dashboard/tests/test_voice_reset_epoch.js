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
  let nowMs = opts.nowMs || 1_000_000;
  class FakeDate extends Date {
    static now() { return nowMs; }
  }
  const stored = new Map();
  const localStorage = {
    getItem(key) { return stored.has(key) ? stored.get(key) : null; },
    setItem(key, value) { stored.set(key, String(value)); },
    removeItem(key) { stored.delete(key); },
  };
  const fetches = [];
  let resolveUpload = null;
  const fetch = (url, init) => {
    fetches.push({ url, init });
    if (opts.deferUpload) {
      return new Promise((resolve) => { resolveUpload = resolve; });
    }
    return Promise.resolve({ ok: opts.uploadOk !== false });
  };
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
    console, setTimeout, clearTimeout, setInterval, clearInterval, Promise, JSON, Math,
    Date: FakeDate, Object, Array, String, localStorage, fetch,
    WebSocket: FakeWS,
    location: { protocol: 'https:', host: 'localhost:8080', search: opts.search || '' },
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
    voice, sockets, renders, runEffects, cap, fetches, localStorage,
    advanceTime(ms) { nowMs += ms; },
    resolveUpload(ok) { resolveUpload({ ok: ok !== false }); },
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

  // REGRESSION (live failure 2026-06-06): the server epoch is PER-CONNECTION and
  // resets to 0 on every fresh ws_voice connection, but acceptEpoch/serverEpoch are
  // module state that survive reconnects. After a Send/Clear bumped acceptEpoch, a
  // WS reconnect left it stale-high, so the new connection's epoch-0 frames were ALL
  // dropped — dictation died silently (Whisper transcribed fine; nothing rendered).
  it('6. a WS reconnect resets the epoch baseline so fresh epoch-0 frames are accepted', () => {
    const h = makeHarness({ resetMode: true });
    const ws = h.startListening();
    ws.fireFinal('before reset', 0);
    h.resetEpoch('send');                         // acceptEpoch -> 1
    assert.equal(h.cap._state.acceptEpoch, 1, 'reset raised the bar');

    ws.fireOpen();                                // simulate a reconnect (open fires again)
    assert.equal(h.cap._state.acceptEpoch, 0, 'reconnect re-syncs the epoch baseline to 0');
    assert.equal(h.cap._state.serverEpoch, 0, 'serverEpoch also reset for the fresh connection');

    h.renders.length = 0;
    ws.fireFinal('after the reconnect', 0);       // server epoch is fresh (0) again
    assert.ok(h.renders.some((r) => /after the reconnect/.test(r)),
      'epoch-0 frames on the new connection are accepted, not silently dropped');
  });

  it('7. Clear drops a carried prefix before accepting the reset acknowledgment', () => {
    const h = makeHarness({ resetMode: true });
    const ws = h.startListening();

    // Represents text retained across a target-session switch.
    h.cap._state.carryPrefix = 'Ugh';
    h.voice.setBufferText('Ugh');

    // Actual Clear ordering: empty the visible store, then reset capture.
    h.voice.setBufferText('');
    h.resetEpoch('clear');

    assert.equal(h.cap._state.carryPrefix, '');

    // The server correctly acknowledges the reset with an empty,
    // incremented-epoch buffer. This must remain empty.
    ws.fireBufferState('', 1);
    assert.equal(h.voice.bufferText, '');
  });

  it('8. reset-mode Clear schedules a buffered trace dump when tracing is enabled', () => {
    const h = makeHarness({ resetMode: true });
    h.cap.startTrace();

    h.resetEpoch('clear');

    assert.ok(h.cap._state.traceDumpTimer,
      'Clear should schedule one post-reset trace upload');
    clearTimeout(h.cap._state.traceDumpTimer);
  });

  it('9. trace activation expires after one hour even while the page stays open', () => {
    const h = makeHarness({ resetMode: true });
    h.cap.startTrace();
    const expiresAt = Number(h.localStorage.getItem('voice-trace'));
    assert.equal(expiresAt, 1_000_000 + 60 * 60 * 1000);

    h.advanceTime(60 * 60 * 1000 + 1);
    h.resetEpoch('send');

    assert.equal(h.cap._state.traceOn, false);
    assert.equal(h.localStorage.getItem('voice-trace'), null);
    assert.equal(h.cap._state.trace.length, 0);
  });

  it('10. tracing is off by default', () => {
    const h = makeHarness({ resetMode: true });

    h.resetEpoch('send');

    assert.equal(h.cap._state.traceOn, false);
    assert.equal(h.localStorage.getItem('voice-trace'), null);
    assert.equal(h.cap._state.trace.length, 0);
  });

  it('11. a successful upload advances the trace ring instead of repeating history', async () => {
    const h = makeHarness({ resetMode: true });
    h.cap.startTrace();
    h.resetEpoch('send');
    assert.equal(h.cap._state.trace.length, 1);

    h.cap.dumpTrace();
    await Promise.resolve();
    await Promise.resolve();

    assert.equal(h.fetches.length, 1);
    assert.equal(h.fetches[0].url, '/api/voice/trace');
    assert.equal(h.cap._state.trace.length, 0);
  });

  it('12. a failed upload retains the ring for a later retry', async () => {
    const h = makeHarness({ resetMode: true, uploadOk: false });
    h.cap.startTrace();
    h.resetEpoch('send');

    h.cap.dumpTrace();
    await Promise.resolve();
    await Promise.resolve();

    assert.equal(h.cap._state.trace.length, 1);
  });

  it('13. a successful upload retains events appended while it was in flight', async () => {
    const h = makeHarness({ resetMode: true, deferUpload: true });
    h.cap.startTrace();
    h.resetEpoch('send');

    const upload = h.cap.dumpTrace();
    h.resetEpoch('clear');
    assert.equal(h.cap._state.trace.length, 2);
    h.resolveUpload(true);
    await upload;

    assert.equal(h.cap._state.trace.length, 1);
    assert.equal(h.cap._state.trace[0].v.reason, 'clear');
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
