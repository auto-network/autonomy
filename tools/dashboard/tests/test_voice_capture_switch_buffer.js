// #23 switch-takes-buffer (voice-capture carry-over).
// When the dictation target is switched while the buffer holds text, the capture
// side resets s.finals for the new session — so without a carry-over the new
// session's first transcript OVERWRITES the retained text. This verifies the
// carryPrefix mechanism: the old text leads, the new session's transcript
// appends, and Clear/Send clears the carry so it can't re-prepend.

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const CAPTURE_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/voice-capture.js');

function makeHarness() {
  const sockets = [];
  class FakeWS {
    constructor(url) {
      this.url = url;
      this.readyState = 0;          // CONNECTING
      this._listeners = {};
      this.sent = [];
      sockets.push(this);
    }
    addEventListener(type, cb) { (this._listeners[type] ||= []).push(cb); }
    send(data) { this.sent.push(data); }
    close() { this.readyState = 3; this._fire('close', {}); }
    _fire(type, ev) { for (const cb of (this._listeners[type] || [])) cb(ev); }
    fireOpen() { this.readyState = 1; this._fire('open', {}); }
    fireTranscript(kind, text) {
      this._fire('message', { data: JSON.stringify({ type: 'transcript', kind, text }) });
    }
    fireBufferState(text) {
      this._fire('message', { data: JSON.stringify({ type: 'buffer_state', text }) });
    }
  }
  FakeWS.OPEN = 1;

  const effects = [];
  const stores = {};
  const alpine = {
    effect(fn) { effects.push(fn); fn(); },
    store(name, obj) { if (obj !== undefined) { stores[name] = obj; return obj; } return stores[name]; },
  };
  const docListeners = {};
  const document = {
    visibilityState: 'visible',
    addEventListener(name, cb) { (docListeners[name] ||= []).push(cb); },
    removeEventListener() {},
  };
  const voice = {
    boundSessionId: '', micMode: 'idle', bufferText: '', enabled: true, sheetError: '',
    setBufferText(t) { this.bufferText = t; },
    setConnState() {},
  };
  stores.voice = voice;

  const sandbox = {
    console, setTimeout, clearTimeout, Promise, JSON, Math, Date, Object, Array, String,
    WebSocket: FakeWS,
    location: { protocol: 'https:', host: 'localhost:8080' },
    navigator: {},                    // no wakeLock / mediaDevices
    document,
    window: { console, Autonomy: {}, addEventListener() {}, removeEventListener() {} },
  };
  sandbox.window.document = document;
  sandbox.window.Alpine = alpine;
  sandbox.Alpine = alpine;
  sandbox.globalThis = sandbox;
  // AudioContext / AudioWorkletNode intentionally absent → ensureMicReady rejects,
  // but the socket + message handler are attached BEFORE the mic path, so the
  // transcript/render logic under test runs fully.
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(CAPTURE_JS, 'utf8'), sandbox, { filename: 'voice-capture.js' });

  for (const cb of (docListeners['alpine:init'] || [])) cb();

  function runEffects() { for (const fn of effects) fn(); }
  // startListening sets s.starting=true and only clears it once ensureMicReady's
  // promise settles (it rejects here — no audio in the harness). Flush microtasks
  // so a follow-up switch isn't blocked by the stale starting guard.
  const flush = () => new Promise((r) => setImmediate(r));

  return {
    voice, sockets, runEffects, flush,
    state: sandbox.window.Autonomy.voiceCapture._state,
  };
}

describe('#23 voice-capture switch-takes-buffer', () => {
  it('carries the buffer to the new session so its transcript APPENDS, not overwrites', async () => {
    const h = makeHarness();

    // Bind session A and dictate "hello world".
    h.voice.boundSessionId = 'auto-A';
    h.voice.micMode = 'listening';
    h.runEffects();
    await h.flush();
    assert.equal(h.sockets.length, 1, 'a socket opens for A');
    h.sockets[0].fireOpen();
    h.sockets[0].fireTranscript('final', 'hello world');
    assert.equal(h.voice.bufferText, 'hello world');

    // Switch the binding to session B WHILE the buffer is full.
    h.voice.boundSessionId = 'auto-B';
    h.runEffects();
    await h.flush();
    assert.equal(h.state.carryPrefix, 'hello world', 'old text is carried into the switch');
    assert.equal(h.sockets.length, 2, 'a fresh socket opens for B');

    // The fresh B session re-sends an (empty) manager buffer — must NOT wipe the carry.
    h.sockets[1].fireOpen();
    h.sockets[1].fireBufferState('');
    assert.equal(h.voice.bufferText, 'hello world', 'empty buffer_state keeps the carried text');

    // New speech on B appends after the carried text.
    h.sockets[1].fireTranscript('final', 'next sentence');
    assert.equal(h.voice.bufferText, 'hello world next sentence');
  });

  it('Clear after a switch resets the carry so it cannot re-prepend', async () => {
    const h = makeHarness();
    h.voice.boundSessionId = 'auto-A';
    h.voice.micMode = 'listening';
    h.runEffects();
    await h.flush();
    h.sockets[0].fireOpen();
    h.sockets[0].fireTranscript('final', 'alpha');
    h.voice.boundSessionId = 'auto-B';
    h.runEffects();
    await h.flush();
    assert.equal(h.state.carryPrefix, 'alpha');

    // Operator clears the box (bufferText emptied) → carry must be dropped.
    h.voice.setBufferText('');
    h.runEffects();
    assert.equal(h.state.carryPrefix, '', 'carry cleared on Clear/Send');
    assert.equal(h.state.finals, '', 'finals cleared too');

    // A subsequent render starts clean (no stale carried prefix).
    h.sockets[1].fireOpen();
    h.sockets[1].fireTranscript('final', 'beta');
    assert.equal(h.voice.bufferText, 'beta');
  });

  it('does nothing unusual when there is no buffer at switch time (carry stays empty)', async () => {
    const h = makeHarness();
    h.voice.boundSessionId = 'auto-A';
    h.voice.micMode = 'listening';
    h.runEffects();
    await h.flush();
    h.sockets[0].fireOpen();
    // No transcript yet — switch with an empty box.
    h.voice.boundSessionId = 'auto-B';
    h.runEffects();
    await h.flush();
    assert.equal(h.state.carryPrefix, '', 'no carry when nothing was buffered');
    h.sockets[1].fireOpen();
    h.sockets[1].fireTranscript('final', 'fresh start');
    assert.equal(h.voice.bufferText, 'fresh start');
  });
});
