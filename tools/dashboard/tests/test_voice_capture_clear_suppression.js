// #39 / clearing-reappearance harness — deterministic interleave of clear/send
// with racing transcript frames against the REAL voice-capture module.
//
// The clearing bugs (text reappears after Clear; buffer won't clear; a frame that
// races the Clear defeats suppression) are timing-dependent and impossible to pin
// down on-device. This harness loads the real IIFE with a controllable mock socket
// and store, fires final/partial/buffer_state frames in any order relative to a
// Clear/Send, and asserts the resulting bufferText. Each scenario the operator hits
// becomes a one-line repro here instead of a guess.

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
      this.readyState = 0;
      this._listeners = {};
      this.sent = [];
      sockets.push(this);
    }
    addEventListener(type, cb) { (this._listeners[type] ||= []).push(cb); }
    send(data) { this.sent.push(data); }
    close() { this.readyState = 3; this._fire('close', {}); }
    _fire(type, ev) { for (const cb of (this._listeners[type] || [])) cb(ev); }
    fireOpen() { this.readyState = 1; this._fire('open', {}); }
    fireFinal(text) { this._fire('message', { data: JSON.stringify({ type: 'transcript', kind: 'final', text }) }); }
    firePartial(text) { this._fire('message', { data: JSON.stringify({ type: 'transcript', kind: 'partial', text }) }); }
    fireBufferState(text) { this._fire('message', { data: JSON.stringify({ type: 'buffer_state', text }) }); }
    /** control messages the client sent back to the server (e.g. 'discard') */
    controls() { return this.sent.filter((d) => typeof d === 'string').map((d) => { try { return JSON.parse(d).type; } catch (_e) { return null; } }).filter(Boolean); }
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
  const flush = () => new Promise((r) => setImmediate(r));

  // Operator clears (long-press keyboard) or sends — both empty the box. The
  // store fires its bufferText='' which the capture clear-effect observes.
  function clearBox() { voice.setBufferText(''); runEffects(); }

  return {
    voice, sockets, runEffects, flush, clearBox,
    state: sandbox.window.Autonomy.voiceCapture._state,
    async startListening() {
      voice.boundSessionId = 'auto-A';
      voice.micMode = 'listening';
      runEffects();
      await flush();
      sockets[0].fireOpen();
      return sockets[0];
    },
  };
}

describe('#39 clear / suppression interleave harness', () => {
  it('a Clear suppresses an exact re-emit of the just-removed text (buffer_state)', async () => {
    const h = makeHarness();
    const ws = await h.startListening();
    ws.fireFinal('hello world this is a test');
    assert.equal(h.voice.bufferText, 'hello world this is a test');

    h.clearBox();
    assert.equal(h.voice.bufferText, '', 'box empties on clear');
    assert.ok(ws.controls().includes('discard'), 'told the server to discard');

    // whisper re-transcribes its still-buffered audio and re-pushes the manager buffer
    ws.fireBufferState('hello world this is a test');
    assert.equal(h.voice.bufferText, '', 'the re-emit is suppressed, not reappearing');
  });

  it('a Clear suppresses a partial that incrementally rebuilds the removed text', async () => {
    const h = makeHarness();
    const ws = await h.startListening();
    ws.fireFinal('thank you very much');
    h.clearBox();
    ws.firePartial('thank');
    ws.firePartial('thank you');
    ws.firePartial('thank you very much');
    assert.equal(h.voice.bufferText, '', 'incremental re-emit stays suppressed');
  });

  it('NEW speech after a Clear shows normally (suppression does not over-reach)', async () => {
    const h = makeHarness();
    const ws = await h.startListening();
    ws.fireFinal('first message');
    h.clearBox();
    ws.fireBufferState('first message');           // re-emit, suppressed
    assert.equal(h.voice.bufferText, '');
    ws.fireFinal('a completely different thing');   // genuinely new
    assert.equal(h.voice.bufferText, 'a completely different thing');
  });

  it('a final that RACES the Clear (arrives right after) does not strand old text', async () => {
    const h = makeHarness();
    const ws = await h.startListening();
    ws.firePartial('order coffee');
    ws.fireFinal('order coffee please');
    assert.equal(h.voice.bufferText, 'order coffee please');
    h.clearBox();
    // the in-flight final for the same utterance lands just after the clear
    ws.fireFinal('order coffee please');
    assert.equal(h.voice.bufferText, '', 'the racing duplicate is suppressed');
  });

  // REAL TRACE (captured from voice_whisper_repro against live WhisperLive):
  // Whisper streams cumulative PARTIALS within a segment, then one FINAL, then the
  // next segment. If you clear mid-segment, the visible text is a PARTIAL and
  // s.finals is still '' — so remembering s.finals on clear remembers NOTHING, and
  // the segment's final (the whole sentence) re-emits and REAPPEARS. The buffer
  // must remember what was DISPLAYED (the partial), not just committed finals.
  it('REAL: clearing mid-partial must not let the segment final reappear', async () => {
    const h = makeHarness();
    const ws = await h.startListening();
    // pre-clear — only partials have arrived (no final yet for this utterance)
    ws.firePartial('And so, my fellow Americans');
    ws.firePartial('And so, my fellow Americans, ask not');
    ws.firePartial('And so, my fellow Americans, ask not what you');
    assert.ok(/^And so, my fellow Americans/.test(h.voice.bufferText), 'partial shown');

    h.clearBox();
    assert.equal(h.voice.bufferText, '', 'box empties on clear');

    // the same utterance keeps going and FINALIZES (whole sentence re-emitted),
    // then the next segment finalizes.
    ws.firePartial('And so, my fellow Americans, ask not what your country can do for you.');
    ws.fireFinal('And so, my fellow Americans, ask not what your country can do for you.');
    ws.fireFinal('Ask what you can do for your country.');

    assert.ok(!/and so, my fellow americans/i.test(h.voice.bufferText),
      'cleared text reappeared in the buffer: ' + JSON.stringify(h.voice.bufferText));
  });

  it('after Clear + re-emit, the badge can come back: new speech repopulates bufferText', async () => {
    const h = makeHarness();
    const ws = await h.startListening();
    ws.fireFinal('alpha beta');
    h.clearBox();
    ws.fireBufferState('alpha beta');      // suppressed
    assert.equal(h.voice.bufferText, '');  // badge gone (count 0)
    ws.fireFinal('gamma');                 // new word
    // bufferText non-empty again → the word-count badge MUST be able to re-show.
    assert.equal(h.voice.bufferText, 'gamma');
    assert.ok(h.voice.bufferText.trim().length > 0, 'badge source is non-empty after new speech');
  });
});
