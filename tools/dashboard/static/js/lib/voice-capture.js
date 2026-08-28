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
  // The build this page was served from, published by the shell. A request that
  // names a build may be kept by the browser; one that does not is rechecked on
  // every load. Anything fetched after render has to add it itself.
  function staticVersion() {
    // Called at import time, and this module is imported by tests that run
    // outside a browser, where there is no document to read.
    if (typeof document === 'undefined' || typeof document.querySelector !== 'function') return '';
    var meta = document.querySelector('meta[name="autonomy-static-version"]');
    var v = meta && meta.getAttribute('content');
    return v ? '?v=' + encodeURIComponent(v) : '';
  }

  var WORKLET_URL = '/static/js/lib/voice-smoke-worklet.js' + staticVersion();

  var s = {
    ws: null, wsOpen: false, started: false, talkActive: false, requiresReconnect: false,
    bind: '',
    stream: null, ctx: null, primedContext: null, sourceNode: null, workletNode: null, sinkNode: null, micGranted: false,
    captureGen: 0,          // monotonic id of the LIVE capture pipeline. Only the
                            // worklet whose gen === captureGen may post audio; every
                            // superseded/torn-down worklet goes inert. Without this,
                            // teardown→restart cycles (watchdog/_onWake/reconnect)
                            // leak live worklets that all keep streaming the same mic
                            // into the current socket — N interleaved copies that
                            // shred the audio into gibberish (proven from a captured
                            // WAV: 4 same-mic streams interleaved frame-by-frame).
    starting: false,
    startGen: 0,            // invalidates asynchronous capture/socket startup work
    desiredBind: '',        // target for the current start generation
    finals: '',
    lastRendered: '',       // last text actually shown in the box (incl. in-flight
                            // partial) — what a Clear/Send must remember to suppress,
                            // since s.finals is empty when clearing mid-partial.
    carryPrefix: '',        // text carried over when the binding switches mid-buffer
                            // (#23 switch-takes-buffer): prepended to whatever the
                            // NEW session transcribes so the old text isn't clobbered.
    removed: [],            // recently cleared/sent text, for re-emit suppression (legacy/flag-off path)
    serverEpoch: 0,         // #43: highest transcript-acceptance epoch seen from the server
    acceptEpoch: 0,         // #43: drop transcript/buffer_state frames whose epoch < this
    wakeLock: null,
    wakeLockRequest: null,
    wakeGen: 0,             // stale acquire/release completions cannot change status
    wakeLockNeedsGesture: false,
    reconnectAttempt: 0,    // backoff index for the auto-reconnect loop
    reconnectTimer: null,
    stabilityTimer: null,   // resets the backoff once a fresh link survives a beat
    lastFrameAt: 0,         // last worklet audio frame — audio-stall watchdog baseline
    connectionId: '',       // server identity for the current /ws/voice connection
    serverFsm: '',          // last acknowledged canonical server voice state
    serverUpstream: '',     // last acknowledged WhisperLive readiness
    voiceStateAt: 0,        // receipt time of the current connection's state ack
    lastFlowAt: 0,          // last advancing, current-connection audio_flow ack
    lastLocalSendAt: 0,     // last PCM frame handed to the current WebSocket
    lastForwarded: 0,       // cumulative server-forwarded count on this connection
    flowRepairAttempted: false,
    trackMuted: false,
    trackMuteTimer: null,
    incident: null,
    incidentSeq: 0,
    incidentTimer: null,
    verificationTimer: null,
    resetBoundaryPending: false,
    resetBoundaryTimer: null,
    resetBoundaryGen: 0,
    resetBoundaryExpectedEpoch: 0,
    serverStateRepairAttempted: false,
    actionCooldownUntil: 0,
    trace: [],              // failure-trace ring (debug only)
    traceOn: false,
    traceExpiresAt: 0,
    traceDumpTimer: null,
  };

  var RECOVERY_VERIFY_MS = 8000;
  var RECOVERY_COOLDOWN_MS = 30000;
  var RESTORE_VERIFY_MS = 22000;
  var RECONNECT_VERIFY_MS = 90000;
  var RESET_BOUNDARY_VERIFY_MS = 22000;

  function _storeSet(method, field, value) {
    var st = store();
    if (!st) return;
    if (typeof st[method] === 'function') st[method](value);
    else st[field] = value;
  }
  function _setCapture(state) { _storeSet('setCaptureStatus', 'captureStatus', state); }
  function _setTransport(state) { _storeSet('setTransportStatus', 'transportStatus', state); }
  function _setAction(reason) { _storeSet('setActionRequired', 'actionRequiredReason', reason || null); }
  function _setWake(state) { _storeSet('setWakeStatus', 'wakeStatus', state); }
  function _publishIncident() {
    var value = s.incident ? {
      id: s.incident.id,
      active: !!s.incident.active,
      reason: s.incident.reason,
      captureAttempts: s.incident.captureAttempts,
      transportAttempts: s.incident.transportAttempts,
      startedAt: s.incident.startedAt,
    } : null;
    _storeSet('setRecoveryIncident', 'recoveryIncident', value);
  }

  function _clearIncidentTimer() {
    if (s.incidentTimer) clearTimeout(s.incidentTimer);
    s.incidentTimer = null;
  }

  function _clearVerificationTimer() {
    if (s.verificationTimer) clearTimeout(s.verificationTimer);
    s.verificationTimer = null;
  }

  function _armIncidentDeadline(incident, deadlineAt) {
    if (!incident || !incident.active) return;
    incident.deadlineAt = Math.max(incident.deadlineAt || 0, deadlineAt);
    _clearIncidentTimer();
    var incidentId = incident.id;
    s.incidentTimer = setTimeout(function () {
      if (s.incident && s.incident.active && s.incident.id === incidentId) {
        _requireAction('recovery_failed');
      }
    }, Math.max(0, incident.deadlineAt - _nowMs()));
    if (s.incidentTimer && typeof s.incidentTimer.unref === 'function') s.incidentTimer.unref();
  }

  function _armHealthVerification(force) {
    if (force && s.verificationTimer) _clearVerificationTimer();
    if (s.verificationTimer || (!force && s.lastFlowAt)) return;
    s.verificationTimer = setTimeout(function () {
      s.verificationTimer = null;
      var st = store();
      if (st && st.micMode === 'listening' &&
          (!s.lastFlowAt || st.transportStatus !== 'flowing') &&
          !(s.incident && s.incident.active)) {
        _requireAction('recovery_failed');
      }
    }, RESTORE_VERIFY_MS);
    if (s.verificationTimer && typeof s.verificationTimer.unref === 'function') s.verificationTimer.unref();
  }

  function _joinIncident(reason, verifyMs) {
    var now = _nowMs();
    var st = store();
    if (st && (st.actionRequiredReason === 'mic_denied' ||
               st.actionRequiredReason === 'mic_gesture')) return null;
    if (st && st.actionRequiredReason && now < s.actionCooldownUntil) return null;
    if (s.incident && s.incident.active) return s.incident;
    s.incident = {
      id: ++s.incidentSeq,
      active: true,
      reason: reason || 'unknown',
      captureAttempts: 0,
      transportAttempts: 0,
      startedAt: now,
      deadlineAt: now + (verifyMs || RECOVERY_VERIFY_MS),
    };
    _setAction(null);
    _setConn('reconnecting');
    _publishIncident();
    _armIncidentDeadline(s.incident, s.incident.deadlineAt);
    return s.incident;
  }

  function _requireAction(reason) {
    _clearIncidentTimer();
    _clearVerificationTimer();
    if (s.reconnectTimer) { clearTimeout(s.reconnectTimer); s.reconnectTimer = null; }
    if (s.stabilityTimer) { clearTimeout(s.stabilityTimer); s.stabilityTimer = null; }
    if (s.resetBoundaryTimer) { clearTimeout(s.resetBoundaryTimer); s.resetBoundaryTimer = null; }
    s.resetBoundaryPending = false;
    if (s.incident) s.incident.active = false;
    _publishIncident();
    s.actionCooldownUntil = _nowMs() + RECOVERY_COOLDOWN_MS;
    s.requiresReconnect = true;
    s.talkActive = false;
    s.startGen++;
    s.starting = false;
    var failedSocket = s.ws;
    s.ws = null;
    s.wsOpen = false;
    s.started = false;
    s.connectionId = '';
    try { if (failedSocket) failedSocket.close(1000, 'action-required'); } catch (_e) {}
    _disposeCapturePipeline();
    _releaseWakeLock();
    _setAction(reason || 'recovery_failed');
    _setTransport('disconnected');
    _setConn('disconnected');
    var st = store();
    if (st && !st.sheetError) st.sheetError = 'Microphone needs to be enabled again.';
  }

  function _verifyHealthyFlow() {
    var captureHealthy = !!(
      _currentTrackIsLive() && s.ctx && s.ctx.state === 'running' &&
      s.lastFrameAt && s.lastLocalSendAt &&
      (!s.incident || s.lastLocalSendAt >= s.incident.startedAt)
    );
    var transportHealthy = s.serverFsm === 'listening' && s.serverUpstream === 'ready';
    _setTransport(transportHealthy ? 'flowing' : 'connecting');
    _setCapture(captureHealthy ? 'live' : 'interrupted');
    var healthStore = store();
    if (!captureHealthy || !transportHealthy || (healthStore && healthStore.actionRequiredReason)) {
      _setConn(healthStore && healthStore.actionRequiredReason ? 'disconnected' : 'reconnecting');
      return;
    }
    _clearIncidentTimer();
    _clearVerificationTimer();
    s.incident = null;
    s.actionCooldownUntil = 0;
    s.serverStateRepairAttempted = false;
    _publishIncident();
    _setAction(null);
    _setConn('ok');
  }

  function _resetRecoveryForGesture() {
    _clearIncidentTimer();
    _clearVerificationTimer();
    s.incident = null;
    s.actionCooldownUntil = 0;
    _publishIncident();
    _setAction(null);
  }

  // ── Failure-trace recorder (clearing reliability) ───────────────────────
  // Capture REAL frames so a flaky clear can be replayed deterministically in the
  // mock harness instead of guessed. OFF by default — enable with ?vtrace=1 (the
  // localStorage activation expires after one hour). Records inbound WS frames,
  // every render, and buffer-empty
  // (clear/send) events into a bounded in-memory ring. On a clear it auto-POSTs the
  // window ONCE a few seconds later (to capture the post-clear re-emits) — never
  // per-frame (a per-frame POST once flooded the loop and stalled the audio path).
  var TRACE_MAX = 1200;
  var TRACE_TTL_MS = 60 * 60 * 1000;
  var TRACE_STORAGE_KEY = 'voice-trace';
  function _traceDisable() {
    try { localStorage.removeItem(TRACE_STORAGE_KEY); } catch (_e) {}
    s.traceOn = false;
    s.traceExpiresAt = 0;
    s.trace = [];
  }
  function _traceEnable() {
    var expiresAt = _nowMs() + TRACE_TTL_MS;
    try { localStorage.setItem(TRACE_STORAGE_KEY, String(expiresAt)); } catch (_e) {}
    s.traceExpiresAt = expiresAt;
    s.traceOn = true;
  }
  function _traceActive() {
    if (!s.traceOn) return false;
    if (!Number.isSafeInteger(s.traceExpiresAt) || _nowMs() >= s.traceExpiresAt) {
      _traceDisable();
      return false;
    }
    return true;
  }
  function _traceRec(kind, payload) {
    if (!_traceActive()) return;
    var e = { t: _nowMs(), kind: kind };
    if (payload !== undefined) e.v = payload;
    s.trace.push(e);
    if (s.trace.length > TRACE_MAX) s.trace.shift();
  }
  function _traceDump(reason) {
    if (!_traceActive() || !s.trace.length) return;
    var frames = s.trace.slice();
    try {
      return fetch('/api/voice/trace', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          reason: reason || 'manual',
          bind: s.bind || '',
          ua: (typeof navigator !== 'undefined' && navigator.userAgent) || '',
          frames: frames,
        }),
        keepalive: true,
      }).then(function (response) {
        if (!response.ok) return false;
        // Drop only the prefix that this successful request persisted. Events
        // appended while the POST was in flight remain available to the next
        // dump, and a failed request keeps the complete ring for retry.
        var last = frames[frames.length - 1];
        var index = s.trace.indexOf(last);
        if (index >= 0) s.trace.splice(0, index + 1);
        return true;
      }).catch(function () { return false; });
    } catch (_e) { return Promise.resolve(false); }
  }
  function _traceScheduleDump() {
    if (!_traceActive() || s.traceDumpTimer) return;   // one pending dump at a time
    s.traceDumpTimer = setTimeout(function () {
      s.traceDumpTimer = null;
      _traceDump('clear+window');
    }, 5000);
  }
  function _traceInit() {
    try {
      if (typeof location !== 'undefined' && /[?&]vtrace=1\b/.test(location.search) &&
          typeof localStorage !== 'undefined') {
        _traceEnable();
        return;
      }
      var expiresAt = Number(localStorage.getItem(TRACE_STORAGE_KEY));
      s.traceExpiresAt = Number.isSafeInteger(expiresAt) ? expiresAt : 0;
      s.traceOn = s.traceExpiresAt > _nowMs();
      if (!s.traceOn) _traceDisable();
    } catch (_e) { _traceDisable(); }
  }

  // ── Auto-reconnect with backoff ─────────────────────────────────────────
  // An unexpected socket drop used to need a manual session-switch (react()
  // only re-runs on micMode/boundSessionId changes, not on a close). Now we
  // retry with exponential backoff + jitter, cap the attempts, and surface the
  // state on the mic (red while reconnecting, solid red + tap-to-retry once we
  // give up) so it never looks alive when it's dead.
  var RECONNECT_BACKOFF = [1000, 2000, 4000, 8000, 16000, 30000];
  function _setConn(state) {
    var st = store();
    if (st && typeof st.setConnState === 'function') st.setConnState(state);
    else if (st) st.connState = state;
  }
  function _wantsConnection() {
    var st = store();
    return !!(st && st.boundSessionId && (st.micMode === 'listening' || st.micMode === 'vad_paused'));
  }
  function _scheduleReconnect() {
    var incident = _joinIncident('socket_closed', RECONNECT_VERIFY_MS);
    if (!incident) return;
    if (!incident.reconnectDeadlineAt) {
      incident.reconnectDeadlineAt = _nowMs() + RECONNECT_VERIFY_MS;
      _armIncidentDeadline(incident, incident.reconnectDeadlineAt);
    }
    if (incident.transportAttempts === 0) {
      incident.transportAttempts = 1;
      _publishIncident();
    }
    _setTransport('connecting');
    _setConn('reconnecting');
    if (s.reconnectTimer) return;
    if (s.reconnectAttempt >= RECONNECT_BACKOFF.length) {
      _requireAction('recovery_failed');   // give up → manual retry only (no loop)
      _diag('reconnect: gave up after ' + RECONNECT_BACKOFF.length + ' attempts');
      return;
    }
    var base = RECONNECT_BACKOFF[s.reconnectAttempt];
    var jitter = base * 0.2 * (Math.random() * 2 - 1);   // ±20% so we don't hammer in lockstep
    var delay = Math.max(300, base + jitter);
    s.reconnectAttempt++;
    _diag('reconnect: attempt ' + s.reconnectAttempt + ' of ' + RECONNECT_BACKOFF.length + ' in ' + Math.round(delay) + 'ms');
    s.reconnectTimer = setTimeout(function () {
      s.reconnectTimer = null;
      if (!_wantsConnection()) { _setConn('ok'); return; }   // muted/unbound meanwhile
      var st = store();
      s.starting = false; s.started = false;
      startListening(st.boundSessionId);
    }, delay);
  }
  // Manual retry from the red mic — reset the backoff and try immediately.
  function retryReconnectNow() {
    var st = store();
    if (!st || !st.boundSessionId) return false;
    _resetRecoveryForGesture();
    if (s.reconnectTimer) { clearTimeout(s.reconnectTimer); s.reconnectTimer = null; }
    s.reconnectAttempt = 0;
    _joinIncident('manual_retry', RESTORE_VERIFY_MS);
    _setTransport('connecting');
    _setConn('reconnecting');
    s.starting = false; s.started = false;
    startListening(st.boundSessionId);
    return true;
  }
  // The global EventBus saw the server come back (epoch change after a uvicorn
  // hot-reload / restart). That same restart dropped the voice WS, so re-establish
  // audio NOW instead of waiting out the backoff — the server's per-session manager
  // buffer is restored on reconnect, so the dictated text survives the blip. No-op
  // when muted/unbound or if the socket somehow survived.
  function onServerRecovered() {
    if (!_wantsConnection()) return;          // muted/unbound → nothing to re-establish
    if (s.ws && s.wsOpen) return;             // connection survived → leave it alone
    if (s.reconnectTimer) {
      clearTimeout(s.reconnectTimer);
      s.reconnectTimer = null;
      s.reconnectAttempt = 0;
      s.starting = false;
      s.started = false;
      var pendingStore = store();
      if (pendingStore && pendingStore.boundSessionId) {
        startListening(pendingStore.boundSessionId);
      }
      return;
    }
    s.reconnectAttempt = 0;
    _requestTransportRepair('server_recovered');
  }

  // ── Audio-capture liveness watchdog ─────────────────────────────────────
  // The mic stream / AudioContext can die SILENTLY (iOS audio interruption,
  // route change) with NO visibilitychange/focus/pageshow — so _onWake (which is
  // event-gated) never fires and the worklet simply stops producing frames: the
  // UI still shows "listening" but no audio flows and no transcripts return
  // ("stalled listening", recoverable only by a manual disconnect/reconnect —
  // confirmed in the server log: audio frames stop while state stays listening).
  // This time-based watchdog catches it and restarts capture, surfaced via the
  // SAME `reconnecting` state used for a backend reconnect.
  var AUDIO_STALL_MS = 4000;
  var AUDIO_FLOW_STALL_MS = 3000;

  function _disposeCapturePipeline() {
    s.captureGen++;
    if (s.trackMuteTimer) clearTimeout(s.trackMuteTimer);
    s.trackMuteTimer = null;
    s.trackMuted = false;
    try { if (s.workletNode) { s.workletNode.port.onmessage = null; s.workletNode.disconnect(); } } catch (_e) {}
    try { if (s.sourceNode) s.sourceNode.disconnect(); } catch (_e) {}
    try { if (s.stream) s.stream.getTracks().forEach(function (t) { t.stop(); }); } catch (_e) {}
    try { if (s.ctx && typeof s.ctx.close === 'function') s.ctx.close(); } catch (_e) {}
    try { if (s.primedContext && typeof s.primedContext.close === 'function') s.primedContext.close(); } catch (_e) {}
    s.stream = null;
    s.ctx = null;
    s.primedContext = null;
    s.sourceNode = null;
    s.workletNode = null;
    s.sinkNode = null;
    s.micGranted = false;
    s.lastFrameAt = 0;
    s.lastLocalSendAt = 0;
    _setCapture('absent');
  }

  function _requestCaptureRepair(reason) {
    var st = store();
    var bind = s.bind || (st && st.boundSessionId) || '';
    if (!bind) return false;
    var incident = _joinIncident(reason);
    // A cumulative audio_flow acknowledgement on the same socket cannot prove
    // that PCM came from the rebuilt browser capture rather than the old one.
    // Spend the incident's single capture AND transport repair together so the
    // new capture is verified only on a fresh, independently identified socket.
    if (!incident || incident.captureAttempts > 0) return false;
    incident.captureAttempts += 1;
    if (incident.transportAttempts === 0) incident.transportAttempts = 1;
    _publishIncident();
    _setCapture('acquiring');
    _disposeCapturePipeline();
    _setCapture('acquiring');
    _replaceVoiceSocket(reason);
    return true;
  }

  function _requestTransportRepair(reason) {
    var incident = _joinIncident(reason, RESTORE_VERIFY_MS);
    if (!incident || incident.transportAttempts > 0) return false;
    incident.transportAttempts += 1;
    s.flowRepairAttempted = true;
    _publishIncident();
    _setTransport('connecting');
    _replaceVoiceSocket(reason);
    return true;
  }

  function _replaceVoiceSocket(reason) {
    var st = store();
    var bind = s.bind || (st && st.boundSessionId) || '';
    if (!bind) return;
    _diag(reason + ' — replacing voice socket');
    s.startGen++;
    s.starting = false;
    s.desiredBind = bind;
    var old = s.ws;
    s.ws = null;
    s.wsOpen = false;
    s.started = false;
    s.requiresReconnect = false;
    s.connectionId = '';
    s.serverFsm = '';
    s.serverUpstream = '';
    s.voiceStateAt = 0;
    s.lastFlowAt = 0;
    s.lastForwarded = 0;
    try { if (old) old.close(1000, 'health-repair'); } catch (_e) {}
    _setTransport('connecting');
    _setConn('reconnecting');
    startListening(bind);
  }

  function _audioWatchdogTick() {
    var st = store();
    if (!st || st.micMode !== 'listening') return;       // only while actively capturing
    if (!s.talkActive || !s.ctx || s.starting) return;   // not streaming / mid-(re)start
    if (st.actionRequiredReason || (s.incident && s.incident.active)) return;
    // Two silent-death modes: (a) the worklet stops posting frames (AudioContext
    // suspended/died), and (b) the mic TRACK ends but the context keeps posting
    // silent buffers — iOS turned the mic OFF (no notch indicator) while the app
    // still thinks it's listening. Frame-presence alone misses (b), so also check
    // the track readyState.
    var trackDead = false;
    var trackMuted = s.trackMuted;
    try {
      if (s.stream && typeof s.stream.getTracks === 'function') {
        var tks = s.stream.getTracks();
        trackDead = (tks.length === 0) || tks.some(function (t) { return t.readyState === 'ended'; });
        trackMuted = trackMuted || tks.some(function (t) { return t.muted === true; });
      }
    } catch (_e) {}
    var contextInterrupted = !!(s.ctx && s.ctx.state && s.ctx.state !== 'running');
    if ((trackMuted || contextInterrupted) && !s.trackMuteTimer) {
      _setCapture('interrupted');
      _requestCaptureRepair(trackMuted ? 'track_muted' : 'audio_context_suspended');
      return;
    }
    var noFrames = !!s.lastFrameAt && (_nowMs() - s.lastFrameAt) > AUDIO_STALL_MS;
    if (!trackDead && !noFrames) {
      // A live worklet and open WebSocket do not prove the server received the
      // PCM. Once this connection has demonstrated flow, require cumulative
      // acknowledgements to keep advancing while local sends continue.
      var now = _nowMs();
      var localStillSending = s.lastLocalSendAt && now - s.lastLocalSendAt <= AUDIO_STALL_MS;
      var flowBaseline = s.lastFlowAt || s.voiceStateAt;
      var flowStalled = flowBaseline && now - flowBaseline > AUDIO_FLOW_STALL_MS;
      if (!s.resetBoundaryPending && localStillSending && flowStalled && s.serverFsm === 'listening') {
        _setTransport('stalled');
        _requestTransportRepair('server audio flow stalled');
      }
      return;
    }
    _diag('audio stall (' + (trackDead ? 'mic track ended' : 'no frames') + ') — restarting capture');
    _setCapture('interrupted');
    _requestCaptureRepair(trackDead ? 'track_ended' : 'frames_stalled');
  }

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
  function _acquireWakeLock(forceFresh) {
    try {
      if (!navigator.wakeLock || typeof navigator.wakeLock.request !== 'function') {
        _setWake('unsupported');
        return Promise.resolve(false);
      }
      if (s.wakeLock) { _setWake('held'); return Promise.resolve(true); }
      if (!forceFresh && s.wakeLockRequest) return s.wakeLockRequest;
      var wakeGen = ++s.wakeGen;
      _setWake('acquiring');
      var request = navigator.wakeLock.request('screen').then(function (sentinel) {
          if (wakeGen !== s.wakeGen || !_wantsConnection()) {
            try { sentinel.release(); } catch (_e) {}
            return false;
          }
          var previous = s.wakeLock;
          s.wakeLock = sentinel;
          s.wakeLockNeedsGesture = false;
          _setWake('held');
          if (sentinel && typeof sentinel.addEventListener === 'function') {
            sentinel.addEventListener('release', function () {
              if (wakeGen !== s.wakeGen || s.wakeLock !== sentinel) return;
              s.wakeLock = null;
              _setWake('released');
              if (_wantsConnection() &&
                  (typeof document === 'undefined' || document.visibilityState === 'visible')) {
                _acquireWakeLock();
              }
            });
          }
          if (previous && previous !== sentinel) {
            try { previous.release(); } catch (_e) {}
          }
          return true;
        }).catch(function () {
          if (wakeGen !== s.wakeGen) return false;
          s.wakeLockNeedsGesture = true;
          _setWake('denied');
          var st = store();
          if (st) st.sheetError = 'Screen may lock. Tap the microphone to allow keep-awake.';
          return false;
        }).finally(function () {
          if (s.wakeLockRequest === request) s.wakeLockRequest = null;
        });
      s.wakeLockRequest = request;
      return request;
    } catch (_e) { _setWake('denied'); return Promise.resolve(false); }
  }
  function _releaseWakeLock() {
    var releaseGen = ++s.wakeGen;
    s.wakeLockRequest = null;
    try {
      if (!s.wakeLock) { _setWake('released'); return; }
      var sentinel = s.wakeLock;
      s.wakeLock = null;
      Promise.resolve(sentinel.release()).finally(function () {
        if (releaseGen === s.wakeGen && !s.wakeLock) _setWake('released');
      });
    } catch (_e) { if (releaseGen === s.wakeGen) _setWake('released'); }
  }

  // Called directly from pointer/keyboard handlers. WebKit can require transient
  // user activation for the document's first wake-lock authorization, so this
  // cannot be deferred to the later Alpine effect that observes micMode.
  function activateFromGesture() {
    // Always issue a request in this gesture. An older asynchronous request may
    // have been made after visibility recovery and cannot borrow this activation.
    _acquireWakeLock(true);
    try {
      if (s.ctx && s.ctx.state === 'suspended' && typeof s.ctx.resume === 'function') {
        s.ctx.resume().catch(function () {});
      } else if ((!s.ctx || s.ctx.state === 'closed') && !s.primedContext && window.AudioContext) {
        // Construct the context inside the pointer call stack. WebKit may allow
        // getUserMedia later while still refusing an asynchronously-created
        // AudioContext; ensureMicReady adopts this primed context.
        s.primedContext = new AudioContext({ sampleRate: 16000, latencyHint: 'interactive' });
        if (s.primedContext.state === 'suspended' && typeof s.primedContext.resume === 'function') {
          s.primedContext.resume().catch(function () {});
        }
      }
    } catch (_e) {}
    return true;
  }

  // The large red recovery action uses one direct gesture to discard stale
  // browser/server state, request current-document permissions, and start a
  // fresh pipeline. getUserMedia is invoked synchronously inside startListening.
  function enableFromGesture() {
    var st = store();
    var bind = (st && st.boundSessionId) || s.bind || '';
    if (!bind) return false;
    if (s.bind && s.bind !== bind && st) {
      var carried = String(st.bufferText || '').trim();
      if (carried) s.carryPrefix = carried;
    }
    _resetRecoveryForGesture();
    teardown();
    s.reconnectAttempt = 0;
    s.flowRepairAttempted = false;
    s.requiresReconnect = false;
    if (st && typeof st.setMicMode === 'function') st.setMicMode('listening');
    _setCapture('acquiring');
    _setTransport('connecting');
    activateFromGesture();
    _setConn('reconnecting');
    startListening(bind);
    return true;
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
    var muted = s.trackMuted;
    try {
      if (s.stream) {
        s.stream.getTracks().forEach(function (t) {
          if (t.readyState === 'ended') dead = true;
          if (t.muted === true) muted = true;
        });
      } else if (s.micGranted) {
        dead = true; // mic was granted earlier but the stream is gone
      }
    } catch (_e) {}
    if (!dead && muted) {
      s.trackMuted = true;
      _setCapture('interrupted');
      if (s.trackMuteTimer) clearTimeout(s.trackMuteTimer);
      var wakeGen = s.captureGen;
      s.trackMuteTimer = setTimeout(function () {
        s.trackMuteTimer = null;
        if (wakeGen === s.captureGen && s.trackMuted) {
          _requestCaptureRepair('track_muted');
        }
      }, 1000);
      return;
    }
    if (dead) {
      _diag('mic stream died while locked — restarting capture');
      _setCapture('interrupted');
      if (wantsListening) _requestCaptureRepair('track_ended');
      return;
    }
    if (s.ctx && s.ctx.state === 'suspended' && typeof s.ctx.resume === 'function') {
      s.ctx.resume().then(function () {
        _diag('resumed after unlock');
      }).catch(function () {
        _setCapture('interrupted');
        _requestCaptureRepair('audio_context_suspended');
      });
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

  // #43: reset-suppression mode. When the `voice.reset_suppression` flag is on,
  // re-emit suppression is handled by a server-side WhisperLive reset + per-frame
  // epoch acceptance (this module), and the explicit resetEpoch() hook replaces the
  // legacy bufferText==='' -> discard inference + the _stripRemoved heuristic.
  // Flag absent/off (the default) keeps the legacy path byte-for-byte.
  function _resetMode() {
    try {
      var f = (typeof Alpine !== 'undefined' && Alpine.store) ? Alpine.store('flags') : null;
      return !!(f && typeof f.get === 'function' && f.get('voice.reset_suppression') === true);
    } catch (_e) { return false; }
  }

  function wsUrl(bind) {
    var proto = (typeof location !== 'undefined' && location.protocol === 'https:') ? 'wss:' : 'ws:';
    return proto + '//' + location.host + '/ws/voice?bind=' + encodeURIComponent(bind) + '&audio_ack=1';
  }

  function sendControl(type, extra) {
    if (!s.ws || s.ws.readyState !== WebSocket.OPEN) return false;
    var frame = { type: type };
    if (extra) { for (var k in extra) { if (Object.prototype.hasOwnProperty.call(extra, k)) frame[k] = extra[k]; } }
    try { s.ws.send(JSON.stringify(frame)); return true; } catch (_e) { return false; }
  }

  function _ackAudioReady(epoch, token) {
    if (!s.connectionId || !s.talkActive || s.serverFsm !== 'listening' ||
        s.serverUpstream !== 'ready' || !token) return false;
    return sendControl('audio_ready', {
      connection_id: s.connectionId,
      epoch: (epoch == null) ? s.serverEpoch : (epoch | 0),
      token: String(token),
    });
  }

  function _boundListeningDisagrees() {
    var st = store();
    if (!st || st.micMode !== 'listening' || s.resetBoundaryPending) return false;
    s.lastFlowAt = 0;
    _setTransport('connecting');
    _setConn('reconnecting');
    _armHealthVerification(false);
    if (!s.serverStateRepairAttempted) {
      s.serverStateRepairAttempted = true;
      if (!s.started) {
        if (sendControl('start')) s.started = true;
      } else {
        sendControl('unmute');
      }
      s.talkActive = true;
    }
    return true;
  }

  function _clearResetBoundary(expectedGen) {
    if (expectedGen != null && expectedGen !== s.resetBoundaryGen) return false;
    if (s.resetBoundaryTimer) clearTimeout(s.resetBoundaryTimer);
    s.resetBoundaryTimer = null;
    s.resetBoundaryPending = false;
    s.resetBoundaryExpectedEpoch = 0;
    return true;
  }

  function _finishResetBoundary(expectedGen) {
    if (!_clearResetBoundary(expectedGen)) return false;
    var st = store();
    if (st && st.micMode === 'listening' && st.transportStatus !== 'flowing') {
      // A reset acknowledgment proves the replacement WhisperLive session is
      // ready. Preserve an already-verified browser capture/transport path so
      // Send never flashes recovery or arms a false failure. If health was not
      // verified before the boundary, keep its bounded verification instead.
      _armHealthVerification(true);
    }
    return true;
  }

  function _beginResetBoundary(expectedEpoch) {
    // A health deadline started before this intentional protocol pause must not
    // abort the reset. Completion below starts a fresh bounded window.
    _clearVerificationTimer();
    _clearResetBoundary();
    var resetGen = ++s.resetBoundaryGen;
    s.resetBoundaryPending = true;
    s.resetBoundaryExpectedEpoch = expectedEpoch | 0;
    s.resetBoundaryTimer = setTimeout(function () {
      if (resetGen !== s.resetBoundaryGen) return;
      s.resetBoundaryTimer = null;
      if (!s.resetBoundaryPending) return;
      s.resetBoundaryPending = false;
      s.resetBoundaryExpectedEpoch = 0;
      _requestTransportRepair('reset_timeout');
    }, RESET_BOUNDARY_VERIFY_MS);
    if (s.resetBoundaryTimer && typeof s.resetBoundaryTimer.unref === 'function') s.resetBoundaryTimer.unref();
  }

  // #43: explicit Send/Clear reset hook. Called by the viewer's Send/Clear handlers
  // (voice-shell.js), the SOLE callers — never inferred from an empty buffer. It
  // (a) raises acceptEpoch so every in-flight pre-reset frame (the re-emit of the
  // just-sent/cleared speech) is dropped before it can repopulate the buffer, and
  // (b) tells the server to reset the WhisperLive session (flush its buffered audio
  // + bump the epoch). The server's post-reset frames carry the new epoch and flow
  // again, so genuinely-new speech is never suppressed. No-op (and harmless) when
  // the flag is off — the legacy discard path owns reset then.
  function resetEpoch(reason) {
    if (!_resetMode()) return false;
    var why = reason || '';
    s.acceptEpoch = Math.max(s.acceptEpoch, s.serverEpoch) + 1;
    s.finals = '';
    // A target switch can preserve text here. Send/Clear are terminal
    // boundaries for that text, so it must not survive the reset and
    // reappear when the server sends buffer_state("").
    s.carryPrefix = '';
    s.lastRendered = '';
    _traceRec('reset', { reason: why, acceptEpoch: s.acceptEpoch });
    if (why === 'clear') _traceScheduleDump();
    var sent = sendControl('reset', { reason: why });
    if (sent) _beginResetBoundary(s.acceptEpoch);
    return sent;
  }

  function _renderBuffer(text, meta) {
    var st = store();
    if (!st || typeof st.setBufferText !== 'function') {
      _diag('NO STORE — text="' + (text || '').slice(-40) + '"');
      return;
    }
    // Carried text (from a mid-buffer session switch) always leads; the new
    // session's transcript appends after it. carryPrefix is '' in the normal
    // case, so this is a no-op until a switch seeds it.
    var core = text || '';
    var full = s.carryPrefix ? (core ? (s.carryPrefix + ' ' + core) : s.carryPrefix) : core;
    s.lastRendered = full;   // remember what's on screen so a Clear can suppress it
    _traceRec('render', full);
    st.setBufferText(full, meta || { update: 'snapshot' });
  }

  function attachSocket(ws, bind) {
    s.ws = ws; s.bind = bind; ws.binaryType = 'arraybuffer';
    ws.addEventListener('open', function () {
      if (ws !== s.ws) return;
      _clearResetBoundary();
      s.wsOpen = true; s.started = false; s.requiresReconnect = false;
      // #43: the server's transcript epoch is PER-CONNECTION and starts at 0 on
      // every fresh ws_voice connection. acceptEpoch/serverEpoch live in module
      // state and survive reconnects, so without this reset a reconnect after any
      // Send/Clear would leave acceptEpoch>0 and silently drop ALL of the new
      // connection's epoch-0 frames (dictation dies with no error). Re-sync to the
      // fresh connection's epoch baseline.
      s.acceptEpoch = 0;
      s.serverEpoch = 0;
      s.connectionId = '';
      s.serverFsm = '';
      s.serverUpstream = '';
      s.voiceStateAt = 0;
      s.lastFlowAt = 0;
      s.lastLocalSendAt = 0;
      s.lastForwarded = 0;
      s.serverStateRepairAttempted = false;
      // An open socket is not proof of working dictation. Keep the recovery
      // treatment until this connection acknowledges actual forwarded audio.
      _setTransport('connecting');
      _setConn('reconnecting');
      // Only declare full recovery (reset the backoff) once the link has been
      // STABLE for a beat — an open-then-close flap must not keep resetting it.
      if (s.stabilityTimer) clearTimeout(s.stabilityTimer);
      s.stabilityTimer = setTimeout(function () { s.reconnectAttempt = 0; s.stabilityTimer = null; }, 3000);
      _diag('socket OPEN → ' + bind);
    });
    ws.addEventListener('message', function (event) {
      if (ws !== s.ws) return;
      var frame;
      try { frame = JSON.parse(event.data); } catch (_e) { return; }
      _traceRec('in', frame);   // record the real inbound frame for replay
      var type = String(frame.type || '');
      if (type === 'voice_state') {
        var stateConnection = String(frame.connection_id || '');
        if (!stateConnection) return;
        if (s.connectionId && stateConnection !== s.connectionId) return;
        s.connectionId = stateConnection;
        s.serverFsm = String(frame.fsm_state || '');
        s.serverUpstream = String(frame.upstream || '');
        if (frame.epoch != null) s.serverEpoch = frame.epoch | 0;
        s.voiceStateAt = _nowMs();
        if (s.serverUpstream === 'unavailable') {
          s.requiresReconnect = true;
          _setTransport('upstream_error');
          _requestTransportRepair('upstream_unavailable');
        } else if (s.serverFsm === 'muted') {
          // Muted is healthy only when it matches operator intent. If the
          // operator wants listening, retry the control once and bound the wait.
          if (!_boundListeningDisagrees()) {
            _clearVerificationTimer();
            _setTransport('connecting');
            _setConn('ok');
          }
        } else if (s.serverFsm === 'listening') {
          // Start/unmute is only control-plane truth. Stay unverified until a
          // later audio_flow proves current PCM crossed the data plane too.
          _setTransport('connecting');
          _setConn('reconnecting');
        } else {
          _boundListeningDisagrees();
        }
        return;
      }
      if (type === 'audio_flow') {
        if (!s.connectionId || String(frame.connection_id || '') !== s.connectionId) return;
        var forwarded = Number(frame.forwarded);
        if (!Number.isSafeInteger(forwarded) || forwarded <= s.lastForwarded) return;
        s.lastForwarded = forwarded;
        s.lastFlowAt = _nowMs();
        s.flowRepairAttempted = false;
        s.requiresReconnect = false;
        _verifyHealthyFlow();
        return;
      }
      if (type === 'committed' || type === 'commit_error') {
        _ackAudioReady(s.serverEpoch, frame.audio_ready_token);
        return;
      }
      var resetBoundaryAcked = false;
      if (type === 'buffer_state' && s.resetBoundaryPending) {
        var boundaryEpoch = (frame.epoch == null) ? 0 : (frame.epoch | 0);
        if (boundaryEpoch >= s.resetBoundaryExpectedEpoch) {
          _ackAudioReady(boundaryEpoch, frame.audio_ready_token);
          _finishResetBoundary(s.resetBoundaryGen);
          resetBoundaryAcked = true;
        }
      }
      // Mute-gate: while muted, the operator wants the box FROZEN. WhisperLive
      // keeps re-transcribing its buffered pre-mute speech and emits a churn of
      // variants (NOT noise — pure silence/white-noise emit nothing); ignore
      // transcript/buffer_state frames so none of that re-renders. Resumes on unmute.
      var _muteSt = store();
      if (_muteSt && _muteSt.micMode === 'muted' && (type === 'transcript' || type === 'buffer_state')) {
        _traceRec('muted-drop', type);
        return;
      }
      // #43 epoch acceptance: in reset mode, every transcript/buffer_state frame
      // carries the server's reset epoch. Drop anything below acceptEpoch — that's
      // the re-emit of just-sent/cleared speech still draining from the pre-reset
      // WhisperLive buffer. Genuine post-reset speech arrives at the new epoch
      // (>= acceptEpoch) and flows. Track the high-water epoch so the next
      // resetEpoch() raises the bar correctly.
      var _rmode = _resetMode();
      if (_rmode && (type === 'transcript' || type === 'buffer_state')) {
        var ep = (frame.epoch == null) ? 0 : (frame.epoch | 0);
        if (ep < s.acceptEpoch) { _traceRec('epoch-drop', ep); return; }
        if (ep > s.serverEpoch) s.serverEpoch = ep;
      }
      if (type === 'transcript') {
        var t = String(frame.text || '').trim();
        if (frame.kind === 'final') {
          if (t) {
            var candF = s.finals ? (s.finals + ' ' + t) : t;
            // Reset mode handles re-emit via epoch; the fuzzy _stripRemoved
            // heuristic is bypassed (it would risk stripping legit text).
            var keptF = _rmode ? candF : _stripRemoved(candF);
            if (!_rmode && s.removed.length) _vlog('FINAL t="' + t.slice(0, 70) + '" removedN=' + s.removed.length + ' kept="' + keptF.slice(0, 70) + '"');
            s.finals = keptF;
            _finalCount++;
            _renderBuffer(s.finals, {
              update: 'final', kind: 'final', epoch: frame.epoch, tsMs: frame.ts_ms,
            });
          }
        } else if (frame.kind === 'partial') {
          if (t) {
            var candP = s.finals ? (s.finals + ' ' + t) : t;
            var keptP = _rmode ? candP : _stripRemoved(candP);
            if (!_rmode && s.removed.length) _vlog('PARTIAL t="' + t.slice(0, 70) + '" removedN=' + s.removed.length + ' kept="' + keptP.slice(0, 70) + '"');
            _renderBuffer(keptP, {
              update: 'partial', kind: 'partial', epoch: frame.epoch, tsMs: frame.ts_ms,
            });
          }
        }
        return;
      }
      if (type === 'buffer_state') {
        // Strip removed text here too: a reconnect restores the server's MANAGER
        // buffer, which re-emits can have re-polluted after a Clear/Send — this
        // path used to bypass suppression entirely.
        var bsRaw = String(frame.text || '');
        var bsKept = _rmode ? bsRaw : _stripRemoved(bsRaw);
        if (!_rmode && bsRaw) _vlog('BUFFER_STATE raw="' + bsRaw.slice(0, 70) + '" removedN=' + s.removed.length + ' kept="' + bsKept.slice(0, 70) + '"');
        s.finals = bsKept;
        if (frame.epoch != null && (frame.epoch | 0) > s.serverEpoch) {
          s.serverEpoch = frame.epoch | 0;
        }
        _renderBuffer(s.finals, {
          update: 'restore', kind: 'buffer_state', epoch: frame.epoch,
        });
        if (!resetBoundaryAcked) _ackAudioReady(frame.epoch, frame.audio_ready_token);
        return;
      }
      if (type === 'error') {
        _clearResetBoundary();
        var code = String(frame.code || '');
        _diag('server ERROR: ' + code + ' ' + String(frame.message || ''));
        if (code === 'whisperlive_connect_failed' || code === 'whisperlive_session_error') {
          s.requiresReconnect = true;
          _setTransport('upstream_error');
          _requestTransportRepair('upstream_error');
          var st2 = store();
          if (st2) st2.sheetError = 'Transcription service unavailable. Reconnect and retry.';
        }
        return;
      }
    });
    ws.addEventListener('close', function () {
      if (ws !== s.ws) return;   // intentional teardown nulls s.ws first → ignored here
      _clearResetBoundary();
      s.wsOpen = false; s.started = false; s.ws = null;
      s.starting = false;
      s.startGen++;
      _setTransport('disconnected');
      if (s.stabilityTimer) { clearTimeout(s.stabilityTimer); s.stabilityTimer = null; }
      if (_wantsConnection()) {
        _scheduleReconnect();   // unexpected drop while we still want to be live
      } else {
        s.talkActive = false;
      }
    });
    ws.addEventListener('error', function () { /* surfaced by close */ });
  }

  function _currentTrackIsLive() {
    try {
      if (!s.stream || typeof s.stream.getTracks !== 'function') return false;
      var tracks = s.stream.getTracks();
      if (!tracks.length || s.trackMuted) return false;
      return tracks.every(function (track) {
        return track.readyState === 'live' && track.enabled !== false && track.muted !== true;
      });
    } catch (_e) { return false; }
  }

  var STALE_START = { staleStart: true };

  function _cleanupUninstalledCapture(stream, ctx, sourceNode, workletNode) {
    try { if (workletNode) { workletNode.port.onmessage = null; workletNode.disconnect(); } } catch (_e) {}
    try { if (sourceNode) sourceNode.disconnect(); } catch (_e) {}
    try { if (stream) stream.getTracks().forEach(function (track) { track.stop(); }); } catch (_e) {}
    try { if (ctx && typeof ctx.close === 'function' && ctx.state !== 'closed') ctx.close(); } catch (_e) {}
  }

  function ensureMicReady(startGen, bind) {
    _setCapture('acquiring');
    if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== 'function') {
      return Promise.reject(new Error('microphone is unavailable in this browser'));
    }
    if (!window.AudioWorkletNode || !window.AudioContext) {
      return Promise.reject(new Error('AudioWorklet is unavailable in this browser'));
    }
    if (s.micGranted) {
      if (s.ctx && s.ctx.state === 'suspended') return s.ctx.resume();
      if (s.ctx && s.ctx.state === 'running' && s.lastFrameAt) _setCapture('live');
      return Promise.resolve();
    }
    // Claim a fresh pipeline generation. Any worklet created by an earlier (or a
    // racing-later) call will have a gen that no longer equals s.captureGen and is
    // refused below — so at most ONE worklet ever streams to the socket.
    var myGen = ++s.captureGen;
    function stale() {
      return myGen !== s.captureGen || startGen !== s.startGen || bind !== s.desiredBind;
    }
    return navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, sampleRate: 16000, echoCancellation: true },
      video: false,
    }).then(function (stream) {
      if (stale()) {
        _cleanupUninstalledCapture(stream, null, null, null);
        throw STALE_START;
      }
      var ctx = (s.primedContext && s.primedContext.state !== 'closed')
        ? s.primedContext
        : new AudioContext({ sampleRate: 16000, latencyHint: 'interactive' });
      s.primedContext = null;
      return ctx.audioWorklet.addModule(WORKLET_URL).then(function () {
        if (stale()) {
          _cleanupUninstalledCapture(stream, ctx, null, null);
          throw STALE_START;
        }
        var sourceNode = ctx.createMediaStreamSource(stream);
        var workletNode = new AudioWorkletNode(ctx, 'voice-smoke-pcm', {
          processorOptions: { targetSampleRate: 16000, frameSamples: 1600 },
        });
        var sinkNode = ctx.createGain();
        sinkNode.gain.value = 0;
        if (stale()) {
          _cleanupUninstalledCapture(stream, ctx, sourceNode, workletNode);
          throw STALE_START;
        }
        workletNode.port.onmessage = function (event) {
          // Superseded pipeline: a teardown/restart happened after this worklet was
          // built. Detach + dismantle it the moment it next fires so it can never
          // interleave its frames with the live pipeline's, then stay silent.
          if (stale()) {
            try { workletNode.port.onmessage = null; workletNode.disconnect(); sourceNode.disconnect(); } catch (_e) {}
            try { stream.getTracks().forEach(function (t) { t.stop(); }); } catch (_e) {}
            try { if (ctx && typeof ctx.close === 'function' && ctx.state !== 'closed') ctx.close(); } catch (_e) {}
            return;
          }
          var payload = event.data || {};
          if (payload.type !== 'audio' || !(payload.buffer instanceof ArrayBuffer)) return;
          s.lastFrameAt = _nowMs();   // worklet alive (mic + AudioContext producing)
          if (_currentTrackIsLive() && ctx.state === 'running') _setCapture('live');
          else _setCapture('interrupted');
          if (!s.talkActive || !s.wsOpen || !s.ws || s.ws.readyState !== WebSocket.OPEN) return;
          if (!s.started || s.requiresReconnect) return;
          try {
            s.ws.send(payload.buffer);
            s.lastLocalSendAt = _nowMs();
          } catch (_e) {}
        };
        sourceNode.connect(workletNode);
        workletNode.connect(sinkNode);
        sinkNode.connect(ctx.destination);
        // Track events are the browser's authoritative source-availability
        // signals. A temporary mute gets one second to clear after foreground;
        // ended is permanent and repairs immediately.
        try {
          stream.getTracks().forEach(function (t) {
            t.addEventListener('mute', function () {
              if (myGen !== s.captureGen) return;
              s.trackMuted = true;
              _setCapture('interrupted');
              if (s.trackMuteTimer) clearTimeout(s.trackMuteTimer);
              s.trackMuteTimer = setTimeout(function () {
                s.trackMuteTimer = null;
                if (myGen !== s.captureGen || !s.trackMuted) return;
                if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
                _requestCaptureRepair('track_muted');
              }, 1000);
            });
            t.addEventListener('unmute', function () {
              if (myGen !== s.captureGen) return;
              s.trackMuted = false;
              if (s.trackMuteTimer) clearTimeout(s.trackMuteTimer);
              s.trackMuteTimer = null;
              _setCapture('acquiring');
            });
            t.addEventListener('ended', function () {
              if (myGen !== s.captureGen) return;
              _diag('mic track ended');
              _setCapture('interrupted');
              _requestCaptureRepair('track_ended');
            });
          });
        } catch (_e) {}
        try {
          ctx.onstatechange = function () {
            if (myGen !== s.captureGen) return;
            if (ctx.state === 'running') {
              // A running context is not current audio evidence. The next
              // worklet frame is the only event that may restore `live`.
              _setCapture('acquiring');
            } else if (ctx.state === 'suspended' || ctx.state === 'interrupted') {
              _setCapture('interrupted');
            }
          };
        } catch (_e) {}
        if (stale()) {
          _cleanupUninstalledCapture(stream, ctx, sourceNode, workletNode);
          throw STALE_START;
        }
        // Dismantle whatever pipeline we're replacing so a torn-down-but-never-
        // refired worklet can't linger with a live mic track (the gen guard only
        // fires on the next frame; a dead-track worklet would otherwise leak).
        if (s.workletNode && s.workletNode !== workletNode) {
          try { s.workletNode.port.onmessage = null; s.workletNode.disconnect(); } catch (_e) {}
        }
        if (s.sourceNode && s.sourceNode !== sourceNode) { try { s.sourceNode.disconnect(); } catch (_e) {} }
        if (s.stream && s.stream !== stream) { try { s.stream.getTracks().forEach(function (t) { t.stop(); }); } catch (_e) {} }
        if (s.ctx && s.ctx !== ctx && typeof s.ctx.close === 'function' && s.ctx.state !== 'closed') { try { s.ctx.close(); } catch (_e) {} }
        s.stream = stream; s.ctx = ctx; s.sourceNode = sourceNode;
        s.workletNode = workletNode; s.sinkNode = sinkNode; s.micGranted = true;
        // Baseline the watchdog at pipeline installation so a worklet that
        // never emits its *first* frame is detected after the same four-second
        // bound as one that stops later. lastLocalSendAt remains zero, so this
        // timestamp alone can never verify listening.
        s.lastFrameAt = _nowMs();
        _setCapture('acquiring');
      });
    });
  }

  function startListening(bind) {
    bind = String(bind || '');
    if (!bind) return;
    if (s.starting && s.desiredBind === bind) return;
    // A bind change is a privacy boundary. Invalidate the pending acquisition
    // before starting the new target so a late getUserMedia/addModule result for
    // the old session can neither install itself nor send a control frame.
    if ((s.bind && s.bind !== bind) || (s.starting && s.desiredBind !== bind)) teardown();
    if (s.starting) return;
    s.bind = bind;
    s.desiredBind = bind;
    s.starting = true;
    var myStartGen = ++s.startGen;
    _armHealthVerification(false);
    _setCapture(s.micGranted ? 'live' : 'acquiring');
    _setTransport('connecting');
    _setConn('reconnecting');
    if (!s.ws) attachSocket(new WebSocket(wsUrl(bind)), bind);
    var startSocket = s.ws;
    ensureMicReady(myStartGen, bind).then(function () {
      if (myStartGen !== s.startGen || bind !== s.desiredBind || startSocket !== s.ws) return;
      // wait briefly for the socket to open, then start the upstream
      var waited = 0;
      (function waitOpen() {
        if (myStartGen !== s.startGen || bind !== s.desiredBind || startSocket !== s.ws) return;
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
          if (myStartGen === s.startGen) s.starting = false;
          return;
        }
        waited += 100;
        setTimeout(waitOpen, 100);
      })();
    }).catch(function (e) {
      if (e === STALE_START || (e && e.staleStart)) return;
      if (myStartGen !== s.startGen || bind !== s.desiredBind) return;
      s.starting = false;
      _setCapture('interrupted');
      var code = e && e.name === 'NotAllowedError' ? 'mic_denied' : 'mic_gesture';
      _requireAction(code);
      var st = store();
      if (st) st.sheetError = 'Mic error: ' + (e && e.message ? e.message : 'could not start');
    });
  }

  function muteListening() {
    _clearVerificationTimer();
    s.serverStateRepairAttempted = false;
    sendControl('mute');
    s.talkActive = false;
    _setTransport('connecting');
  }

  function resumeListening() {
    // Prior flow proves only the pre-mute interval. Bound this new attempt even
    // if the server never emits a state or flow frame.
    s.lastFlowAt = 0;
    s.serverStateRepairAttempted = false;
    _setTransport('connecting');
    _armHealthVerification(true);
    if (!s.started) { if (sendControl('start')) s.started = true; }
    else { sendControl('unmute'); }
    s.talkActive = true;
  }

  function teardown() {
    if (s.reconnectTimer) { clearTimeout(s.reconnectTimer); s.reconnectTimer = null; }
    if (s.stabilityTimer) { clearTimeout(s.stabilityTimer); s.stabilityTimer = null; }
    s.reconnectAttempt = 0;
    _clearIncidentTimer();
    _clearVerificationTimer();
    _clearResetBoundary();
    s.incident = null;
    _publishIncident();
    _setAction(null);
    _setConn('ok');
    s.talkActive = false;
    s.startGen++;
    s.desiredBind = '';
    _releaseWakeLock();
    try { if (s.ws) s.ws.close(1000, 'end'); } catch (_e) {}
    s.ws = null; s.wsOpen = false; s.started = false; s.bind = ''; s.starting = false; s.finals = '';
    s.connectionId = ''; s.serverFsm = ''; s.serverUpstream = ''; s.voiceStateAt = 0;
    s.lastFlowAt = 0; s.lastLocalSendAt = 0; s.lastForwarded = 0;
    s.flowRepairAttempted = false;
    _setTransport('disconnected');
    // Invalidate the live pipeline FIRST so its worklet goes inert immediately.
    _disposeCapturePipeline();
  }

  function react() {
    var st = store();
    if (!st) return;
    // A tab may have a saved resume intent before asynchronous feature flags
    // arrive.  Do not acquire the microphone until the voice client is enabled.
    if (!st.enabled) {
      if (s.ws || s.stream) teardown();
      return;
    }
    if (st.actionRequiredReason) {
      s.talkActive = false;
      return;
    }
    var bound = st.boundSessionId;
    var mode = st.micMode;
    if (bound && mode === 'listening') {
      if (s.bind !== bound || !s.ws) {
        // Genuine target switch (old bind was real, target differs) with text
        // still in the box → carry it over so the new session's transcription
        // appends instead of overwriting. teardown() (inside startListening)
        // wipes s.finals; carryPrefix survives it and is cleared only when the
        // buffer is emptied (Clear/Send/unbind, handled in the clear effect).
        if (s.bind && bound !== s.bind) {
          var carried = (st.bufferText || '').trim();
          if (carried) s.carryPrefix = carried;
        }
        startListening(bound);
      } else if (!s.talkActive && !s.starting) resumeListening();
    } else if (bound && mode === 'muted') {
      if (s.talkActive) muteListening();
    } else if (!bound || mode === 'idle') {
      if (s.ws || s.stream) teardown();
    }
  }

  document.addEventListener('alpine:init', function () {
    if (typeof Alpine === 'undefined' || typeof Alpine.effect !== 'function') return;
    _traceInit();
    Alpine.effect(react);
    // When the buffer is cleared externally (operator pressed Send), reset our
    // accumulator AND tell the server to drop the committed audio — otherwise
    // the old finals re-render into the box and the operator sends duplicates.
    Alpine.effect(function () {
      var st = store();
      if (!st) return;
      var buf = st.bufferText;   // read first to keep the Alpine dependency tracked
      // #43: in reset mode the explicit resetEpoch() hook (called by the Send/Clear
      // handlers) owns reset — an empty buffer is NOT an action here, because it can
      // also be a server-side WhisperLive reset or a reconnect straggler. Never
      // infer a discard from bufferText===''. Legacy path runs only when flag off.
      if (_resetMode()) return;
      // Remember what was DISPLAYED (last render) — NOT just committed finals.
      // Clearing mid-utterance leaves s.finals empty (only a partial was shown),
      // yet whisper_live still finalizes that segment and re-emits the whole
      // sentence; suppression must key off the visible text or it reappears.
      var visible = (s.lastRendered ||
                     (s.carryPrefix ? (s.carryPrefix + ' ' + s.finals) : s.finals) || '').trim();
      if (buf === '' && visible) {
        _vlog('CLEAR remembering="' + visible.slice(0, 80) + '"');
        _traceRec('clear', visible);       // mark the clear, then capture the re-emit window
        _traceScheduleDump();
        _rememberRemoved(visible);
        s.finals = '';
        s.carryPrefix = '';
        s.lastRendered = '';
        sendControl('discard');
      } else if (buf === '' && !visible) {
        _vlog('CLEAR but nothing was displayed — nothing to remember');
      }
    });
  });

  window.Autonomy = window.Autonomy || {};
  window.Autonomy.voiceCapture = {
    teardown: teardown,
    retryReconnect: retryReconnectNow,
    activateFromGesture: activateFromGesture,
    enableFromGesture: enableFromGesture,
    onServerRecovered: onServerRecovered,
    resetEpoch: resetEpoch,   // #43: explicit Send/Clear reset hook (voice-shell.js calls this)
    dumpTrace: function () { return _traceDump('manual'); },
    startTrace: _traceEnable,
    stopTrace: _traceDisable,
    _audioWatchdogTick: _audioWatchdogTick,   // exposed for tests
    _state: s,
  };

  // Audio-stall watchdog: cheap (guards on micMode), runs for the page lifetime.
  if (typeof setInterval === 'function') {
    var _wd = setInterval(_audioWatchdogTick, 2000);
    if (_wd && typeof _wd.unref === 'function') _wd.unref();   // don't pin Node test loops
  }
})();
