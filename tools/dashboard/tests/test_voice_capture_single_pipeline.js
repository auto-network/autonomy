// PROVEN bug (2026-06-08): the "gibberish dictation" failure mode.
//
// Forensics on a captured broken-state WAV (data/voice-captures/host-0530-...wav)
// showed the PCM the browser sent to WhisperLive was FOUR copies of the same mic,
// interleaved one 100ms frame at a time (every-4th-frame continuous; all 4
// de-interleaved sub-streams cross-correlate at 1.00). WhisperLive received
// A0 B0 C0 D0 A1 B1 C1 D1 ... and transcribed mush. Force-quitting the app fixed
// it — because that destroys the leaked pipelines.
//
// Cause: voice-capture.js's worklet `port.onmessage` posts to the SHARED s.ws and
// is never detached; teardown() only calls async ctx.close() and leaves the old
// worklet alive. The five teardown();startListening() paths (audio-stall watchdog,
// _onWake, three reconnect paths) race the async ensureMicReady and leak live
// pipelines that keep streaming the same mic to the CURRENT socket.
//
// INVARIANT under test: at most ONE worklet may post to the live socket. A worklet
// whose pipeline was torn down / superseded MUST go inert. These tests drive the
// REAL ensureMicReady (mocked getUserMedia / AudioContext / AudioWorkletNode), then
// fire frames from every worklet ever created and assert the live socket carries
// exactly one source. FAILS on current code (leaked worklets interleave); PASSES
// once each pipeline is generation-guarded so only the newest posts.

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
    constructor(url) { this.url = url; this.readyState = 0; this._l = {}; this.sent = []; sockets.push(this); }
    addEventListener(t, c) { (this._l[t] ||= []).push(c); }
    send(d) { this.sent.push(d); }
    close() { this.readyState = 3; this._fire('close', {}); }
    _fire(t, e) { for (const c of (this._l[t] || [])) c(e); }
    fireOpen() { this.readyState = 1; this._fire('open', {}); }
  }
  FakeWS.OPEN = 1;

  // Each pipeline gets a fresh worklet node; we keep every node ever created so the
  // test can fire frames from the leaked ones too.
  const workletNodes = [];
  class FakeWorkletNode {
    constructor(ctx) { this.ctx = ctx; this.port = { onmessage: null, postMessage() {}, close() {} }; this.alive = true; workletNodes.push(this); }
    connect() {}
    disconnect() { this.alive = false; }
  }
  class FakeAudioContext {
    constructor() { this.state = 'running'; this.sampleRate = 16000; this.closed = false; this.audioWorklet = { addModule() { return Promise.resolve(); } }; this.destination = {}; }
    createMediaStreamSource() { return { connect() {} }; }
    createGain() { return { gain: { value: 1 }, connect() {} }; }
    resume() { this.state = 'running'; return Promise.resolve(); }
    close() { this.closed = true; this.state = 'closed'; return Promise.resolve(); }
  }
  function makeStream() {
    const track = { readyState: 'live', stop() { this.readyState = 'ended'; }, addEventListener() {} };
    return { getTracks() { return [track]; } };
  }
  const mediaDevices = { getUserMedia() { return Promise.resolve(makeStream()); } };

  const effects = [];
  const stores = {};
  const alpine = { effect(fn) { effects.push(fn); fn(); }, store(n, o) { if (o !== undefined) { stores[n] = o; return o; } return stores[n]; } };
  const docListeners = {};
  const document = { visibilityState: 'visible', addEventListener(n, cb) { (docListeners[n] ||= []).push(cb); }, removeEventListener() {} };
  const voice = { boundSessionId: '', micMode: 'idle', bufferText: '', enabled: true, connState: 'ok', sheetError: '',
    setBufferText(t) { this.bufferText = t; }, setConnState(s2) { this.connState = s2; } };
  stores.voice = voice;
  stores.flags = { isLoaded: true, get() { return false; } };

  const sandbox = {
    console, setTimeout, clearTimeout, setInterval, clearInterval, Promise, JSON, Math, Date,
    Object, Array, String, ArrayBuffer, Uint8Array,
    WebSocket: FakeWS, AudioContext: FakeAudioContext, AudioWorkletNode: FakeWorkletNode,
    location: { protocol: 'https:', host: 'localhost:8080' },
    navigator: { mediaDevices },
    document,
    window: { console, Autonomy: {}, addEventListener() {}, removeEventListener() {} },
  };
  sandbox.window.document = document;
  sandbox.window.Alpine = alpine;
  sandbox.Alpine = alpine;
  sandbox.window.AudioContext = FakeAudioContext;
  sandbox.window.AudioWorkletNode = FakeWorkletNode;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(CAPTURE_JS, 'utf8'), sandbox, { filename: 'voice-capture.js' });
  for (const cb of (docListeners['alpine:init'] || [])) cb();
  const api = sandbox.window.Autonomy.voiceCapture;

  const runEffects = () => { for (const fn of effects) fn(); };
  const flush = async () => { for (let i = 0; i < 30; i += 1) await Promise.resolve(); };
  const makeFrame = (tag) => { const b = new ArrayBuffer(4); new Uint8Array(b)[0] = tag; return b; };
  const readTag = (buf) => new Uint8Array(buf)[0];
  // Faithful to a real AudioWorkletNode port: a frame the worklet posts only runs a
  // handler if one is attached; with onmessage===null the message is simply dropped.
  const fire = (node, tag) => { if (node.port.onmessage) node.port.onmessage({ data: { type: 'audio', buffer: makeFrame(tag) } }); };

  return { voice, sockets, workletNodes, state: api._state, api, runEffects, flush, readTag, fire };
}

// Bring up one capture pipeline through the REAL start path; resolves when live.
async function startPipeline(h) {
  h.voice.boundSessionId = 'auto-A';
  h.voice.micMode = 'listening';
  h.voice.connState = 'ok';
  h.runEffects();                                    // react -> startListening -> creates socket + ensureMicReady
  h.sockets[h.sockets.length - 1].fireOpen();        // open before waitOpen polls
  await h.flush();                                   // resolve getUserMedia/addModule + waitOpen -> started+talkActive
}

describe('voice capture: single-pipeline invariant (gibberish interleave bug)', () => {
  it('sanity: the current worklet posts to the live socket (harness drives a real pipeline)', async () => {
    const h = makeHarness();
    await startPipeline(h);
    const sock = h.sockets[h.sockets.length - 1];
    const node = h.workletNodes[h.workletNodes.length - 1];
    sock.sent.length = 0;
    h.fire(node, 7);
    assert.deepEqual(sock.sent.map(h.readTag), [7], 'the live worklet must reach the socket');
  });

  it('a superseded worklet does NOT post to the live socket', async () => {
    const h = makeHarness();
    await startPipeline(h);
    const oldNode = h.workletNodes[h.workletNodes.length - 1];
    h.api.teardown();                                // real teardown leaves the old worklet attached (the bug)
    await startPipeline(h);
    const liveSock = h.sockets[h.sockets.length - 1];
    const newNode = h.workletNodes[h.workletNodes.length - 1];
    liveSock.sent.length = 0;
    h.fire(oldNode, 1);                              // leaked pipeline still receiving mic audio
    h.fire(newNode, 2);                              // current pipeline
    assert.deepEqual(liveSock.sent.map(h.readTag), [2],
      'the live socket must carry ONLY the current worklet; a torn-down worklet must be inert');
  });

  it('after N restart cycles the live socket carries exactly ONE worklet (no interleave)', async () => {
    const h = makeHarness();
    await startPipeline(h);
    for (let i = 0; i < 3; i += 1) { h.api.teardown(); await startPipeline(h); }   // 4 pipelines, mirrors the captured WAV
    const liveSock = h.sockets[h.sockets.length - 1];
    liveSock.sent.length = 0;
    // every worklet ever created still hears the same mic and fires a frame
    h.workletNodes.forEach((node, idx) => h.fire(node, idx));
    const distinct = new Set(liveSock.sent.map(h.readTag));
    assert.equal(distinct.size, 1,
      `exactly one worklet may post; interleaving worklets present: ${JSON.stringify([...distinct])}`);
  });
});
