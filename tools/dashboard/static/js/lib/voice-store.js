/**
 * Shared voice state — Alpine.store('voice').
 *
 * S5 substrate only: global bind/mic/capsule state with per-device
 * persistence for discoverability + capsule position. Page/template
 * wiring lands in follow-on commits.
 *
 * Contract source:
 * - graph://a9fea0aa-0a8 (S5 bead, implementation substrate)
 * - latest operator-approved artifact at
 *   /workspace/output/designs/session-viewer-voice-fidelity/session-viewer-mobile.html
 */
(function () {
  var VOICE_CLIENT_FLAG = 'voice.client_enabled';
  var VOICEOVER_FLAG = 'voice.voiceover_enabled';
  var STORAGE_KEYS = {
    capsulePosition: 'autonomy.voice.capsulePosition',
    discoverabilitySeen: 'autonomy.voice.discoverabilitySeen',
    resumeIntent: 'autonomy.voice.resumeIntent',
  };

  function _getLocalStorage() {
    try {
      if (typeof window !== 'undefined' && window.localStorage) {
        return window.localStorage;
      }
    } catch (_err) {}
    return null;
  }

  // Dictation is deliberately scoped to one browser tab.  Persist enough to
  // reconnect after a PWA reload, but never microphone permission, audio, or
  // transcript text; the voice server restores the latter with buffer_state.
  function _getSessionStorage() {
    try {
      if (typeof window !== 'undefined' && window.sessionStorage) {
        return window.sessionStorage;
      }
    } catch (_err) {}
    return null;
  }

  function _readResumeIntent() {
    var storage = _getSessionStorage();
    if (!storage) return null;
    try {
      var raw = storage.getItem(STORAGE_KEYS.resumeIntent);
      if (!raw) return null;
      var value = JSON.parse(raw);
      if (!value || typeof value.sessionId !== 'string' || !value.sessionId ||
          (value.micMode !== 'listening' && value.micMode !== 'muted')) {
        return null;
      }
      return { sessionId: value.sessionId, micMode: value.micMode };
    } catch (_err) {
      return null;
    }
  }

  function _writeResumeIntent(sessionId, micMode) {
    var storage = _getSessionStorage();
    if (!storage) return;
    try {
      if (!sessionId || (micMode !== 'listening' && micMode !== 'muted')) {
        storage.removeItem(STORAGE_KEYS.resumeIntent);
        return;
      }
      storage.setItem(STORAGE_KEYS.resumeIntent, JSON.stringify({
        sessionId: sessionId,
        micMode: micMode,
      }));
    } catch (_err) {}
  }

  function _readBool(key, fallback) {
    var storage = _getLocalStorage();
    if (!storage) return fallback;
    try {
      var value = storage.getItem(key);
      if (value === '1' || value === 'true') return true;
      if (value === '0' || value === 'false') return false;
    } catch (_err) {}
    return fallback;
  }

  function _writeBool(key, value) {
    var storage = _getLocalStorage();
    if (!storage) return;
    try {
      storage.setItem(key, value ? '1' : '0');
    } catch (_err) {}
  }

  function _normalizePosition(value) {
    if (!value || typeof value !== 'object') return null;
    var x = Number(value.x);
    var y = Number(value.y);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
    return { x: x, y: y };
  }

  function _readPosition(key) {
    var storage = _getLocalStorage();
    if (!storage) return null;
    try {
      var raw = storage.getItem(key);
      if (!raw) return null;
      return _normalizePosition(JSON.parse(raw));
    } catch (_err) {
      return null;
    }
  }

  function _writePosition(key, value) {
    var storage = _getLocalStorage();
    if (!storage) return;
    try {
      if (!value) {
        storage.removeItem(key);
        return;
      }
      storage.setItem(key, JSON.stringify(value));
    } catch (_err) {}
  }

  function _getFlagsStore() {
    try {
      if (typeof Alpine === 'undefined' || typeof Alpine.store !== 'function') {
        return null;
      }
      return Alpine.store('flags') || null;
    } catch (_err) {
      return null;
    }
  }

  function _isClientEnabled() {
    var flags = _getFlagsStore();
    if (!flags || typeof flags.get !== 'function') return false;
    try {
      return flags.get(VOICE_CLIENT_FLAG) === true;
    } catch (_err) {
      return false;
    }
  }

  function _isVoiceoverEnabled() {
    var flags = _getFlagsStore();
    if (!flags || typeof flags.get !== 'function') return false;
    try {
      return flags.get(VOICEOVER_FLAG) === true;
    } catch (_err) {
      return false;
    }
  }

  function _isLiveSession(opts) {
    return !!(opts && opts.isLive === true);
  }

  function _normalizeText(value) {
    return typeof value === 'string' ? value : (value == null ? '' : String(value));
  }

  function _sendFetch(path, opts) {
    if (typeof window !== 'undefined' && window.Autonomy && typeof window.Autonomy.fetch === 'function') {
      return window.Autonomy.fetch(path, opts);
    }
    if (typeof fetch === 'function') {
      return fetch(path, opts);
    }
    return Promise.reject(new Error('voice-store: fetch is unavailable'));
  }

  function _sendErrorMessage(status) {
    if (status === 401 || status === 403) {
      return 'Send failed. Voice permission was revoked. Refresh and sign in again.';
    }
    if (status === 404) {
      return 'Send failed. Session is no longer available.';
    }
    return 'Send failed. Session connection dropped. Retry after reconnecting or end voice on this session.';
  }

  function _getSessionStore(sessionId) {
    try {
      if (typeof window === 'undefined' ||
          typeof window.getSessionStore !== 'function') return null;
      return window.getSessionStore(sessionId) || null;
    } catch (_err) {
      return null;
    }
  }

  function _newOutboxId(sessionId) {
    if (typeof window !== 'undefined' && typeof window.newOutboxId === 'function') {
      return window.newOutboxId();
    }
    return 'ob_' + sessionId;
  }

  function _resetVoiceCaptureEpoch(reason) {
    try {
      if (typeof window === 'undefined' || !window.Autonomy ||
          !window.Autonomy.voiceCapture ||
          typeof window.Autonomy.voiceCapture.resetEpoch !== 'function') {
        return false;
      }
      return window.Autonomy.voiceCapture.resetEpoch(reason) === true;
    } catch (_err) {
      return false;
    }
  }

  // Stable, snapshot-based live-transcript seam for dashboard plugins. Whisper
  // partials revise earlier words, so consumers always receive the complete
  // current buffer plus a monotonic revision — never append-only deltas.
  var _bufferSubscribers = [];
  var _bufferRevision = 0;
  var _lastBufferSnapshot = Object.freeze({
    revision: 0,
    text: '',
    update: 'initial',
    kind: '',
    sessionId: '',
    epoch: 0,
    tsMs: 0,
  });
  var _surfaceClaimSeq = 0;

  function _voiceStore() {
    try {
      if (typeof Alpine === 'undefined' || typeof Alpine.store !== 'function') return null;
      return Alpine.store('voice') || null;
    } catch (_err) {
      return null;
    }
  }

  function _publishBuffer(store, meta) {
    var detail = meta && typeof meta === 'object' ? meta : {};
    _bufferRevision += 1;
    _lastBufferSnapshot = Object.freeze({
      revision: _bufferRevision,
      text: _normalizeText(store && store.bufferText),
      update: String(detail.update || detail.kind || 'snapshot'),
      kind: String(detail.kind || ''),
      sessionId: String((store && store.boundSessionId) || ''),
      epoch: Number.isFinite(Number(detail.epoch)) ? Number(detail.epoch) : 0,
      tsMs: Number.isFinite(Number(detail.tsMs)) ? Number(detail.tsMs) : 0,
    });
    var listeners = _bufferSubscribers.slice();
    for (var i = 0; i < listeners.length; i++) {
      try { listeners[i](_lastBufferSnapshot); } catch (_err) {}
    }
    return _lastBufferSnapshot;
  }

  function _snapshot() {
    var store = _voiceStore();
    if (!store || _lastBufferSnapshot.text === store.bufferText) return _lastBufferSnapshot;
    return Object.freeze({
      revision: _bufferRevision,
      text: _normalizeText(store.bufferText),
      update: 'snapshot',
      kind: '',
      sessionId: String(store.boundSessionId || ''),
      epoch: 0,
      tsMs: 0,
    });
  }

  function _subscribeBuffer(callback) {
    if (typeof callback !== 'function') return function () {};
    var declaration = _activePluginVoiceDeclaration();
    if (!declaration || declaration.voice.live_transcript !== true) {
      return function () {};
    }
    _bufferSubscribers.push(callback);
    try { callback(_snapshot()); } catch (_err) {}
    var active = true;
    return function () {
      if (!active) return;
      active = false;
      var index = _bufferSubscribers.indexOf(callback);
      if (index >= 0) _bufferSubscribers.splice(index, 1);
    };
  }

  function _activePluginVoiceDeclaration() {
    try {
      var autonomy = window.Autonomy || {};
      var pluginId = autonomy._activePluginId || '';
      var plugins = Array.isArray(autonomy.plugins) ? autonomy.plugins : [];
      var plugin = plugins.find(function (row) { return row && row.id === pluginId; });
      return plugin ? { id: pluginId, voice: plugin.voice || {} } : null;
    } catch (_err) {
      return null;
    }
  }

  function _claimSurface(options) {
    var store = _voiceStore();
    var declaration = _activePluginVoiceDeclaration();
    var opts = options && typeof options === 'object' ? options : {};
    if (!store || !declaration || declaration.voice.live_transcript !== true) return null;
    if (opts.caption === 'plugin' && declaration.voice.replace_caption !== true) return null;
    if (opts.controls === 'plugin' && declaration.voice.replace_controls !== true) return null;
    var claimId = ++_surfaceClaimSeq;
    store.surfaceClaim = {
      claimId: claimId,
      pluginId: declaration.id,
      caption: opts.caption === 'plugin' ? 'plugin' : 'platform',
      controls: opts.controls === 'plugin' ? 'plugin' : 'platform',
    };
    // A plugin that owns the controls must not inherit an already-open platform
    // keyboard sheet underneath its own UI.
    if (store.surfaceClaim.controls === 'plugin') store.sheetOpen = false;
    var released = false;
    return {
      pluginId: declaration.id,
      release: function () {
        if (released) return;
        released = true;
        if (store.surfaceClaim && store.surfaceClaim.claimId === claimId) {
          store.surfaceClaim = null;
        }
      },
    };
  }

  function _publicSnapshot() {
    var declaration = _activePluginVoiceDeclaration();
    if (!declaration || declaration.voice.live_transcript !== true) return null;
    return _snapshot();
  }

  function _releaseSurfaceClaim() {
    var store = _voiceStore();
    if (!store || !store.surfaceClaim) return false;
    store.surfaceClaim = null;
    return true;
  }

  function _clearBuffer(reason) {
    var store = _voiceStore();
    var declaration = _activePluginVoiceDeclaration();
    if (!store || !declaration || declaration.voice.live_transcript !== true ||
        typeof store.clearBuffer !== 'function') return false;
    var update = reason || 'clear';
    store.clearBuffer(update);
    _resetVoiceCaptureEpoch(update);
    return true;
  }

  function _buildStore() {
    var resumeIntent = _readResumeIntent();
    return {
      boundSessionId: resumeIntent ? resumeIntent.sessionId : '',
      micMode: resumeIntent ? resumeIntent.micMode : 'idle',
      // micMode is the persisted operator intent kept for API compatibility.
      // These fields are current-document observations and are never persisted.
      captureStatus: 'absent',
      transportStatus: 'disconnected',
      actionRequiredReason: null,
      wakeStatus: 'released',
      recoveryIncident: null,
      // Voice-socket health, driven by voice-capture's reconnect loop:
      //   'ok'            — connected (or not yet needed)
      //   'reconnecting'  — dropped, auto-retrying with backoff (mic shows red+spin)
      //   'disconnected'  — backoff exhausted; tap the mic to retry (solid red)
      connState: 'ok',
      bufferText: '',
      surfaceClaim: null,
      capsulePosition: _readPosition(STORAGE_KEYS.capsulePosition),
      pendingRebindTarget: '',
      discoverabilitySeen: _readBool(STORAGE_KEYS.discoverabilitySeen, false),
      awayEventSessionId: '',
      // The session the operator is currently VIEWING (published by the active
      // session viewer). When it differs from boundSessionId, input is going to a
      // different session — the cross-session cue (#23) the capsule + sheet show.
      viewedSessionId: '',
      sheetOpen: false,
      sheetMode: 'partial',
      sheetError: '',
      sheetOpenedAt: 0,
      sheetResumeListeningOnDismiss: false,
      // The existing session path remains the default. Voiceover is an
      // explicit, read-only destination that answers about the viewed session.
      deliveryMode: 'session',
      voiceoverBusy: false,
      voiceoverReply: '',
      voiceoverHistory: [],
      attachments: [],
      _nextAttachId: 0,

      get enabled() {
        return _isClientEnabled();
      },

      get active() {
        return !!this.boundSessionId && this.micMode !== 'idle';
      },

      get desiredMode() {
        return this.micMode;
      },

      get effectiveState() {
        if (this.micMode === 'idle' || !this.boundSessionId) return 'off';
        if (this.actionRequiredReason) return 'enable_required';
        if (this.recoveryIncident && this.recoveryIncident.active) return 'repairing';
        if (this.micMode === 'muted') return 'muted';
        if (this.micMode === 'vad_paused') return 'vad_paused';
        if (this.captureStatus === 'live' && this.transportStatus === 'flowing') {
          return 'listening';
        }
        return 'checking';
      },

      get voiceoverEnabled() {
        return _isVoiceoverEnabled();
      },

      get voiceoverActive() {
        return this.voiceoverEnabled && this.deliveryMode === 'voiceover';
      },

      // True while any attachment's upload is still in flight (no path yet).
      // Send is blocked until every staged file has a server path, so the
      // outgoing body can never reference an attachment that didn't land.
      get attachmentsPending() {
        return this.attachments.some(function (a) { return !a.path; });
      },

      // Send is allowed when there's transcript text OR at least one
      // fully-uploaded attachment, and nothing is mid-upload.
      get canSend() {
        if (this.voiceoverActive) {
          return !this.voiceoverBusy && !!(this.bufferText || '').trim();
        }
        if (this.attachmentsPending) return false;
        if ((this.bufferText || '').trim()) return true;
        return this.attachments.some(function (a) { return !!a.path; });
      },

      requestBind(sessionId, opts) {
        if (!this.enabled) return { ok: false, reason: 'disabled' };
        if (!_isLiveSession(opts)) return { ok: false, reason: 'dead' };
        if (!sessionId) return { ok: false, reason: 'missing_session' };
        if (this.boundSessionId === sessionId) {
          return { ok: true, reason: 'already_bound' };
        }
        if (this.boundSessionId && this.boundSessionId !== sessionId) {
          this.pendingRebindTarget = sessionId;
          return { ok: false, reason: 'confirm' };
        }
        this.bindSession(sessionId);
        return { ok: true, reason: 'bound' };
      },

      bindSession(sessionId) {
        // Switch-takes-buffer (#23): a rebind deliberately PRESERVES bufferText so
        // an in-flight dictation cuts over to the new target instead of being
        // stranded. Do NOT reset bufferText here — only a full unbind clears it.
        var priorActionRequired = this.boundSessionId ? this.actionRequiredReason : null;
        this.boundSessionId = sessionId || '';
        this.pendingRebindTarget = '';
        this.awayEventSessionId = '';
        this.micMode = this.boundSessionId ? 'listening' : 'idle';
        this.captureStatus = 'absent';
        this.transportStatus = this.boundSessionId ? 'connecting' : 'disconnected';
        this.actionRequiredReason = priorActionRequired || null;
        this.recoveryIncident = null;
        _writeResumeIntent(this.boundSessionId, this.micMode);
      },

      confirmRebind() {
        if (!this.pendingRebindTarget) return false;
        var target = this.pendingRebindTarget;
        this.pendingRebindTarget = '';
        this.bindSession(target);
        return true;
      },

      cancelRebind() {
        this.pendingRebindTarget = '';
      },

      toggleMic() {
        if (!this.boundSessionId) return false;
        if (this.micMode === 'listening' || this.micMode === 'vad_paused') {
          return this.setMicMode('muted');
        }
        if (this.micMode === 'muted') {
          return this.setMicMode('listening');
        }
        return false;
      },

      setMicMode(mode) {
        if (!this.boundSessionId) return false;
        if (mode !== 'listening' && mode !== 'muted' && mode !== 'vad_paused') {
          return false;
        }
        this.micMode = mode;
        if (mode === 'listening') {
          if (this.captureStatus === 'absent') this.captureStatus = 'acquiring';
          if (this.transportStatus === 'disconnected') this.transportStatus = 'connecting';
        }
        // A push-to-talk pause is transient.  Reloading resumes ordinary
        // listening rather than preserving a stale pressed-button state.
        _writeResumeIntent(this.boundSessionId, mode === 'vad_paused' ? 'listening' : mode);
        return true;
      },

      setCaptureStatus(state) {
        if (state !== 'absent' && state !== 'acquiring' &&
            state !== 'live' && state !== 'interrupted') return false;
        this.captureStatus = state;
        return true;
      },

      setTransportStatus(state) {
        if (state !== 'disconnected' && state !== 'connecting' &&
            state !== 'flowing' && state !== 'stalled' &&
            state !== 'upstream_error') return false;
        this.transportStatus = state;
        return true;
      },

      setActionRequired(reason) {
        this.actionRequiredReason = reason ? String(reason) : null;
        return true;
      },

      setWakeStatus(state) {
        if (state !== 'unsupported' && state !== 'acquiring' &&
            state !== 'held' && state !== 'released' && state !== 'denied') return false;
        this.wakeStatus = state;
        return true;
      },

      setRecoveryIncident(incident) {
        this.recoveryIncident = incident && typeof incident === 'object'
          ? incident
          : null;
        return true;
      },

      setConnState(state) {
        if (state !== 'ok' && state !== 'reconnecting' && state !== 'disconnected') return false;
        this.connState = state;
        return true;
      },

      setViewedSession(sessionId) {
        this.viewedSessionId = sessionId || '';
      },

      setBufferText(text, meta) {
        this.bufferText = _normalizeText(text);
        return _publishBuffer(this, meta);
      },

      publishBuffer(update) {
        return _publishBuffer(this, { update: update || 'edit' });
      },

      clearBuffer(update) {
        this.setBufferText('', { update: update || 'clear' });
        this.sheetError = '';
        // Tactile confirmation that the buffer was wiped (#35). Clear-only — Send
        // empties bufferText directly, not through here.
        try {
          if (typeof window !== 'undefined' && window.Autonomy &&
              typeof window.Autonomy.haptic === 'function') {
            window.Autonomy.haptic();
          }
        } catch (_e) {}
      },

      setDeliveryMode(mode) {
        if (mode !== 'session' && mode !== 'voiceover') return false;
        if (mode === 'voiceover' && !this.voiceoverEnabled) return false;
        this.deliveryMode = mode;
        this.sheetError = '';
        return true;
      },

      pulseAwayEvent(sessionId) {
        if (!sessionId || sessionId !== this.boundSessionId) return false;
        this.awayEventSessionId = sessionId;
        return true;
      },

      clearAwayEvent() {
        this.awayEventSessionId = '';
      },

      clearPendingRebindIfSessionEnded(sessionId) {
        if (sessionId && sessionId === this.pendingRebindTarget) {
          this.pendingRebindTarget = '';
        }
        if (sessionId && sessionId === this.boundSessionId) {
          this.endSession();
        }
      },

      endSession() {
        this.boundSessionId = '';
        this.pendingRebindTarget = '';
        this.setBufferText('', { update: 'end' });
        this.micMode = 'idle';
        this.captureStatus = 'absent';
        this.transportStatus = 'disconnected';
        this.actionRequiredReason = null;
        this.recoveryIncident = null;
        _writeResumeIntent('', 'idle');
        this.awayEventSessionId = '';
        this.sheetOpen = false;
        this.sheetMode = 'partial';
        this.sheetError = '';
        this.sheetResumeListeningOnDismiss = false;
        this.deliveryMode = 'session';
        this.voiceoverBusy = false;
        this.voiceoverReply = '';
        this.voiceoverHistory = [];
      },

      setCapsulePosition(position) {
        var normalized = _normalizePosition(position);
        if (!normalized) return false;
        this.capsulePosition = normalized;
        _writePosition(STORAGE_KEYS.capsulePosition, normalized);
        return true;
      },

      resetCapsulePosition() {
        this.capsulePosition = null;
        _writePosition(STORAGE_KEYS.capsulePosition, null);
      },

      markDiscoverabilitySeen() {
        this.discoverabilitySeen = true;
        _writeBool(STORAGE_KEYS.discoverabilitySeen, true);
      },

      openSheet() {
        if (!this.enabled || !this.boundSessionId) return false;
        this.sheetOpen = true;
        this.sheetMode = 'partial';
        this.sheetError = '';
        // Timestamp the open so a tap-outside dismiss can ignore the trailing
        // synthesized click from the very tap that opened the sheet (the
        // original open-then-instantly-close flicker).
        this.sheetOpenedAt = (typeof Date !== 'undefined' && Date.now) ? Date.now() : 0;
        // Jeremy 2026-05-31: keep capturing while the sheet is open so the
        // full transcript streams LIVE into the visible editor. This overrides
        // the spec's "opening the sheet implicitly mutes capture" (state-matrix
        // L127) — the operator wants to watch the whole message fill as they
        // speak, not have the mic cut out when the text view is up.
        this.sheetResumeListeningOnDismiss = false;
        return true;
      },

      collapseSheet() {
        if (!this.sheetOpen) return false;
        this.sheetMode = 'partial';
        return true;
      },

      dismissSheet() {
        if (!this.sheetOpen) return false;
        this.sheetOpen = false;
        this.sheetMode = 'partial';
        this.sheetError = '';
        if (this.sheetResumeListeningOnDismiss && this.boundSessionId) {
          this.setMicMode('listening');
        }
        this.sheetResumeListeningOnDismiss = false;
        return true;
      },

      expandSheet() {
        if (!this.sheetOpen) return false;
        this.sheetMode = 'full';
        return true;
      },

      // Upload each picked file to the bound session's workspace and stage a
      // chip in the sheet. Mirrors the keyboard composer's addFiles: the same
      // /api/upload endpoint (which routes host sessions to a readable host
      // path and container sessions to a docker-cp'd /tmp path), passing the
      // bound session so the file lands where that agent can open it.
      addAttachmentFiles(fileList) {
        if (!fileList || !this.boundSessionId) return;
        var tmux = this.boundSessionId;
        var self = this;
        for (var i = 0; i < fileList.length; i++) {
          var file = fileList[i];
          var id = ++this._nextAttachId;
          var isImage = !!(file.type && file.type.indexOf('image/') === 0);
          var att = {
            id: id, name: file.name || 'upload', isImage: isImage,
            dataUrl: null, path: null, rel_path: null, mime: null, size: null,
          };
          this.attachments.push(att);

          if (isImage && typeof FileReader === 'function') {
            (function (attId, f) {
              var reader = new FileReader();
              reader.onload = function (e) {
                var found = self.attachments.find(function (a) { return a.id === attId; });
                if (found) found.dataUrl = e.target.result;
              };
              reader.readAsDataURL(f);
            })(id, file);
          }

          var form = new FormData();
          form.append('file', file);
          form.append('tmux_session', tmux);
          (function (attId) {
            _sendFetch('/api/upload', { method: 'POST', body: form })
              .then(function (r) { return r.json(); })
              .then(function (data) {
                var found = self.attachments.find(function (a) { return a.id === attId; });
                if (!found) return;
                if (data && data.ok) {
                  var meta = (data.files && data.files[0]) || data;
                  found.path = meta.path;
                  found.rel_path = meta.rel_path || '';
                  found.mime = meta.mime || '';
                  found.size = meta.size || 0;
                } else {
                  self.removeAttachment(attId);
                  self.sheetError = 'Attachment upload failed.';
                }
              })
              .catch(function () {
                self.removeAttachment(attId);
                self.sheetError = 'Attachment upload failed.';
              });
          })(id);
        }
      },

      removeAttachment(id) {
        this.attachments = this.attachments.filter(function (a) { return a.id !== id; });
      },

      clearAttachments() {
        this.attachments = [];
      },

      // Compose attachment path(s) + transcript into one body, matching the
      // keyboard composer's buildComposerBody: paths first (one per line), a
      // blank line, then the text — so the viewer's user-turn renderer can
      // surface inline thumbnails for path lines and the agent receives both.
      _buildSendBody() {
        var trimmed = (this.bufferText || '').trim();
        var lines = [];
        for (var i = 0; i < this.attachments.length; i++) {
          if (this.attachments[i].path) lines.push(this.attachments[i].path);
        }
        if (trimmed) {
          if (lines.length) lines.push('');
          lines.push(trimmed);
        }
        return lines.join('\n');
      },

      async sendBuffer() {
        if (!this.boundSessionId) {
          this.sheetError = 'Send failed. Session is no longer available.';
          return false;
        }
        if (this.attachmentsPending) {
          this.sheetError = 'Attachment still uploading…';
          return false;
        }
        var body = this._buildSendBody();
        if (!body) return false;
        this.sheetError = '';
        if (typeof window === 'undefined' || typeof window.stageOutboxSend !== 'function') {
          this.sheetError = 'Send failed. Message outbox is unavailable.';
          return false;
        }
        try {
          var sessionStore = _getSessionStore(this.boundSessionId);
          var existing = sessionStore && sessionStore.outbox;
          if (existing && (typeof existing.text !== 'string' || !existing.text.trim())) {
            sessionStore.outbox = null;
            existing = null;
          }
          if (existing && !(existing.source === 'voice' && existing.state === 'capturing')) {
            this.sheetError = 'Message still pending.';
            return false;
          }
          var baseOutbox = existing || {
            localId: _newOutboxId(this.boundSessionId),
            source: 'voice',
            ts: (typeof Date !== 'undefined' && Date.now) ? Date.now() : 0,
          };
          window.stageOutboxSend(this.boundSessionId, {
            localId: baseOutbox.localId,
            state: 'sending',
            source: 'voice',
            text: body,
            ts: baseOutbox.ts || ((typeof Date !== 'undefined' && Date.now) ? Date.now() : 0),
          }, { tmuxSession: this.boundSessionId });
          _resetVoiceCaptureEpoch('send');
          this.setBufferText('', { update: 'send' });
          this.clearAttachments();
          this.sheetError = '';
          this.sheetOpen = false;
          this.sheetMode = 'partial';
          if (this.sheetResumeListeningOnDismiss && this.boundSessionId) {
            this.setMicMode('listening');
          }
          this.sheetResumeListeningOnDismiss = false;
          return true;
        } catch (_err) {
          this.sheetError = _sendErrorMessage(0);
          return false;
        }
      },

      speakVoiceover(text) {
        if (!text || typeof window === 'undefined' || !window.speechSynthesis ||
            typeof window.SpeechSynthesisUtterance !== 'function') return false;
        var self = this;
        var resumeMode = (this.micMode === 'listening' || this.micMode === 'vad_paused')
          ? 'listening' : '';
        try {
          window.speechSynthesis.cancel();
          var utterance = new window.SpeechSynthesisUtterance(text);
          utterance.rate = 1.04;
          utterance.pitch = 0.98;
          // Do not feed Voiceover's own speech back through Whisper. Resume
          // only if capture was active and the operator has not changed state.
          if (resumeMode && typeof this.setMicMode === 'function') this.setMicMode('muted');
          var restoreCapture = function () {
            if (resumeMode && self.boundSessionId && self.micMode === 'muted' &&
                typeof self.setMicMode === 'function') self.setMicMode(resumeMode);
          };
          utterance.onend = restoreCapture;
          utterance.onerror = restoreCapture;
          window.speechSynthesis.speak(utterance);
          return true;
        } catch (_err) {
          if (resumeMode && this.boundSessionId && this.micMode === 'muted' &&
              typeof this.setMicMode === 'function') this.setMicMode(resumeMode);
          return false;
        }
      },

      async askVoiceover() {
        if (!this.voiceoverEnabled) {
          this.deliveryMode = 'session';
          this.sheetError = 'Voiceover is disabled.';
          return false;
        }
        var question = (this.bufferText || '').trim();
        var sessionId = this.viewedSessionId || this.boundSessionId;
        if (!sessionId) {
          this.sheetError = 'Voiceover needs a session to read.';
          return false;
        }
        if (!question || this.voiceoverBusy) return false;

        this.voiceoverBusy = true;
        this.sheetError = '';
        try {
          var response = await _sendFetch('/api/voiceover/ask', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              session_id: sessionId,
              question: question,
              history: this.voiceoverHistory.slice(-8),
            }),
          });
          var data = await response.json();
          if (!response.ok || !data || data.ok !== true || !data.text) {
            this.sheetError = (data && data.error) || 'Voiceover could not answer right now.';
            return false;
          }
          this.voiceoverReply = String(data.text);
          this.voiceoverHistory.push({ role: 'user', content: question });
          this.voiceoverHistory.push({ role: 'assistant', content: this.voiceoverReply });
          if (this.voiceoverHistory.length > 8) {
            this.voiceoverHistory = this.voiceoverHistory.slice(-8);
          }
          _resetVoiceCaptureEpoch('voiceover');
          this.setBufferText('', { update: 'voiceover' });
          this.speakVoiceover(this.voiceoverReply);
          return true;
        } catch (_err) {
          this.sheetError = 'Voiceover\'s local model is unavailable. Check Ollama and try again.';
          return false;
        } finally {
          this.voiceoverBusy = false;
        }
      },
    };
  }

  document.addEventListener('alpine:init', function() {
    Alpine.store('voice', _buildStore());
  });

  window.Autonomy = window.Autonomy || {};
  window.Autonomy.voice = window.Autonomy.voice || {};
  window.Autonomy.voice.storageKeys = STORAGE_KEYS;
  window.Autonomy.voice.subscribe = _subscribeBuffer;
  window.Autonomy.voice.snapshot = _publicSnapshot;
  window.Autonomy.voice.claimSurface = _claimSurface;
  window.Autonomy.voice.releaseSurfaceClaim = _releaseSurfaceClaim;
  window.Autonomy.voice.clearBuffer = _clearBuffer;
})();
