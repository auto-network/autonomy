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
    constructor() { this.readyState = 0; this.listeners = {}; this.sent = []; sockets.push(this); }
    addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
    send(payload) { this.sent.push(payload); }
    close() { this.readyState = 3; }
    fire(type, event) { for (const callback of this.listeners[type] || []) callback(event || {}); }
    open() { this.readyState = 1; this.fire('open'); }
    deliver(frame) { this.fire('message', { data: JSON.stringify(frame) }); }
  }
  FakeWS.OPEN = 1;

  class FakeAudioContext {
    constructor() {
      this.state = 'running';
      this.destination = {};
      this.audioWorklet = { addModule() { return Promise.resolve(); } };
    }
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createGain() { return { gain: { value: 1 }, connect() {} }; }
    resume() { this.state = 'running'; return Promise.resolve(); }
    close() { this.state = 'closed'; return Promise.resolve(); }
  }
  class FakeWorkletNode {
    constructor() { this.port = { onmessage: null }; }
    connect() {}
    disconnect() {}
  }
  const baseNavigator = {
    mediaDevices: {
      getUserMedia() {
        return Promise.resolve({
          getTracks() {
            return [{
              readyState: 'live', enabled: true, muted: false,
              addEventListener() {}, stop() {},
            }];
          },
        });
      },
    },
  };
  const navigator = Object.assign(baseNavigator, options.navigator || {});

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
    navigator, location: { protocol: 'https:', host: 'localhost:8080' },
    AudioContext: FakeAudioContext, AudioWorkletNode: FakeWorkletNode, ArrayBuffer,
    setTimeout: () => 0, clearTimeout: noop,
    setInterval: () => ({ unref: noop }), clearInterval: noop,
    Promise, JSON, Math, Date, Number, Object, Array, String,
  };
  sandbox.window = {
    console, Alpine, document, Autonomy: {},
    AudioContext: FakeAudioContext, AudioWorkletNode: FakeWorkletNode,
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

function makeCaptureHealthy(h) {
  h.state.ctx = { state: 'running' };
  h.state.stream = {
    getTracks() {
      return [{ readyState: 'live', enabled: true, muted: false }];
    },
  };
  h.state.lastFrameAt = Date.now();
  h.state.lastLocalSendAt = Date.now();
}

describe('voice flow health acknowledgements', () => {
  it('does not claim a newly opened or merely state-acknowledged socket is healthy', () => {
    const h = makeHarness();
    makeCaptureHealthy(h);
    h.state.talkActive = true;
    assert.equal(h.voice.connState, 'reconnecting');

    h.socket.deliver({
      type: 'voice_state', connection_id: 'current',
      fsm_state: 'listening', upstream: 'ready', epoch: 0,
    });
    assert.equal(h.voice.connState, 'reconnecting');
    assert.equal(h.socket.sent.length, 0);

    h.socket.deliver({
      type: 'audio_flow', connection_id: 'current',
      received: 1, forwarded: 1, ts_ms: Date.now(),
    });
    assert.equal(h.voice.connState, 'ok');
    assert.equal(h.state.lastForwarded, 1);
  });

  it('reopens audio in wire order after reset and commit results', () => {
    const h = makeHarness();
    h.state.talkActive = true;
    h.socket.deliver({
      type: 'voice_state', connection_id: 'current',
      fsm_state: 'listening', upstream: 'ready', epoch: 0,
    });
    h.socket.deliver({
      type: 'buffer_state', text: '', epoch: 1,
      audio_ready_token: 'reset-token',
    });
    assert.deepEqual(JSON.parse(h.socket.sent.at(-1)), {
      type: 'audio_ready', connection_id: 'current', epoch: 1,
      token: 'reset-token',
    });
    h.socket.deliver({ type: 'committed', audio_ready_token: 'commit-token' });
    assert.deepEqual(JSON.parse(h.socket.sent.at(-1)), {
      type: 'audio_ready', connection_id: 'current', epoch: 1,
      token: 'commit-token',
    });
  });

  it('ignores stale connection IDs and non-advancing flow counters', () => {
    const h = makeHarness();
    makeCaptureHealthy(h);
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

  it('accepts verified mute but repairs an unavailable upstream', () => {
    const h = makeHarness();
    h.voice.micMode = 'muted';
    h.socket.deliver({
      type: 'voice_state', connection_id: 'current',
      fsm_state: 'muted', upstream: 'ready', epoch: 0,
    });
    assert.equal(h.voice.connState, 'ok');

    h.socket.deliver({
      type: 'voice_state', connection_id: 'current',
      fsm_state: 'listening', upstream: 'unavailable', epoch: 0,
    });
    assert.equal(h.voice.connState, 'reconnecting');
    assert.equal(h.voice.transportStatus, 'connecting');
    assert.equal(h.voice.recoveryIncident.transportAttempts, 1);
  });

  it('keeps automatic restore tappable when iPhone denies the wake lock', async () => {
    const h = makeHarness({
      navigator: {
        wakeLock: { request() { return Promise.reject(new Error('gesture required')); } },
      },
    });
    h.api.activateFromGesture();
    await new Promise((resolve) => setImmediate(resolve));
    makeCaptureHealthy(h);
    assert.equal(h.state.wakeLockNeedsGesture, true);

    h.socket.deliver({
      type: 'voice_state', connection_id: 'current',
      fsm_state: 'listening', upstream: 'ready', epoch: 0,
    });
    h.socket.deliver({
      type: 'audio_flow', connection_id: 'current',
      received: 1, forwarded: 1, ts_ms: Date.now(),
    });
    assert.equal(h.voice.connState, 'ok');
    assert.equal(h.voice.wakeStatus, 'denied');
    assert.match(h.voice.sheetError, /keep-awake/i);
  });

  it('accepts healthy flow after a user-gesture wake lock succeeds', async () => {
    const sentinel = { addEventListener() {}, release() {} };
    const h = makeHarness({
      navigator: { wakeLock: { request() { return Promise.resolve(sentinel); } } },
    });
    h.api.activateFromGesture();
    await new Promise((resolve) => setImmediate(resolve));
    makeCaptureHealthy(h);
    assert.equal(h.state.wakeLock, sentinel);
    assert.equal(h.state.wakeLockNeedsGesture, false);
    assert.equal(h.voice.wakeStatus, 'held');

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
