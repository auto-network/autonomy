const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const STORE_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/voice-store.js');
const CAPTURE_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/voice-capture.js');

function makeHarness(options = {}) {
  let now = 1000;
  let nextTimer = 1;
  const timers = new Map();
  const logs = [];
  const sockets = [];
  const streams = [];
  const contexts = [];
  const worklets = [];
  const documentListeners = {};
  const windowListeners = {};
  const stores = {
    flags: {
      get(name) {
        return name === 'voice.client_enabled' ||
          (name === 'voice.reset_suppression' && options.resetMode === true);
      },
    },
  };
  const effects = [];

  class FakeDate extends Date {
    static now() { return now; }
  }

  function setTimer(callback, delay = 0) {
    const id = nextTimer++;
    timers.set(id, { at: now + Number(delay || 0), callback });
    return id;
  }

  function clearTimer(id) { timers.delete(id); }

  class FakeTrack {
    constructor() {
      this.readyState = 'live';
      this.enabled = true;
      this.muted = false;
      this.listeners = {};
    }
    addEventListener(type, callback) {
      (this.listeners[type] ||= []).push(callback);
    }
    fire(type) {
      if (type === 'mute') this.muted = true;
      if (type === 'unmute') this.muted = false;
      if (type === 'ended') this.readyState = 'ended';
      for (const callback of this.listeners[type] || []) callback({ type });
    }
    stop() { this.readyState = 'ended'; }
  }

  function newStream() {
    const track = new FakeTrack();
    const stream = { track, getTracks() { return [track]; } };
    streams.push(stream);
    return stream;
  }

  class FakeAudioContext {
    constructor() {
      logs.push('audio-context');
      this.state = options.contextState || 'running';
      this.destination = {};
      this.audioWorklet = { addModule() { return Promise.resolve(); } };
      contexts.push(this);
    }
    createMediaStreamSource() {
      return { connect() {}, disconnect() {} };
    }
    createGain() {
      return { gain: { value: 1 }, connect() {} };
    }
    resume() {
      logs.push('audio-resume');
      this.state = 'running';
      return Promise.resolve();
    }
    close() {
      this.state = 'closed';
      return Promise.resolve();
    }
  }

  class FakeWorkletNode {
    constructor() {
      this.port = { onmessage: null };
      worklets.push(this);
    }
    connect() {}
    disconnect() {}
  }

  class FakeWebSocket {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.listeners = {};
      this.sent = [];
      sockets.push(this);
    }
    addEventListener(type, callback) {
      (this.listeners[type] ||= []).push(callback);
    }
    fire(type, event = {}) {
      for (const callback of this.listeners[type] || []) callback(event);
    }
    open() {
      this.readyState = 1;
      this.fire('open');
    }
    send(payload) { this.sent.push(payload); }
    deliver(frame) {
      this.fire('message', { data: JSON.stringify(frame) });
    }
    close() { this.readyState = 3; }
  }
  FakeWebSocket.OPEN = 1;

  const resumeIntent = options.resume === false
    ? null
    : JSON.stringify({ sessionId: 'auto-A', micMode: 'listening' });
  const sessionData = resumeIntent
    ? { 'autonomy.voice.resumeIntent': resumeIntent }
    : {};
  const sessionStorage = {
    getItem(key) { return Object.prototype.hasOwnProperty.call(sessionData, key) ? sessionData[key] : null; },
    setItem(key, value) { sessionData[key] = String(value); },
    removeItem(key) { delete sessionData[key]; },
  };
  const localStorage = {
    getItem() { return null; }, setItem() {}, removeItem() {},
  };
  const document = {
    visibilityState: 'visible',
    querySelector() { return null; },
    addEventListener(type, callback) {
      (documentListeners[type] ||= []).push(callback);
    },
  };
  const navigator = {
    mediaDevices: {
      getUserMedia() {
        logs.push('get-user-media');
        if (options.micError) return Promise.reject(options.micError);
        const stream = newStream();
        if (options.micRequests) {
          return new Promise((resolve, reject) => {
            options.micRequests.push({ stream, resolve: () => resolve(stream), reject });
          });
        }
        return Promise.resolve(stream);
      },
    },
    wakeLock: {
      request() {
        logs.push('wake-lock');
        if (options.wakeError) return Promise.reject(options.wakeError);
        const listeners = {};
        const sentinel = {
          released: false,
          addEventListener(type, callback) { (listeners[type] ||= []).push(callback); },
          release() {
            this.released = true;
            for (const callback of listeners.release || []) callback({ type: 'release' });
            return Promise.resolve();
          },
        };
        if (options.wakeRequests) {
          return new Promise((resolve, reject) => {
            options.wakeRequests.push({ sentinel, resolve: () => resolve(sentinel), reject });
          });
        }
        return Promise.resolve(sentinel);
      },
    },
  };
  const Alpine = {
    effect(callback) { effects.push(callback); callback(); },
    store(name, value) {
      if (value !== undefined) { stores[name] = value; return value; }
      return stores[name];
    },
  };
  const windowObj = {
    console, Alpine, document, navigator, sessionStorage, localStorage,
    AudioContext: FakeAudioContext, AudioWorkletNode: FakeWorkletNode,
    Autonomy: {},
    addEventListener(type, callback) {
      (windowListeners[type] ||= []).push(callback);
    },
    removeEventListener() {},
  };
  const sandbox = {
    console, window: windowObj, document, navigator, Alpine,
    sessionStorage, localStorage,
    WebSocket: FakeWebSocket,
    AudioContext: FakeAudioContext,
    AudioWorkletNode: FakeWorkletNode,
    location: { protocol: 'https:', host: 'localhost:8080' },
    setTimeout: setTimer, clearTimeout: clearTimer,
    setInterval() { return { unref() {} }; }, clearInterval() {},
    Promise, JSON, Math, Date: FakeDate, Number, Object, Array, String,
    Boolean, Error, ArrayBuffer, Uint8Array, parseInt, parseFloat, FormData,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(STORE_JS, 'utf8'), sandbox, { filename: 'voice-store.js' });
  vm.runInContext(fs.readFileSync(CAPTURE_JS, 'utf8'), sandbox, { filename: 'voice-capture.js' });
  for (const callback of documentListeners['alpine:init'] || []) callback();

  async function flush() {
    for (let index = 0; index < 40; index += 1) await Promise.resolve();
  }

  async function advance(ms) {
    const target = now + ms;
    while (true) {
      let nextId = null;
      let next = null;
      for (const [id, timer] of timers) {
        if (timer.at <= target && (!next || timer.at < next.at)) {
          nextId = id;
          next = timer;
        }
      }
      if (!next) break;
      now = next.at;
      timers.delete(nextId);
      next.callback();
      await flush();
    }
    now = target;
    await flush();
  }

  function frame() {
    const node = worklets.at(-1);
    assert.ok(node && node.port.onmessage, 'capture worklet is ready');
    node.port.onmessage({ data: { type: 'audio', buffer: new ArrayBuffer(8) } });
  }

  function verifyFlow(connectionId = 'connection-current', forwarded = 1) {
    const socket = sockets.at(-1);
    socket.deliver({
      type: 'voice_state', connection_id: connectionId,
      fsm_state: 'listening', upstream: 'ready', epoch: 0,
    });
    socket.deliver({
      type: 'audio_flow', connection_id: connectionId,
      received: forwarded, forwarded, ts_ms: now,
    });
  }

  return {
    get voice() { return stores.voice; },
    get socket() { return sockets.at(-1); },
    get stream() { return streams.at(-1); },
    sockets, streams, contexts, worklets, logs, effects,
    api: windowObj.Autonomy.voiceCapture,
    flush, advance, frame, verifyFlow,
    runEffects() { for (const effect of effects) effect(); },
    fireWindow(type, detail) {
      for (const callback of windowListeners[type] || []) callback({ type, detail });
    },
  };
}

async function startRestored(h) {
  assert.equal(h.voice.effectiveState, 'checking');
  assert.ok(h.socket, 'restored intent starts a current-document socket');
  h.socket.open();
  await h.flush();
  h.frame();
}

describe('verified dictation health coordinator', () => {
  it('preserves the exact capture through a long system-authentication interruption', async () => {
    const h = makeHarness();
    await startRestored(h);
    h.verifyFlow();
    const stream = h.stream;
    const context = h.contexts.at(-1);
    const worklet = h.worklets.at(-1);
    const socket = h.socket;
    const captureGen = h.api._state.captureGen;
    const micRequests = h.logs.filter((entry) => entry === 'get-user-media').length;

    h.fireWindow('autonomy:system-auth-change', { active: true, depth: 1, reason: 'webauthn' });
    stream.track.fire('mute');
    context.state = 'interrupted';
    context.onstatechange();
    await h.advance(30000);
    h.api._audioWatchdogTick();
    stream.track.fire('unmute');
    context.state = 'running';
    context.onstatechange();
    h.frame();
    h.fireWindow('autonomy:system-auth-change', { active: false, depth: 0, reason: 'webauthn' });
    await h.flush();

    assert.equal(h.stream, stream);
    assert.equal(h.contexts.at(-1), context);
    assert.equal(h.worklets.at(-1), worklet);
    assert.equal(h.socket, socket);
    assert.equal(h.api._state.captureGen, captureGen);
    assert.equal(h.logs.filter((entry) => entry === 'get-user-media').length, micRequests);
    assert.equal(h.voice.actionRequiredReason, null);
  });

  it('records an ended track during system authentication without automatic acquisition', async () => {
    const h = makeHarness();
    await startRestored(h);
    const stream = h.stream;
    const requests = h.logs.filter((entry) => entry === 'get-user-media').length;
    h.fireWindow('autonomy:system-auth-change', { active: true, depth: 1, reason: 'webauthn' });
    stream.track.fire('ended');
    assert.equal(h.voice.captureStatus, 'interrupted');
    h.fireWindow('autonomy:system-auth-change', { active: false, depth: 0, reason: 'webauthn' });
    await h.flush();
    assert.equal(h.voice.actionRequiredReason, 'mic_gesture');
    assert.equal(h.stream, stream);
    assert.equal(h.logs.filter((entry) => entry === 'get-user-media').length, requests);
  });

  it('keeps restored intent checking until current capture and server flow agree', async () => {
    const h = makeHarness();
    await startRestored(h);
    assert.equal(h.voice.captureStatus, 'live');
    assert.equal(h.voice.transportStatus, 'connecting');
    assert.equal(h.voice.effectiveState, 'checking');

    h.verifyFlow();
    assert.equal(h.voice.transportStatus, 'flowing');
    assert.equal(h.voice.effectiveState, 'listening');

    h.socket.deliver({
      type: 'audio_flow', connection_id: 'stale-connection',
      received: 99, forwarded: 99, ts_ms: 2000,
    });
    assert.equal(h.api._state.lastForwarded, 1, 'stale connection evidence is ignored');
  });

  it('gives a temporary track mute grace, then requires an explicit resume without rebuilding', async () => {
    const h = makeHarness();
    await startRestored(h);
    h.verifyFlow();
    const initialStreams = h.streams.length;

    h.stream.track.fire('mute');
    assert.equal(h.voice.captureStatus, 'interrupted');
    assert.equal(h.voice.effectiveState, 'checking');
    await h.advance(999);
    h.stream.track.fire('unmute');
    h.frame();
    assert.equal(h.streams.length, initialStreams, 'unmute inside grace does not rebuild');

    h.stream.track.fire('mute');
    await h.advance(1000);
    assert.equal(h.streams.length, initialStreams);
    assert.equal(h.voice.actionRequiredReason, 'mic_gesture');
    h.api._audioWatchdogTick();
    h.fireWindow('focus');
    await h.flush();
    assert.equal(h.streams.length, initialStreams, 'concurrent triggers never rebuild capture');
  });

  it('requires a gesture when a worklet never emits its first frame', async () => {
    const h = makeHarness();
    h.socket.open();
    await h.flush();
    const before = h.streams.length;
    await h.advance(4001);
    h.api._audioWatchdogTick();
    await h.flush();
    assert.equal(h.voice.actionRequiredReason, 'mic_gesture');
    assert.equal(h.streams.length, before);
  });

  it('reports ended capture immediately without automatic restart storms', async () => {
    const h = makeHarness();
    await startRestored(h);
    h.verifyFlow();
    const before = h.streams.length;

    h.stream.track.fire('ended');
    await h.flush();
    assert.equal(h.streams.length, before);
    assert.equal(h.voice.actionRequiredReason, 'mic_gesture');
    assert.equal(h.voice.effectiveState, 'enable_required');
    h.api._audioWatchdogTick();
    await h.advance(5000);
    assert.equal(h.streams.length, before, 'automatic repair never starts');
  });

  it('does not replace the socket or clear capture loss when an old flow acknowledgement arrives', async () => {
    const h = makeHarness();
    await startRestored(h);
    h.verifyFlow('connection-current', 1);
    const oldSocket = h.socket;
    h.stream.track.fire('ended');
    await h.flush();
    assert.equal(h.socket, oldSocket, 'capture loss leaves transport untouched');

    oldSocket.deliver({
      type: 'audio_flow', connection_id: 'connection-current',
      received: 2, forwarded: 2, ts_ms: 1001,
    });
    assert.equal(h.voice.effectiveState, 'enable_required');
    assert.equal(h.voice.actionRequiredReason, 'mic_gesture');
  });

  it('keeps a late old acquisition from replacing a newer session binding', async () => {
    const micRequests = [];
    const h = makeHarness({ micRequests });
    assert.equal(micRequests.length, 1, 'session A acquisition is pending');

    h.voice.boundSessionId = 'auto-B';
    h.runEffects();
    assert.equal(micRequests.length, 2, 'session B starts without waiting for A');
    const socketB = h.socket;
    assert.match(socketB.url, /bind=auto-B/);
    socketB.open();
    micRequests[1].resolve();
    await h.flush();
    h.frame();
    const sentBefore = socketB.sent.length;

    micRequests[0].resolve();
    await h.flush();
    assert.equal(h.api._state.bind, 'auto-B');
    assert.equal(h.api._state.stream, micRequests[1].stream);
    assert.equal(micRequests[0].stream.track.readyState, 'ended', 'obsolete A stream is stopped');
    h.frame();
    assert.equal(socketB.sent.length, sentBefore + 1, 'only B pipeline can send audio');
  });

  it('continues the existing socket backoff after a replacement also closes', async () => {
    const h = makeHarness();
    await startRestored(h);
    h.verifyFlow();
    h.socket.close();
    h.socket.fire('close');
    await h.advance(1300);
    assert.equal(h.sockets.length, 2, 'first retry creates a replacement');

    h.socket.open();
    await h.flush();
    h.socket.close();
    h.socket.fire('close');
    await h.advance(2500);
    assert.equal(h.sockets.length, 3, 'second backoff retry is not suppressed by the incident');
    assert.equal(h.api._state.reconnectAttempt, 2);
  });

  it('requires a post-resume worklet frame before capture is live again', async () => {
    const h = makeHarness();
    await startRestored(h);
    h.verifyFlow();
    const context = h.contexts.at(-1);

    context.state = 'interrupted';
    context.onstatechange();
    assert.equal(h.voice.captureStatus, 'interrupted');
    assert.equal(h.voice.effectiveState, 'checking');

    context.state = 'running';
    context.onstatechange();
    assert.equal(h.voice.captureStatus, 'acquiring');
    assert.equal(h.voice.effectiveState, 'checking', 'historical frame time cannot restore listening');
    h.frame();
    assert.equal(h.voice.effectiveState, 'listening');
  });

  it('does not start an automatic acquisition after an ended track', async () => {
    const micRequests = [];
    const h = makeHarness({ micRequests });
    h.socket.open();
    micRequests[0].resolve();
    await h.flush();
    h.frame();
    h.verifyFlow();

    h.stream.track.fire('ended');
    await h.flush();
    assert.equal(micRequests.length, 1);
    assert.equal(h.voice.effectiveState, 'enable_required');
    assert.equal(h.api._state.talkActive, false);
  });

  it('does not treat an intentional reset boundary as a flow stall', async () => {
    const h = makeHarness({ resetMode: true });
    await startRestored(h);
    h.verifyFlow('connection-current', 1);
    const socketCount = h.sockets.length;
    const captureStream = h.stream;
    const captureContext = h.contexts.at(-1);
    const captureWorklet = h.worklets.at(-1);
    assert.equal(h.voice.effectiveState, 'listening');
    assert.equal(h.voice.transportStatus, 'flowing');

    assert.equal(h.api.resetEpoch('send'), true);
    assert.equal(h.voice.effectiveState, 'listening', 'Send does not flap verified health while reset is pending');
    await h.advance(4000);
    h.frame();
    h.api._audioWatchdogTick();
    assert.equal(h.sockets.length, socketCount, 'reset wait does not replace the socket');
    assert.equal(h.api._state.resetBoundaryPending, true);
    assert.equal(h.voice.effectiveState, 'listening', 'intentional reset wait stays healthy');

    h.socket.deliver({
      type: 'buffer_state', text: '', epoch: 1,
      audio_ready_token: 'reset-ready-token',
    });
    assert.equal(h.api._state.resetBoundaryPending, false);
    assert.equal(h.voice.effectiveState, 'listening', 'Send preserves verified microphone health');
    assert.equal(h.voice.transportStatus, 'flowing', 'Send does not flap the audio transport state');
    assert.equal(h.stream, captureStream, 'Send keeps the current microphone stream');
    assert.equal(h.contexts.at(-1), captureContext, 'Send keeps the current AudioContext');
    assert.equal(h.worklets.at(-1), captureWorklet, 'Send keeps the current audio worklet');
    await h.advance(22000);
    assert.equal(h.voice.actionRequiredReason, null, 'Send does not arm a false recovery deadline');
    assert.equal(h.socket.readyState, 1, 'healthy browser voice socket remains open after Send');
  });

  it('keeps a newer reset pending when an older reset result arrives', async () => {
    const h = makeHarness({ resetMode: true });
    await startRestored(h);
    h.verifyFlow('connection-current', 1);

    assert.equal(h.api.resetEpoch('send'), true);
    assert.equal(h.api.resetEpoch('clear'), true);
    assert.equal(h.api._state.acceptEpoch, 2);
    assert.equal(h.api._state.resetBoundaryExpectedEpoch, 2);

    h.socket.deliver({
      type: 'buffer_state', text: 'stale', epoch: 1,
      audio_ready_token: 'older-reset-token',
    });
    assert.equal(h.api._state.resetBoundaryPending, true);
    assert.equal(h.voice.bufferText, '', 'older reset body remains below the acceptance floor');

    h.socket.deliver({
      type: 'buffer_state', text: '', epoch: 2,
      audio_ready_token: 'newer-reset-token',
    });
    assert.equal(h.api._state.resetBoundaryPending, false);
  });

  it('gives a reset its own deadline instead of inheriting an older health timer', async () => {
    const h = makeHarness({ resetMode: true });
    await startRestored(h);
    h.verifyFlow('connection-current', 1);

    h.voice.setMicMode('muted');
    h.runEffects();
    h.voice.setMicMode('listening');
    h.runEffects();
    await h.advance(21000);
    assert.equal(h.voice.actionRequiredReason, null);

    assert.equal(h.api.resetEpoch('send'), true);
    await h.advance(1000);
    assert.equal(h.voice.actionRequiredReason, null, 'older 22s verifier was suspended');
    assert.equal(h.api._state.resetBoundaryPending, true);

    h.socket.deliver({
      type: 'buffer_state', text: '', epoch: 1,
      audio_ready_token: 'reset-ready-token',
    });
    assert.equal(h.api._state.resetBoundaryPending, false);
    assert.equal(h.voice.effectiveState, 'checking');
    await h.advance(22000);
    assert.equal(h.voice.effectiveState, 'enable_required', 'completion starts a fresh bounded verifier');
  });

  it('re-arms a bounded health check when listening resumes after mute', async () => {
    const h = makeHarness();
    h.socket.open();
    await h.flush();
    h.frame();
    const stream = h.stream;
    const context = h.contexts.at(-1);
    const worklet = h.worklets.at(-1);
    const socket = h.socket;

    h.voice.setMicMode('muted');
    h.runEffects();
    await h.advance(23000);
    assert.equal(h.voice.actionRequiredReason, null, 'intentional mute has no health deadline');

    h.voice.setMicMode('listening');
    h.runEffects();
    assert.equal(h.voice.effectiveState, 'checking');
    h.socket.deliver({
      type: 'voice_state', connection_id: 'connection-current',
      fsm_state: 'listening', upstream: 'ready', epoch: 0,
    });
    assert.equal(h.voice.connState, 'ok', 'healthy post-unmute verification is not a disconnect');
    await h.advance(22000);
    assert.equal(h.voice.effectiveState, 'enable_required', 'unacknowledged resume is bounded');
    assert.equal(h.stream, stream);
    assert.equal(h.contexts.at(-1), context);
    assert.equal(h.worklets.at(-1), worklet);
    assert.equal(h.socket, socket, 'verification timeout preserves the exact capture and current socket object');
    assert.equal(stream.track.readyState, 'live');
    assert.equal(context.state, 'running');
  });

  it('bounds a server-muted state that disagrees with listening intent', async () => {
    const h = makeHarness();
    await startRestored(h);
    h.verifyFlow('connection-current', 1);

    h.socket.deliver({
      type: 'voice_state', connection_id: 'connection-current',
      fsm_state: 'muted', upstream: 'ready', epoch: 0,
    });
    assert.equal(h.voice.effectiveState, 'checking');
    assert.equal(h.api._state.serverStateRepairAttempted, true);
    assert.ok(h.socket.sent.some((item) => typeof item === 'string' && JSON.parse(item).type === 'unmute'));
    h.socket.deliver({
      type: 'audio_flow', connection_id: 'connection-current',
      received: 2, forwarded: 2, ts_ms: 1001,
    });
    assert.equal(h.voice.effectiveState, 'checking', 'delayed pre-mute flow cannot override server state');
    await h.advance(22000);
    assert.equal(h.voice.effectiveState, 'enable_required');
  });

  it('does not let a transport incident mask ended capture', async () => {
    const h = makeHarness();
    await startRestored(h);
    h.verifyFlow('connection-current', 1);
    const originalStream = h.stream;

    await h.advance(4000);
    h.frame();
    h.api._audioWatchdogTick();
    assert.equal(h.voice.recoveryIncident.transportAttempts, 1);
    assert.equal(h.voice.recoveryIncident.captureAttempts, 0);
    const transportReplacement = h.socket;

    originalStream.track.fire('ended');
    await h.flush();
    assert.equal(h.voice.actionRequiredReason, 'mic_gesture');
    assert.equal(h.socket, transportReplacement, 'capture loss does not replace transport');
    assert.equal(h.streams.length, 1);
  });

  it('lets a gesture wake request supersede an older pending request safely', async () => {
    const wakeRequests = [];
    const h = makeHarness({ wakeRequests });
    h.socket.open();
    await h.flush();
    assert.equal(wakeRequests.length, 1, 'automatic wake request is pending');

    assert.equal(h.api.enableFromGesture(), true);
    assert.equal(wakeRequests.length, 2, 'gesture performs its own wake request synchronously');
    wakeRequests[1].resolve();
    await h.flush();
    assert.equal(h.voice.wakeStatus, 'held');

    wakeRequests[0].resolve();
    await h.flush();
    assert.equal(wakeRequests[0].sentinel.released, true, 'obsolete sentinel releases itself');
    assert.equal(h.voice.wakeStatus, 'held', 'obsolete completion cannot overwrite the current lock');
  });

  it('surfaces current-document microphone denial immediately', async () => {
    const denied = new Error('permission denied');
    denied.name = 'NotAllowedError';
    const h = makeHarness({ micError: denied });
    h.socket.open();
    await h.flush();
    assert.equal(h.voice.captureStatus, 'absent');
    assert.equal(h.voice.actionRequiredReason, 'mic_denied');
    assert.equal(h.voice.effectiveState, 'enable_required');
  });

  it('shares one transport attempt across watchdog and server recovery triggers', async () => {
    const h = makeHarness();
    await startRestored(h);
    h.verifyFlow();
    await h.advance(4000);
    h.frame();
    h.api._audioWatchdogTick();
    assert.equal(h.voice.recoveryIncident.transportAttempts, 1);
    const socketCount = h.sockets.length;
    h.api.onServerRecovered();
    h.api._audioWatchdogTick();
    await h.flush();
    assert.equal(h.sockets.length, socketCount, 'other triggers do not spend a second transport attempt');
    assert.equal(h.voice.recoveryIncident.transportAttempts, 1);
  });

  it('preserves a recoverable visible partial across flow-health socket replacement', async () => {
    const h = makeHarness();
    await startRestored(h);
    h.verifyFlow('connection-current', 1);

    // Partials are already visible to the operator, but the server deliberately
    // persists finals only. WhisperLive overtime can therefore strand the whole
    // visible draft on the client side before it becomes a server final.
    h.socket.deliver({
      type: 'transcript', kind: 'final', text: 'the finalized prefix', epoch: 0,
    });
    h.socket.deliver({
      type: 'transcript', kind: 'partial',
      text: 'recoverable words already visible to the operator', epoch: 0,
    });
    assert.equal(
      h.voice.bufferText,
      'the finalized prefix recoverable words already visible to the operator',
    );

    const oldSocket = h.socket;
    await h.advance(4000);
    h.frame();
    h.api._audioWatchdogTick();
    const replacement = h.socket;
    assert.notEqual(replacement, oldSocket, 'flow-health recovery replaces the browser socket');

    // The replacement WhisperLive session starts with no old partial. Its first
    // hypothesis must not overwrite text that is still recoverable in JS memory.
    replacement.open();
    await h.flush();
    replacement.deliver({
      type: 'buffer_state', text: 'the finalized prefix', epoch: 0,
    });
    assert.equal(
      h.voice.bufferText,
      'the finalized prefix recoverable words already visible to the operator',
      'the finals-only server snapshot cannot shorten or duplicate the browser draft',
    );
    replacement.deliver({
      type: 'transcript', kind: 'partial', text: 'words after recovery', epoch: 0,
    });
    assert.equal(
      h.voice.bufferText,
      'the finalized prefix recoverable words already visible to the operator words after recovery',
    );
  });

  it('preserves a visible partial across an ordinary unexpected socket close', async () => {
    const h = makeHarness();
    await startRestored(h);
    h.verifyFlow('connection-current', 1);
    h.socket.deliver({
      type: 'transcript', kind: 'partial', text: 'draft before network loss', epoch: 0,
    });

    h.socket.fire('close');
    await h.advance(2000);
    const replacement = h.socket;
    replacement.open();
    await h.flush();
    replacement.deliver({
      type: 'transcript', kind: 'partial', text: 'speech after network loss', epoch: 0,
    });

    assert.equal(
      h.voice.bufferText,
      'draft before network loss speech after network loss',
    );
  });

  it('retries an action-required transport without destroying capture or draft', async () => {
    const h = makeHarness();
    await startRestored(h);
    h.verifyFlow('connection-current', 1);
    h.socket.deliver({
      type: 'transcript', kind: 'partial', text: 'draft before exhausted recovery', epoch: 0,
    });
    const stream = h.stream;
    const context = h.contexts.at(-1);
    const worklet = h.worklets.at(-1);

    // Model the bounded transport controller's terminal state. Deadline behavior
    // is covered separately; this regression targets what the red retry does to
    // still-live capture and client-owned text.
    h.api._state.ws = null;
    h.api._state.wsOpen = false;
    h.api._state.talkActive = false;
    h.voice.setActionRequired('recovery_failed');
    assert.equal(h.voice.actionRequiredReason, 'recovery_failed');

    assert.equal(h.api.enableFromGesture(), true);
    assert.equal(h.stream, stream);
    assert.equal(h.contexts.at(-1), context);
    assert.equal(h.worklets.at(-1), worklet);
    assert.equal(h.voice.bufferText, 'draft before exhausted recovery');

    h.socket.open();
    await h.flush();
    h.socket.deliver({
      type: 'transcript', kind: 'partial', text: 'speech after retry', epoch: 0,
    });
    assert.equal(
      h.voice.bufferText,
      'draft before exhausted recovery speech after retry',
    );
  });

  it('starts every gesture-gated browser API before enableFromGesture returns', () => {
    const h = makeHarness({ resume: false, contextState: 'suspended' });
    h.voice.boundSessionId = 'auto-A';
    h.voice.micMode = 'listening';
    h.logs.length = 0;

    assert.equal(h.api.enableFromGesture(), true);
    assert.deepEqual(h.logs.slice(0, 4), [
      'wake-lock', 'audio-context', 'audio-resume', 'get-user-media',
    ]);
    assert.equal(h.voice.effectiveState, 'checking');
  });

  it('does not repair ordinary silence while forwarding acknowledgements advance', async () => {
    const h = makeHarness();
    await startRestored(h);
    h.verifyFlow('connection-current', 1);
    for (let index = 2; index <= 5; index += 1) {
      await h.advance(2000);
      h.frame();
      h.verifyFlow('connection-current', index);
    }
    assert.equal(h.voice.effectiveState, 'listening');
    assert.equal(h.voice.recoveryIncident, null);
    assert.equal(h.streams.length, 1);
    assert.equal(h.sockets.length, 1);
  });
});
