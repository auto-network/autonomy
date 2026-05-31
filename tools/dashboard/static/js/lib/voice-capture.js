/**
 * Voice capture controller — bridges Alpine.store('voice') to the mic + /ws/voice.
 *
 * The store (voice-store.js) owns all UI state + the send path, but has no
 * capture engine. The proven capture flow lived only in the /_admin/voice-smoke
 * canary page. This controller ports that flow into the in-app shell:
 *
 *   store.micMode === 'listening'  ->  open mic (16kHz PCM via AudioWorklet) +
 *                                      /ws/voice?bind=<session>, stream audio
 *   server 'transcript' (final)    ->  append into store.bufferText (caption/sheet
 *                                      render it; store.sendBuffer() sends it)
 *   store.micMode === 'muted'      ->  pause streaming (mute)
 *   store idle / unbound / ended   ->  tear down mic + socket
 *
 * Reuses the existing worklet (voice-smoke-worklet.js, processor "voice-smoke-pcm").
 */
(function () {
  var WORKLET_URL = '/static/js/lib/voice-smoke-worklet.js';

  var s = {
    ws: null, wsOpen: false, started: false, talkActive: false, requiresReconnect: false,
    bind: '',
    stream: null, ctx: null, sourceNode: null, workletNode: null, sinkNode: null, micGranted: false,
    starting: false,
  };

  function store() {
    try { return (typeof Alpine !== 'undefined' && Alpine.store) ? (Alpine.store('voice') || null) : null; }
    catch (_e) { return null; }
  }

  function wsUrl(bind) {
    var proto = (typeof location !== 'undefined' && location.protocol === 'https:') ? 'wss:' : 'ws:';
    return proto + '//' + location.host + '/ws/voice?bind=' + encodeURIComponent(bind);
  }

  function sendControl(type) {
    if (!s.ws || s.ws.readyState !== WebSocket.OPEN) return false;
    try { s.ws.send(JSON.stringify({ type: type })); return true; } catch (_e) { return false; }
  }

  function appendFinal(clean) {
    var st = store();
    if (!st || typeof st.setBufferText !== 'function') return;
    var prev = st.bufferText || '';
    st.setBufferText(prev ? (prev + ' ' + clean) : clean);
  }

  function attachSocket(ws, bind) {
    s.ws = ws; s.bind = bind; ws.binaryType = 'arraybuffer';
    ws.addEventListener('open', function () {
      if (ws !== s.ws) return;
      s.wsOpen = true; s.started = false; s.requiresReconnect = false;
    });
    ws.addEventListener('message', function (event) {
      if (ws !== s.ws) return;
      var frame;
      try { frame = JSON.parse(event.data); } catch (_e) { return; }
      var type = String(frame.type || '');
      if (type === 'transcript') {
        if (frame.kind === 'final') {
          var clean = String(frame.text || '').trim();
          if (clean) appendFinal(clean);
        }
        return;
      }
      if (type === 'buffer_state') {
        var st = store();
        if (st && typeof st.setBufferText === 'function') st.setBufferText(String(frame.text || ''));
        return;
      }
      if (type === 'error') {
        var code = String(frame.code || '');
        if (code === 'whisperlive_connect_failed' || code === 'whisperlive_session_error') {
          s.requiresReconnect = true;
          var st2 = store();
          if (st2) st2.sheetError = 'Transcription service unavailable. Reconnect and retry.';
        }
        return;
      }
    });
    ws.addEventListener('close', function () {
      if (ws !== s.ws) return;
      s.wsOpen = false; s.started = false; s.talkActive = false; s.ws = null;
    });
    ws.addEventListener('error', function () { /* surfaced by close */ });
  }

  function ensureMicReady() {
    if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== 'function') {
      return Promise.reject(new Error('microphone is unavailable in this browser'));
    }
    if (!window.AudioWorkletNode || !window.AudioContext) {
      return Promise.reject(new Error('AudioWorklet is unavailable in this browser'));
    }
    if (s.micGranted) {
      if (s.ctx && s.ctx.state === 'suspended') return s.ctx.resume();
      return Promise.resolve();
    }
    return navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, sampleRate: 16000, echoCancellation: true },
      video: false,
    }).then(function (stream) {
      var ctx = new AudioContext({ sampleRate: 16000, latencyHint: 'interactive' });
      return ctx.audioWorklet.addModule(WORKLET_URL).then(function () {
        var sourceNode = ctx.createMediaStreamSource(stream);
        var workletNode = new AudioWorkletNode(ctx, 'voice-smoke-pcm', {
          processorOptions: { targetSampleRate: 16000, frameSamples: 1600 },
        });
        var sinkNode = ctx.createGain();
        sinkNode.gain.value = 0;
        workletNode.port.onmessage = function (event) {
          var payload = event.data || {};
          if (payload.type !== 'audio' || !(payload.buffer instanceof ArrayBuffer)) return;
          if (!s.talkActive || !s.wsOpen || !s.ws || s.ws.readyState !== WebSocket.OPEN) return;
          if (!s.started || s.requiresReconnect) return;
          try { s.ws.send(payload.buffer); } catch (_e) {}
        };
        sourceNode.connect(workletNode);
        workletNode.connect(sinkNode);
        sinkNode.connect(ctx.destination);
        s.stream = stream; s.ctx = ctx; s.sourceNode = sourceNode;
        s.workletNode = workletNode; s.sinkNode = sinkNode; s.micGranted = true;
      });
    });
  }

  function startListening(bind) {
    if (s.starting) return;
    if (s.bind && s.bind !== bind) teardown();
    s.bind = bind;
    s.starting = true;
    if (!s.ws) attachSocket(new WebSocket(wsUrl(bind)), bind);
    ensureMicReady().then(function () {
      // wait briefly for the socket to open, then start the upstream
      var waited = 0;
      (function waitOpen() {
        if (s.wsOpen || waited >= 5000) {
          if (s.wsOpen) {
            if (!s.started) { if (sendControl('start')) s.started = true; }
            else { sendControl('unmute'); }
            s.talkActive = true;
          }
          s.starting = false;
          return;
        }
        waited += 100;
        setTimeout(waitOpen, 100);
      })();
    }).catch(function (e) {
      s.starting = false;
      var st = store();
      if (st) st.sheetError = 'Mic error: ' + (e && e.message ? e.message : 'could not start');
    });
  }

  function muteListening() {
    sendControl('mute');
    s.talkActive = false;
  }

  function resumeListening() {
    if (!s.started) { if (sendControl('start')) s.started = true; }
    else { sendControl('unmute'); }
    s.talkActive = true;
  }

  function teardown() {
    s.talkActive = false;
    try { if (s.ws) s.ws.close(1000, 'end'); } catch (_e) {}
    s.ws = null; s.wsOpen = false; s.started = false; s.bind = ''; s.starting = false;
    try { if (s.stream) s.stream.getTracks().forEach(function (t) { t.stop(); }); } catch (_e) {}
    try { if (s.ctx && typeof s.ctx.close === 'function') s.ctx.close(); } catch (_e) {}
    s.stream = null; s.ctx = null; s.sourceNode = null; s.workletNode = null; s.sinkNode = null; s.micGranted = false;
  }

  function react() {
    var st = store();
    if (!st) return;
    var bound = st.boundSessionId;
    var mode = st.micMode;
    if (bound && mode === 'listening') {
      if (s.bind !== bound || !s.ws) startListening(bound);
      else if (!s.talkActive && !s.starting) resumeListening();
    } else if (bound && mode === 'muted') {
      if (s.talkActive) muteListening();
    } else if (!bound || mode === 'idle') {
      if (s.ws || s.stream) teardown();
    }
  }

  document.addEventListener('alpine:init', function () {
    if (typeof Alpine === 'undefined' || typeof Alpine.effect !== 'function') return;
    Alpine.effect(react);
  });

  window.Autonomy = window.Autonomy || {};
  window.Autonomy.voiceCapture = { teardown: teardown, _state: s };
})();
