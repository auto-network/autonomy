/**
 * Shared voice-dot UI helpers.
 *
 * Owns the flag-gated "tap to talk to this session" affordance on
 * existing viewer/card dots. The shell capsule is a separate follow-on
 * commit; this file only covers the dot state model and pointer
 * interactions.
 */
(function () {
  var HOLD_MS = 550;
  var TAP_STATES = {
    idle: true,
    listening: true,
    muted: true,
    vad_paused: true,
  };

  function _voiceStore() {
    try {
      if (typeof Alpine === 'undefined' || typeof Alpine.store !== 'function') return null;
      return Alpine.store('voice') || null;
    } catch (_err) {
      return null;
    }
  }

  function _isLive(opts) {
    return !!(opts && opts.isLive === true);
  }

  function _voiceBindKey(sessionLike) {
    if (!sessionLike) return '';
    if (typeof sessionLike === 'string') return sessionLike;
    return sessionLike.tmux_session || sessionLike.sessionKey || sessionLike.session_id || sessionLike.id || '';
  }

  function _stateFor(sessionId, opts) {
    var voice = _voiceStore();
    if (!_isLive(opts)) return 'dead';
    if (!voice || voice.enabled !== true) return 'flag_off';
    if (voice.boundSessionId === sessionId && voice.micMode && TAP_STATES[voice.micMode]) {
      return voice.micMode;
    }
    return 'idle';
  }

  function _isActiveState(state) {
    return state === 'listening' || state === 'muted' || state === 'vad_paused';
  }

  function _dotInteractionSuppressed(sessionId) {
    var voice = _voiceStore();
    return !!(
      voice &&
      voice.sheetOpen === true &&
      voice.boundSessionId &&
      voice.boundSessionId === sessionId
    );
  }

  function _buttonDisabled(sessionId, opts) {
    var state = _stateFor(sessionId, opts);
    return state === 'dead' || state === 'flag_off' || _dotInteractionSuppressed(sessionId);
  }

  function _buttonTitle(sessionId, opts) {
    var state = _stateFor(sessionId, opts);
    if (state === 'dead') return 'Session is not live';
    if (state === 'flag_off') return 'Voice controls are disabled';
    if (state === 'idle') return 'Talk to this session';
    if (state === 'muted') return 'Unmute voice';
    if (state === 'vad_paused') return 'Mute voice';
    return 'Mute voice';
  }

  function _buttonAriaLabel(sessionId, opts) {
    return _buttonTitle(sessionId, opts);
  }

  function _viewerDotClasses(sessionId, opts) {
    var voice = _voiceStore();
    var state = _stateFor(sessionId, opts);
    return {
      'indicator--green': state === 'idle' || state === 'flag_off',
      'indicator--gray': state === 'dead',
      'indicator-dot--working': state === 'idle' && !!(opts && opts.isWorking),
      'voice-dot--bindable': state === 'idle',
      'voice-dot--hint': state === 'idle' && voice && voice.enabled === true && !voice.discoverabilitySeen,
      'voice-dot--active': _isActiveState(state),
      'voice-dot--muted': state === 'muted',
      'voice-dot--away': !!(voice && voice.awayEventSessionId && voice.awayEventSessionId === sessionId),
    };
  }

  function _cardDotClasses(sessionId, opts) {
    var voice = _voiceStore();
    var state = _stateFor(sessionId, opts);
    return {
      'sc-dot-live': state === 'idle' || state === 'flag_off',
      'sc-dot-dead': state === 'dead',
      'sc-dot--working': state === 'idle' && !!(opts && opts.isWorking),
      'voice-dot--bindable': state === 'idle',
      'voice-dot--hint': state === 'idle' && voice && voice.enabled === true && !voice.discoverabilitySeen,
      'voice-dot--active': _isActiveState(state),
      'voice-dot--muted': state === 'muted',
      'voice-dot--away': !!(voice && voice.awayEventSessionId && voice.awayEventSessionId === sessionId),
    };
  }

  function _showMicGlyph(sessionId, opts) {
    return _isActiveState(_stateFor(sessionId, opts));
  }

  function _showMuteSlash(sessionId, opts) {
    return _stateFor(sessionId, opts) === 'muted';
  }

  function _markDiscoverabilitySeen() {
    var voice = _voiceStore();
    if (voice && typeof voice.markDiscoverabilitySeen === 'function') {
      voice.markDiscoverabilitySeen();
    }
  }

  function _onClick(event, sessionId, opts) {
    var voice = _voiceStore();
    if (!voice) return false;
    var target = event && event.currentTarget;
    if (target && target._voiceHoldTriggered) {
      target._voiceHoldTriggered = false;
      return false;
    }
    var state = _stateFor(sessionId, opts);
    if (state === 'dead' || state === 'flag_off') return false;
    if (_dotInteractionSuppressed(sessionId)) return false;
    _markDiscoverabilitySeen();
    if ((state === 'idle' || state === 'muted') && window.Autonomy &&
        window.Autonomy.voiceCapture &&
        typeof window.Autonomy.voiceCapture.activateFromGesture === 'function') {
      window.Autonomy.voiceCapture.activateFromGesture();
    }
    if (state === 'idle') {
      return voice.requestBind(sessionId, { isLive: true }).ok === true;
    }
    return voice.toggleMic();
  }

  function _clearHold(target) {
    if (!target) return;
    if (target._voiceHoldTimer) {
      clearTimeout(target._voiceHoldTimer);
      target._voiceHoldTimer = null;
    }
  }

  function _onPointerDown(event, sessionId, opts) {
    var target = event && event.currentTarget;
    var voice = _voiceStore();
    if (!target || !voice) return false;
    if (_dotInteractionSuppressed(sessionId)) return false;
    target._voiceHoldTriggered = false;
    _clearHold(target);
    target._voiceHoldTimer = window.setTimeout(function () {
      var state = _stateFor(sessionId, opts);
      if (_isActiveState(state) && voice.boundSessionId === sessionId) {
        voice.endSession();
        target._voiceHoldTriggered = true;
      }
      target._voiceHoldTimer = null;
    }, HOLD_MS);
    return true;
  }

  function _onPointerUp(event) {
    _clearHold(event && event.currentTarget);
    return true;
  }

  function _onPointerCancel(event) {
    _clearHold(event && event.currentTarget);
    return true;
  }

  window.Autonomy = window.Autonomy || {};
  window.Autonomy.voice = window.Autonomy.voice || {};
  window.Autonomy.voice.ui = {
    voiceBindKey: _voiceBindKey,
    sessionDotState: _stateFor,
    dotDisabled: _buttonDisabled,
    dotTitle: _buttonTitle,
    dotAriaLabel: _buttonAriaLabel,
    viewerDotClasses: _viewerDotClasses,
    cardDotClasses: _cardDotClasses,
    showMicGlyph: _showMicGlyph,
    showMuteSlash: _showMuteSlash,
    onClick: _onClick,
    onPointerDown: _onPointerDown,
    onPointerUp: _onPointerUp,
    onPointerCancel: _onPointerCancel,
  };
})();
