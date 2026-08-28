const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const CAPTURE_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/voice-capture.js');

function makeHarness(options = {}) {
  const sockets = [];
  class FakeWS {
    constructor() { this.readyState = 0; this.listeners = {}; sockets.push(this); }
    addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
    send() {}
    close() { this.readyState = 3; }
    fire(type, event) { for (const callback of this.listeners[type] || []) callback(event || {}); }
    open() { this.readyState = 1; this.fire('open'); }
    deliver(frame) { this.fire('message', { data: JSON.stringify(frame) }); }
  }
  FakeWS.OPEN = 1;

  const effects = [];
  const stores = {};
  const voice = {
    boundSessionId: '', micMode: 'idle', enabled: true, bufferText: '',
    connState: 'ok', sheetError: '',
    setConnState(value) { this.connState = value; },
    setBufferText(value) { this.bufferText = value; },
  };
  stores.voice = voice;
  const Alpine = {
    effect(callback) { effects.push(callback); callback(); },
    store(name) { return stores[name]; },
  };
  const documentListeners = {};
  const document = {
    visibilityState: 'visible',
    querySelector() { return null; },
    addEventListener(type, callback) { (documentListeners[type] ||= []).push(callback); },
  };
  const noop = () => {};
  const sandbox = {
    console, WebSocket: FakeWS, Alpine, document,
    navigator: options.navigator || {}, location: { protocol: 'https:', host: 'localhost:8080' },
    setTimeout: () => 0, clearTimeout: noop,
    setInterval: () => ({ unref: noop }), clearInterval: noop,
    Promise, JSON, Math, Date, Number, Object, Array, String,
  };
  sandbox.window = {
    console, Alpine, document, Autonomy: {},
    addEventListener: noop, removeEventListener: noop,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(CAPTURE_JS, 'utf8'), sandbox, {
    filename: 'voice-capture.js',
  });
  for (const callback of documentListeners['alpine:init'] || []) callback();

  voice.boundSessionId = 'auto-A';
  voice.micMode = 'listening';
  for (const effect of effects) effect();
  const socket = sockets.at(-1);
  socket.open();
  return {
    voice, socket,
    api: sandbox.window.Autonomy.voiceCapture,
    state: sandbox.window.Autonomy.voiceCapture._state,
  };
}

describe('voice flow health acknowledgements', () => {
  it('does not claim a newly opened or merely state-acknowledged socket is healthy', () => {
    const h = makeHarness();
    assert.equal(h.voice.connState, 'reconnecting');

    h.socket.deliver({
      type: 'voice_state', connection_id: 'current',
      fsm_state: 'listening', upstream: 'ready', epoch: 0,
    });
    assert.equal(h.voice.connState, 'reconnecting');

    h.socket.deliver({
      type: 'audio_flow', connection_id: 'current',
      received: 1, forwarded: 1, ts_ms: Date.now(),
    });
    assert.equal(h.voice.connState, 'ok');
    assert.equal(h.state.lastForwarded, 1);
  });

  it('ignores stale connection IDs and non-advancing flow counters', () => {
    const h = makeHarness();
    h.socket.deliver({
      type: 'voice_state', connection_id: 'current',
      fsm_state: 'listening', upstream: 'ready', epoch: 0,
    });
    h.socket.deliver({
      type: 'audio_flow', connection_id: 'stale',
      received: 9, forwarded: 9, ts_ms: Date.now(),
    });
    assert.equal(h.voice.connState, 'reconnecting');

    h.socket.deliver({
      type: 'audio_flow', connection_id: 'current',
      received: 1, forwarded: 1, ts_ms: Date.now(),
    });
    h.socket.deliver({
      type: 'audio_flow', connection_id: 'current',
      received: 2, forwarded: 1, ts_ms: Date.now(),
    });
    assert.equal(h.state.lastForwarded, 1);
  });

  it('accepts verified mute but exposes an unavailable upstream as disconnected', () => {
    const h = makeHarness();
    h.socket.deliver({
      type: 'voice_state', connection_id: 'current',
      fsm_state: 'muted', upstream: 'ready', epoch: 0,
    });
    assert.equal(h.voice.connState, 'ok');

    h.socket.deliver({
      type: 'voice_state', connection_id: 'current',
      fsm_state: 'listening', upstream: 'unavailable', epoch: 0,
    });
    assert.equal(h.voice.connState, 'disconnected');
    assert.equal(h.state.requiresReconnect, true);
  });

  it('keeps automatic restore tappable when iPhone denies the wake lock', async () => {
    const h = makeHarness({
      navigator: {
        wakeLock: { request() { return Promise.reject(new Error('gesture required')); } },
      },
    });
    h.api.activateFromGesture();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(h.state.wakeLockNeedsGesture, true);

    h.socket.deliver({
      type: 'voice_state', connection_id: 'current',
      fsm_state: 'listening', upstream: 'ready', epoch: 0,
    });
    h.socket.deliver({
      type: 'audio_flow', connection_id: 'current',
      received: 1, forwarded: 1, ts_ms: Date.now(),
    });
    assert.equal(h.voice.connState, 'disconnected');
    assert.match(h.voice.sheetError, /keep-awake/i);
  });

  it('accepts healthy flow after a user-gesture wake lock succeeds', async () => {
    const sentinel = { addEventListener() {}, release() {} };
    const h = makeHarness({
      navigator: { wakeLock: { request() { return Promise.resolve(sentinel); } } },
    });
    h.api.activateFromGesture();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(h.state.wakeLock, sentinel);
    assert.equal(h.state.wakeLockNeedsGesture, false);

    h.socket.deliver({
      type: 'voice_state', connection_id: 'current',
      fsm_state: 'listening', upstream: 'ready', epoch: 0,
    });
    h.socket.deliver({
      type: 'audio_flow', connection_id: 'current',
      received: 1, forwarded: 1, ts_ms: Date.now(),
    });
    assert.equal(h.voice.connState, 'ok');
  });
});
