// Replay a captured WhisperLive frame trace through the REAL voice-capture.js
// suppression algorithm and emit the operator-visible render timeline.
//
// This is the CLIENT half of the send-suppression harness: the python side
// captures real frames from a real WhisperLive session (reset OFF vs ON); this
// side feeds those exact frames through the production client IIFE — same store,
// same clear-effect, same _stripRemoved — with a Send/Clear injected at the
// recorded boundary. Whatever bufferText the real client computes is what the
// operator would have seen. No synthetic frames: every frame came off the wire.
//
//   stdin  (JSON): { frames: [{kind:'final'|'partial'|'buffer_state', text}], clearAtIndex: N }
//   stdout (JSON): { renders: [<every bufferText set, in order>],
//                    controlsAfterClear: [<control types the client sent post-clear>],
//                    sentText: <bufferText at the moment of Send/Clear> }
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const CAPTURE_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/lib/voice-capture.js');

function build() {
  const sockets = [];
  class FakeWS {
    constructor(url) { this.url = url; this.readyState = 0; this._l = {}; this.sent = []; sockets.push(this); }
    addEventListener(t, c) { (this._l[t] ||= []).push(c); }
    send(d) { this.sent.push(d); }
    close() { this.readyState = 3; this._fire('close', {}); }
    _fire(t, e) { for (const c of (this._l[t] || [])) c(e); }
    fireOpen() { this.readyState = 1; this._fire('open', {}); }
    msg(obj) { this._fire('message', { data: JSON.stringify(obj) }); }
    controls() {
      return this.sent
        .filter((d) => typeof d === 'string')
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
  const docL = {};
  const document = {
    visibilityState: 'visible',
    addEventListener(n, c) { (docL[n] ||= []).push(c); },
    removeEventListener() {},
  };
  const renders = [];
  const voice = {
    boundSessionId: '', micMode: 'idle', bufferText: '', enabled: true, sheetError: '',
    setBufferText(t) { this.bufferText = t; renders.push(t); },
    setConnState() {},
  };
  stores.voice = voice;
  // Route the client's console.debug/log to STDERR — stdout must carry only the
  // result JSON the python side parses.
  const errConsole = {
    log: (...a) => process.stderr.write(a.join(' ') + '\n'),
    debug: (...a) => process.stderr.write(a.join(' ') + '\n'),
    warn: (...a) => process.stderr.write(a.join(' ') + '\n'),
    error: (...a) => process.stderr.write(a.join(' ') + '\n'),
  };
  const sandbox = {
    console: errConsole, setTimeout, clearTimeout, setInterval, clearInterval, Promise, JSON, Math, Date, Object, Array, String,
    WebSocket: FakeWS,
    location: { protocol: 'https:', host: 'localhost:8080' },
    navigator: {},
    document,
    window: { console: errConsole, Autonomy: {}, addEventListener() {}, removeEventListener() {} },
  };
  sandbox.window.document = document;
  sandbox.window.Alpine = alpine;
  sandbox.Alpine = alpine;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(CAPTURE_JS, 'utf8'), sandbox, { filename: 'voice-capture.js' });
  for (const cb of (docL['alpine:init'] || [])) cb();

  function runEffects() { for (const fn of effects) fn(); }
  return {
    voice, sockets, renders, runEffects,
    clearBox() { voice.setBufferText(''); runEffects(); },
    startListening() {
      voice.boundSessionId = 'auto-A';
      voice.micMode = 'listening';
      runEffects();
      sockets[0].fireOpen();
      return sockets[0];
    },
  };
}

function main() {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const frames = input.frames || [];
  const clearAtIndex = (typeof input.clearAtIndex === 'number') ? input.clearAtIndex : -1;

  const h = build();
  const ws = h.startListening();
  const preClearRenders = [];
  let sentText = '';
  let controlsBeforeClear = 0;

  for (let i = 0; i < frames.length; i++) {
    if (i === clearAtIndex) {
      // Operator presses Send/Clear at this exact point in the stream.
      sentText = h.voice.bufferText;
      controlsBeforeClear = ws.controls().length;
      preClearRenders.push(...h.renders.splice(0));
      h.clearBox();
    }
    const f = frames[i];
    const kind = f.kind;
    if (kind === 'final') ws.msg({ type: 'transcript', kind: 'final', text: f.text });
    else if (kind === 'partial') ws.msg({ type: 'transcript', kind: 'partial', text: f.text });
    else if (kind === 'buffer_state') ws.msg({ type: 'buffer_state', text: f.text });
  }
  // If the clear is at the very end (after all frames), still capture it.
  if (clearAtIndex >= frames.length) {
    sentText = h.voice.bufferText;
    controlsBeforeClear = ws.controls().length;
    preClearRenders.push(...h.renders.splice(0));
    h.clearBox();
  }

  const postClearRenders = h.renders.slice();
  const controlsAfterClear = ws.controls().slice(controlsBeforeClear);

  process.stdout.write(JSON.stringify({
    sentText,
    finalBuffer: h.voice.bufferText,
    preClearRenders,
    postClearRenders,
    controlsAfterClear,
  }));
}

main();
