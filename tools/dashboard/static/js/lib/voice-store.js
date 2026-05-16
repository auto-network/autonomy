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
  var STORAGE_KEYS = {
    capsulePosition: 'autonomy.voice.capsulePosition',
    discoverabilitySeen: 'autonomy.voice.discoverabilitySeen',
  };

  function _getLocalStorage() {
    try {
      if (typeof window !== 'undefined' && window.localStorage) {
        return window.localStorage;
      }
    } catch (_err) {}
    return null;
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

  function _buildStore() {
    return {
      boundSessionId: '',
      micMode: 'idle',
      bufferText: '',
      capsulePosition: _readPosition(STORAGE_KEYS.capsulePosition),
      pendingRebindTarget: '',
      discoverabilitySeen: _readBool(STORAGE_KEYS.discoverabilitySeen, false),
      awayEventSessionId: '',
      sheetOpen: false,
      sheetMode: 'partial',
      sheetError: '',
      sheetResumeListeningOnDismiss: false,

      get enabled() {
        return _isClientEnabled();
      },

      get active() {
        return !!this.boundSessionId && this.micMode !== 'idle';
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
        this.boundSessionId = sessionId || '';
        this.pendingRebindTarget = '';
        this.awayEventSessionId = '';
        this.micMode = this.boundSessionId ? 'listening' : 'idle';
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
          this.micMode = 'muted';
          return true;
        }
        if (this.micMode === 'muted') {
          this.micMode = 'listening';
          return true;
        }
        return false;
      },

      setMicMode(mode) {
        if (!this.boundSessionId) return false;
        if (mode !== 'listening' && mode !== 'muted' && mode !== 'vad_paused') {
          return false;
        }
        this.micMode = mode;
        return true;
      },

      setBufferText(text) {
        this.bufferText = _normalizeText(text);
      },

      clearBuffer() {
        this.bufferText = '';
        this.sheetError = '';
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
        this.bufferText = '';
        this.micMode = 'idle';
        this.awayEventSessionId = '';
        this.sheetOpen = false;
        this.sheetMode = 'partial';
        this.sheetError = '';
        this.sheetResumeListeningOnDismiss = false;
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
        this.sheetResumeListeningOnDismiss =
          this.micMode === 'listening' || this.micMode === 'vad_paused';
        if (this.sheetResumeListeningOnDismiss) {
          this.micMode = 'muted';
        }
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
          this.micMode = 'listening';
        }
        this.sheetResumeListeningOnDismiss = false;
        return true;
      },

      expandSheet() {
        if (!this.sheetOpen) return false;
        this.sheetMode = 'full';
        return true;
      },

      async sendBuffer() {
        if (!this.boundSessionId) {
          this.sheetError = 'Send failed. Session is no longer available.';
          return false;
        }
        var body = (this.bufferText || '').trim();
        if (!body) return false;
        this.sheetError = '';
        try {
          var res = await _sendFetch('/api/session/send', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              message: body,
              tmux_session: this.boundSessionId,
            }),
          });
          var data = await res.json();
          if (!res.ok || !data || data.ok !== true) {
            this.sheetError = _sendErrorMessage(res && typeof res.status === 'number' ? res.status : 0);
            return false;
          }
          this.bufferText = '';
          this.sheetError = '';
          this.sheetOpen = false;
          this.sheetMode = 'partial';
          if (this.sheetResumeListeningOnDismiss && this.boundSessionId) {
            this.micMode = 'listening';
          }
          this.sheetResumeListeningOnDismiss = false;
          return true;
        } catch (_err) {
          this.sheetError = _sendErrorMessage(0);
          return false;
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
})();
