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
    finals: '',
    removed: [],            // recently cleared/sent text, for re-emit suppression
    wakeLock: null,
  };

  // ── Look-back suppression of re-emitted cleared/sent text ───────────────
  // After Clear/Send, whisper_live keeps the just-spoken audio in its per-client
  // buffer and re-transcribes it, re-emitting the removed text in a fresh segment
  // that can slip past the server's timestamp cutoff — so the message reappears in
  // the box. The cutoff alone can't win that race (whisper_live has no runtime
  // buffer reset). So we remember what was just removed and refuse to re-render
  // anything that reproduces it. Operator-requested: "I don't like my message
  // reappearing after I click send." TTL-bounded (re-emission only happens for a
  // few seconds while the audio is still buffered upstream) so it can NEVER
  // suppress something the operator legitimately says later.
  var REMOVED_TTL_MS = 45000;
  function _nowMs() { return (typeof Date !== 'undefined' && Date.now) ? Date.now() : 0; }
  function _normWord(w) { return String(w).toLowerCase().replace(/[^a-z0-9]/g, ''); }
  function _wordList(text) {
    return String(text == null ? '' : text).trim().split(/\s+/).map(_normWord).filter(Boolean);
  }
  function _pruneRemoved() {
    var now = _nowMs();
    s.removed = s.removed.filter(function (e) { return now - e.ts < REMOVED_TTL_MS; });
  }
  function _rememberRemoved(text) {
    var words = _wordList(text);
    if (!words.length) return;
    s.removed.push({ words: words, ts: _nowMs() });
    if (s.removed.length > 20) s.removed = s.removed.slice(-20);  // bound memory
  }
  // whisper_live re-transcribes its still-buffered audio and re-emits the just-
  // removed text — often with small variance (a re-timed segment, a reworded
  // token, punctuation) that an exact substring check misses. So strip it by
  // WORD PREFIX, with prejudice: if `candidate` begins by reproducing a recently
  // removed chunk, drop that leading run of words and keep only what's genuinely
  // new. Returns the kept text (original spacing preserved), or '' for a pure
  // re-emit. Case/punctuation-insensitive. TTL-bounded so later speech is safe.
  function _stripRemoved(candidate) {
    var orig = String(candidate == null ? '' : candidate).trim().split(/\s+/).filter(Boolean);
    if (!orig.length) return '';
    var norm = orig.map(_normWord);
    _pruneRemoved();
    var strip = 0;
    for (var i = 0; i < s.removed.length; i++) {
      var rw = s.removed[i].words, k = 0;
      while (k < rw.length && k < norm.length && rw[k] === norm[k]) k++;
      // Count it as a re-emit prefix only on a meaningful run (so a 1-2 word
      // coincidence with new speech isn't stripped), OR when the whole candidate
      // is still inside the removed chunk (a partial re-emit rebuilding it).
      if (k > strip && (k >= Math.min(rw.length, 3) || k === norm.length)) strip = k;
    }
    if (strip >= orig.length) return '';
    return orig.slice(strip).join(' ');
  }

  // Keep the screen awake while capturing (iOS auto-lock cuts off dictation).
  function _acquireWakeLock() {
    try {
      if (navigator.wakeLock && typeof navigator.wakeLock.request === 'function' && !s.wakeLock) {
        navigator.wakeLock.request('screen').then(function (sentinel) {
          s.wakeLock = sentinel;
          if (sentinel && typeof sentinel.addEventListener === 'function') {
            sentinel.addEventListener('release', function () { s.wakeLock = null; });
          }
        }).catch(function () {});
      }
    } catch (_e) {}
  }
  function _releaseWakeLock() {
    try { if (s.wakeLock) { s.wakeLock.release(); s.wakeLock = null; } } catch (_e) {}
  }

  // On returning to the foreground (screen unlock / tab refocus): re-acquire the
  // wake lock, and — critically — detect whether the mic track DIED while we
  // were backgrounded. iOS *ends* the MediaStreamTrack on screen-lock; resuming
  // the AudioContext can't revive a dead track, so the UI still shows "listening"
  // but no audio flows (the operator had to switch sessions to recover). We check
  // the track once on wake and do a single clean restart if it's dead. Listen on
  // visibilitychange + focus + pageshow because iOS fires these inconsistently
  // across Safari tabs vs standalone PWA.
  function _onWake() {
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
    var st = store();
    var wantsListening = !!(st && st.boundSessionId && st.micMode === 'listening');
    if (!(s.talkActive || (s.bind && s.ws) || wantsListening)) return;
    _acquireWakeLock();
    var dead = false;
    try {
      if (s.stream) {
        s.stream.getTracks().forEach(function (t) { if (t.readyState === 'ended') dead = true; });
      } else if (s.micGranted) {
        dead = true; // mic was granted earlier but the stream is gone
      }
    } catch (_e) {}
    if (dead) {
      var bind = s.bind || (st && st.boundSessionId) || '';
      _diag('mic stream died while locked — restarting capture');
      teardown();
      if (bind && wantsListening) startListening(bind);
      return;
    }
    if (s.ctx && s.ctx.state === 'suspended' && typeof s.ctx.resume === 'function') {
      s.ctx.resume().then(function () { _diag('resumed after unlock'); }).catch(function () {});
    }
  }
  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', _onWake);
    window.addEventListener('focus', _onWake);
    window.addEventListener('pageshow', _onWake);
  }

  // Silent console-only trace. The earlier on-screen overlay was removed —
  // it covered the live caption. Re-enable a visible overlay only for local
  // debugging; never ship it on top of the caption.
  var _finalCount = 0;
  function _diag(msg) {
    try { if (window.console && console.debug) console.debug('VOICE ⟶ ' + msg); } catch (_e) {}
  }
  // NOTE: network tracing removed — POSTing per re-emit frame flooded the pipe
  // that also forwards audio and stalled it. _vlog is now console-only (no
  // network), so the call sites are harmless. Re-enable a THROTTLED POST only
  // for a deliberate, short debug session.
  function _vlog(msg) {
    try { if (window.console && console.debug) console.debug('VOICE-DIAG ⟶ ' + msg); } catch (_e) {}
  }

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

  function _renderBuffer(text) {
    var st = store();
    if (!st || typeof st.setBufferText !== 'function') {
      _diag('NO STORE — text="' + (text || '').slice(-40) + '"');
      return;
    }
    st.setBufferText(text);
  }

  function attachSocket(ws, bind) {
    s.ws = ws; s.bind = bind; ws.binaryType = 'arraybuffer';
    ws.addEventListener('open', function () {
      if (ws !== s.ws) return;
      s.wsOpen = true; s.started = false; s.requiresReconnect = false;
      _diag('socket OPEN → ' + bind);
    });
    ws.addEventListener('message', function (event) {
      if (ws !== s.ws) return;
      var frame;
      try { frame = JSON.parse(event.data); } catch (_e) { return; }
      var type = String(frame.type || '');
      if (type === 'transcript') {
        var t = String(frame.text || '').trim();
        if (frame.kind === 'final') {
          if (t) {
            var candF = s.finals ? (s.finals + ' ' + t) : t;
            var keptF = _stripRemoved(candF);
            if (s.removed.length) _vlog('FINAL t="' + t.slice(0, 70) + '" removedN=' + s.removed.length + ' kept="' + keptF.slice(0, 70) + '"');
            s.finals = keptF;
            _finalCount++;
            _renderBuffer(s.finals);
          }
        } else if (frame.kind === 'partial') {
          if (t) {
            var candP = s.finals ? (s.finals + ' ' + t) : t;
            var keptP = _stripRemoved(candP);
            if (s.removed.length) _vlog('PARTIAL t="' + t.slice(0, 70) + '" removedN=' + s.removed.length + ' kept="' + keptP.slice(0, 70) + '"');
            _renderBuffer(keptP);
          }
        }
        return;
      }
      if (type === 'buffer_state') {
        // Strip removed text here too: a reconnect restores the server's MANAGER
        // buffer, which re-emits can have re-polluted after a Clear/Send — this
        // path used to bypass suppression entirely.
        var bsRaw = String(frame.text || '');
        var bsKept = _stripRemoved(bsRaw);
        if (bsRaw) _vlog('BUFFER_STATE raw="' + bsRaw.slice(0, 70) + '" removedN=' + s.removed.length + ' kept="' + bsKept.slice(0, 70) + '"');
        s.finals = bsKept;
        _renderBuffer(s.finals);
        return;
      }
      if (type === 'error') {
        var code = String(frame.code || '');
        _diag('server ERROR: ' + code + ' ' + String(frame.message || ''));
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
        // If iOS ends the track (screen-lock), the queued 'ended' event fires
        // the instant we're foregrounded — recover immediately rather than
        // waiting on the visibility check.
        try {
          stream.getTracks().forEach(function (t) {
            t.addEventListener('ended', function () { _diag('mic track ended'); _onWake(); });
          });
        } catch (_e) {}
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
            _acquireWakeLock();
            _diag('mic ready + START sent — streaming (screen locked OPEN)…');
          } else {
            _diag('FAILED: socket never opened after 5s');
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
    _releaseWakeLock();
    try { if (s.ws) s.ws.close(1000, 'end'); } catch (_e) {}
    s.ws = null; s.wsOpen = false; s.started = false; s.bind = ''; s.starting = false; s.finals = '';
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
    // When the buffer is cleared externally (operator pressed Send), reset our
    // accumulator AND tell the server to drop the committed audio — otherwise
    // the old finals re-render into the box and the operator sends duplicates.
    Alpine.effect(function () {
      var st = store();
      if (!st) return;
      var buf = st.bufferText;
      if (buf === '' && s.finals) {
        // Remember what we just removed (Clear or Send both empty the box) so a
        // whisper_live re-emit of this exact text gets suppressed instead of
        // reappearing. Must happen BEFORE we drop s.finals.
        _vlog('CLEAR remembering="' + s.finals.slice(0, 80) + '"');
        _rememberRemoved(s.finals);
        s.finals = '';
        sendControl('discard');
      } else if (buf === '' && !s.finals) {
        _vlog('CLEAR but s.finals already EMPTY — nothing remembered (bug?)');
      }
    });
  });

  window.Autonomy = window.Autonomy || {};
  window.Autonomy.voiceCapture = { teardown: teardown, _state: s };
})();
