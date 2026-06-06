// Audio-stall watchdog (#17): the mic/AudioContext can die silently with no
// visibilitychange, so the worklet stops producing frames while the UI still
// shows "listening" — dictation stalls until a manual reconnect. The watchdog
// detects "listening but no frame for AUDIO_STALL_MS" and restarts capture,
// surfaced via the SAME connState:'reconnecting' (spinny ring) as a backend
// reconnect. These tests drive the real watchdog tick through mocked WS/Alpine.

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
    constructor(url) { this.url = url; this.readyState = 0; this._l = {}; this.closed = false; sockets.push(this); }
    addEventListener(t, cb) { (this._l[t] ||= []).push(cb); }
    send() {}
    close() { this.closed = true; this.readyState = 3; }
  }
  FakeWS.OPEN = 1;
  const effects = [];
  const stores = {};
  const alpine = {
    effect(fn) { effects.push(fn); fn(); },
    store(n, o) { if (o !== undefined) { stores[n] = o; return o; } return stores[n]; },
  };
  const docListeners = {};
  const document = { visibilityState: 'visible', addEventListener(n, cb) { (docListeners[n] ||= []).push(cb); }, removeEventListener() {} };
  const voice = { boundSessionId: '', micMode: 'idle', bufferText: '', enabled: true, connState: 'ok',
    setBufferText(t) { this.bufferText = t; }, setConnState(s2) { this.connState = s2; } };
  stores.voice = voice;
  const sandbox = {
    console, setTimeout, clearTimeout, setInterval, clearInterval, Promise, JSON, Math, Date, Object, Array, String,
    WebSocket: FakeWS, location: { protocol: 'https:', host: 'localhost:8080' }, navigator: {}, document,
    window: { console, Autonomy: {}, addEventListener() {}, removeEventListener() {} },
  };
  sandbox.window.document = document; sandbox.window.Alpine = alpine; sandbox.Alpine = alpine; sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(CAPTURE_JS, 'utf8'), sandbox, { filename: 'voice-capture.js' });
  for (const cb of (docListeners['alpine:init'] || [])) cb();
  const api = sandbox.window.Autonomy.voiceCapture;
  return { voice, sockets, state: api._state, tick: api._audioWatchdogTick };
}

// Put the capture state into a live-listening posture (as if mic + ctx + ws up).
function makeListening(h, lastFrameAgoMs) {
  h.voice.boundSessionId = 'auto-A';
  h.voice.micMode = 'listening';
  h.voice.connState = 'ok';
  h.state.bind = 'auto-A';
  h.state.talkActive = true;
  h.state.ctx = { state: 'running' };     // truthy AudioContext
  h.state.ws = h.sockets[0] || { readyState: 1, close() {} };
  h.state.wsOpen = true;
  h.state.starting = false;
  h.state.stream = { getTracks: () => [{ readyState: 'live' }] };   // healthy mic track
  h.state.lastFrameAt = Date.now() - lastFrameAgoMs;
}

describe('#17 audio-stall watchdog', () => {
  it('restarts capture + shows reconnecting when no frame for >stall window', () => {
    const h = makeHarness();
    const before = h.sockets.length;
    makeListening(h, 9000);            // 9s since last frame → stalled
    h.tick();
    assert.equal(h.voice.connState, 'reconnecting', 'shows the spinny recon state');
    assert.ok(h.sockets.length > before, 'a fresh capture/socket was started');
  });

  it('restarts when the mic track has ENDED even if frames still flow (iOS mic off)', () => {
    const h = makeHarness();
    makeListening(h, 200);             // frames fresh — frame-presence alone would miss this
    h.state.stream = { getTracks: () => [{ readyState: 'ended' }] };
    const before = h.sockets.length;
    h.tick();
    assert.equal(h.voice.connState, 'reconnecting', 'recon ring on a dead track');
    assert.ok(h.sockets.length > before, 'restarted despite fresh frames');
  });

  it('does NOT fire while frames are still flowing', () => {
    const h = makeHarness();
    makeListening(h, 200);             // a frame 200ms ago → healthy
    const before = h.sockets.length;
    h.tick();
    assert.equal(h.voice.connState, 'ok');
    assert.equal(h.sockets.length, before, 'no restart while healthy');
  });

  it('does NOT fire when muted (not actively listening)', () => {
    const h = makeHarness();
    makeListening(h, 9000);
    h.voice.micMode = 'muted';         // muted → watchdog must stay out of it
    const before = h.sockets.length;
    h.tick();
    assert.equal(h.voice.connState, 'ok');
    assert.equal(h.sockets.length, before);
  });

  it('does NOT fire while already reconnecting (no restart storm)', () => {
    const h = makeHarness();
    makeListening(h, 9000);
    h.voice.connState = 'reconnecting';
    const before = h.sockets.length;
    h.tick();
    assert.equal(h.sockets.length, before, 'leaves an in-progress reconnect alone');
  });
});
