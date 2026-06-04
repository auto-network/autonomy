/**
 * Shell-mounted voice compose surfaces.
 *
 * Owns the mobile floating capsule, passive caption gutter, rebind
 * confirmation pill, and the compose sheet wired to Alpine.store('voice').
 * Desktop handoff lands in S8.
 */
(function () {
  var VOICE_COLLAPSE_FLAG = 'voice.responsive_collapse_enabled';
  var CAPSULE_DRAG_THRESHOLD = 6;
  var CAPSULE_BOTTOM_GAP = 12;
  var CAPSULE_HOLD_MS = 350;    // mic press-and-hold → push-to-talk threshold
  var CAPSULE_CLEAR_MS = 600;   // keyboard press-and-hold → clear (Send drains over this)
  var CAPTION_PREVIEW_WORDS = 12;
  var CAPTION_RESERVED_HEIGHT = 72;
  var SHEET_BACKDROP_GUARD_MS = 300;
  var SHEET_EXPAND_THRESHOLD = -42;
  var SHEET_COLLAPSE_THRESHOLD = 56;
  var SHEET_DISMISS_THRESHOLD = 72;

  function _voiceStore() {
    try {
      if (typeof Alpine === 'undefined' || typeof Alpine.store !== 'function') return null;
      return Alpine.store('voice') || null;
    } catch (_err) {
      return null;
    }
  }

  function _flagsStore() {
    try {
      if (typeof Alpine === 'undefined' || typeof Alpine.store !== 'function') return null;
      return Alpine.store('flags') || null;
    } catch (_err) {
      return null;
    }
  }

  function _isMobileViewport() {
    var viewport = (typeof window !== 'undefined' && window.visualViewport) || null;
    var width = (viewport && viewport.width) || (typeof window !== 'undefined' && window.innerWidth) || 0;
    return width < 768;
  }

  // env(safe-area-inset-top) in px — the iOS notch/status-bar band. Measured
  // once via a probe element and cached. The capsule clamp keeps the capsule
  // below this so it can't be dragged under the status bar where it becomes
  // ungrabbable.
  var _cachedSafeTop = null;
  function _safeAreaInsetTop() {
    if (_cachedSafeTop !== null) return _cachedSafeTop;
    _cachedSafeTop = 0;
    try {
      if (typeof document !== 'undefined' && document.body) {
        var probe = document.createElement('div');
        probe.style.cssText = 'position:fixed;top:0;left:0;width:0;height:env(safe-area-inset-top,0px);visibility:hidden;pointer-events:none;';
        document.body.appendChild(probe);
        _cachedSafeTop = probe.getBoundingClientRect().height || 0;
        if (probe.parentNode) probe.parentNode.removeChild(probe);
      }
    } catch (_e) { _cachedSafeTop = 0; }
    return _cachedSafeTop;
  }

  function _collapseEnabled() {
    var flags = _flagsStore();
    if (!flags || typeof flags.get !== 'function') return false;
    try {
      return flags.get(VOICE_COLLAPSE_FLAG) === true;
    } catch (_err) {
      return false;
    }
  }

  function _isViewerPage() {
    if (typeof document === 'undefined' || typeof document.querySelector !== 'function') return false;
    return !!document.querySelector('.session-viewer[data-mode="page"]');
  }

  function _hideInlineComposer() {
    var voice = _voiceStore();
    // Once voice is bound the floating capsule/caption/sheet ARE the composer —
    // the inline keyboard text box drops so the surface is voice-first. This is
    // NO LONGER gated on viewport: the operator wants desktop to behave exactly
    // like mobile (capsule + dictation tile, one-click send), not the old
    // desktop dictate->import->send slog. Bound + enabled is the gate.
    return !!(
      voice &&
      voice.enabled === true &&
      voice.boundSessionId
    );
  }

  function _capsuleVisible() {
    var voice = _voiceStore();
    // Viewport-agnostic: show the capsule on desktop too (operator wants the
    // mobile voice-first flow everywhere).
    if (!voice || voice.enabled !== true) return false;
    if (!voice.boundSessionId || voice.sheetOpen === true) return false;
    return true;
  }

  function _sheetVisible() {
    var voice = _voiceStore();
    if (!voice || voice.enabled !== true) return false;
    if (!voice.boundSessionId || voice.sheetOpen !== true) return false;
    return true;
  }

  // True when the durability session's pinned outbox tile is actually mounted
  // in the viewer for the bound session (both body flags set + session match,
  // owned/set by session-viewer.js). When true the tile owns the bottom
  // surface, so the floating caption gutter hands off and hides there. The
  // sv-outbox-tile-present flag is false until that tile component ships, so
  // this is INERT (no caption gap, no regression) until the handoff target
  // truly exists in the DOM. Send-path branching keys on the composer-active
  // flag alone, not this.
  // True when the durability session's viewer composer surface is active for
  // the bound session (the body flag set by session-viewer.js + session match).
  // This is the gate for FEEDING the outbox tile (capturing) and for the
  // send-flip — NOT tile-present, because the tile-present flag is driven by
  // s.outbox, so requiring it before we set s.outbox would be circular.
  function _viewerComposerActive() {
    if (typeof document === 'undefined' || !document.body) return false;
    var voice = _voiceStore();
    var bound = voice && voice.boundSessionId;
    if (!bound) return false;
    try {
      var body = document.body;
      return !!(
        body.classList &&
        body.classList.contains('sv-viewer-composer-active') &&
        body.dataset &&
        body.dataset.svComposerSession === bound
      );
    } catch (_err) {
      return false;
    }
  }

  // True only once the outbox tile is actually MOUNTED (composer active AND
  // sv-outbox-tile-present). Used for caption suppression so the gutter only
  // hands off when the tile truly exists — inert until then.
  function _viewerTileActive() {
    if (!_viewerComposerActive()) return false;
    try {
      return !!(document.body.classList &&
        document.body.classList.contains('sv-outbox-tile-present'));
    } catch (_err) {
      return false;
    }
  }

  function _captionVisible() {
    var voice = _voiceStore();
    if (!voice || voice.enabled !== true) return false;
    if (!voice.boundSessionId || voice.sheetOpen === true) return false;
    // Inside the viewer, once the pinned outbox tile is mounted it owns the
    // bottom surface — hand the live text to it and hide this floating gutter.
    if (_viewerTileActive()) return false;
    return true;
  }

  function _rebindVisible() {
    var voice = _voiceStore();
    return !!(voice && voice.enabled === true && voice.pendingRebindTarget);
  }

  function _previewWords(text, limit) {
    if (typeof text !== 'string') return '';
    var size = Number.isFinite(limit) && limit > 0 ? Math.floor(limit) : CAPTION_PREVIEW_WORDS;
    var words = text.trim().split(/\s+/).filter(Boolean);
    if (!words.length) return '';
    return words.slice(-size).join(' ');
  }

  function _sendIcon() {
    // Diagonal paper plane (up-and-to-the-right), per operator preference.
    return (
      '<svg class="voice-capsule__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<line x1="22" y1="2" x2="11" y2="13"></line>' +
      '<polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>' +
      '</svg>'
    );
  }

  function _micOnIcon() {
    return (
      '<svg class="voice-capsule__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<rect x="9" y="2" width="6" height="12" rx="3"></rect>' +
      '<path d="M5 10v1a7 7 0 0 0 14 0v-1"></path>' +
      '<line x1="12" y1="19" x2="12" y2="22"></line>' +
      '</svg>'
    );
  }

  function _micSlashIcon() {
    return (
      '<svg class="voice-capsule__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 .4 1.5"></path>' +
      '<path d="M15 9.3V5a3 3 0 0 0-5.1-2.1"></path>' +
      '<path d="M19 10v1a7 7 0 0 1-.6 2.8"></path>' +
      '<path d="M5 10v1a7 7 0 0 0 11 5.7"></path>' +
      '<line x1="12" y1="19" x2="12" y2="22"></line>' +
      '<line x1="3" y1="3" x2="21" y2="21"></line>' +
      '</svg>'
    );
  }

  function _typeIcon() {
    return (
      '<svg class="voice-capsule__icon" viewBox="0 0 24 24" aria-hidden="true">' +
      '<rect x="2.5" y="5.25" width="19" height="13.5" rx="3.1" fill="none" stroke="currentColor" stroke-width="1.9"></rect>' +
      '<rect x="5.2" y="8.2" width="3.2" height="2.35" rx="1.15" fill="currentColor"></rect>' +
      '<rect x="10.4" y="8.2" width="3.2" height="2.35" rx="1.15" fill="currentColor"></rect>' +
      '<rect x="15.6" y="8.2" width="3.2" height="2.35" rx="1.15" fill="currentColor"></rect>' +
      '<rect x="6.2" y="13.3" width="11.6" height="2.5" rx="1.25" fill="currentColor"></rect>' +
      '</svg>'
    );
  }

  function _viewportWidth() {
    var viewport = (typeof window !== 'undefined' && window.visualViewport) || null;
    return (viewport && viewport.width) || (typeof window !== 'undefined' && window.innerWidth) || 0;
  }

  function _viewportHeight() {
    var viewport = (typeof window !== 'undefined' && window.visualViewport) || null;
    return (viewport && viewport.height) || (typeof window !== 'undefined' && window.innerHeight) || 0;
  }

  function _sessionsStore() {
    try {
      if (typeof Alpine === 'undefined' || typeof Alpine.store !== 'function') return null;
      return Alpine.store('sessions') || null;
    } catch (_err) {
      return null;
    }
  }

  window.Autonomy = window.Autonomy || {};
  window.Autonomy.voice = window.Autonomy.voice || {};
  window.Autonomy.voice.shell = {
    hideInlineComposer: _hideInlineComposer,
    previewWords: _previewWords,
    create: function () {
      return {
        viewportWidth: _viewportWidth(),
        viewportHeight: _viewportHeight(),
        capsulePosition: null,
        capsulePressedAction: '',
        capsulePttActive: false,
        _capsuleHoldTimer: null,
        _capsuleGesture: null,
        _capsuleMoveHandler: null,
        _capsuleUpHandler: null,
        _sheetGesture: null,
        _sheetMoveHandler: null,
        _sheetUpHandler: null,
        _resizeHandler: null,

        init() {
          var voice = _voiceStore();
          this.capsulePosition = voice && voice.capsulePosition ? {
            x: voice.capsulePosition.x,
            y: voice.capsulePosition.y,
          } : null;
          this._resizeHandler = this.refreshViewport.bind(this);
          window.addEventListener('resize', this._resizeHandler);
          if (window.visualViewport && typeof window.visualViewport.addEventListener === 'function') {
            window.visualViewport.addEventListener('resize', this._resizeHandler);
          }
          // Reflect caption visibility as a <body> class so scrollable page
          // content (.sv-entries) can reserve the bottom gutter the fixed
          // caption occupies — otherwise the last entries scroll underneath it.
          var self = this;
          if (typeof Alpine !== 'undefined' && typeof Alpine.effect === 'function') {
            Alpine.effect(function () {
              document.body.classList.toggle('voice-caption-active', !!self.showCaption);
            });
            // Keep the latest dictated text visible in the open sheet: as the
            // transcript grows, scroll the editor to the bottom. Deferred via
            // rAF so x-model has written the new value before we scroll.
            Alpine.effect(function () {
              var st = _voiceStore();
              if (!st) return;
              var _buf = st.bufferText;  // track the transcript for reactivity
              if (!self.showSheet) return;
              if (typeof requestAnimationFrame !== 'function') return;
              requestAnimationFrame(function () {
                var ta = self.$refs && self.$refs.sheetInput;
                if (ta) ta.scrollTop = ta.scrollHeight;
              });
            });
            // Feed live dictation into the durability outbox tile (auto-0530's
            // pinned tile) when inside the viewer for the bound session — the
            // CAPTURING state upstream of SENDING. bufferText streams into
            // s.outbox.text; the tile renders + sets sv-outbox-tile-present,
            // which suppresses our floating caption gutter. Cleared when the
            // buffer empties or we leave the viewer composer surface.
            Alpine.effect(function () {
              var voice = _voiceStore();
              if (!voice) return;
              var bound = voice.boundSessionId;
              var text = voice.bufferText;  // track for reactivity
              if (typeof window === 'undefined' ||
                  typeof window.getSessionStore !== 'function') return;
              if (!bound || !_viewerComposerActive()) return;
              var s = window.getSessionStore(bound);
              if (!s) return;
              var trimmed = (text || '').trim();
              if (trimmed) {
                if (!s.outbox) {
                  s.outbox = {
                    localId: (typeof window.newOutboxId === 'function')
                      ? window.newOutboxId() : ('ob_' + bound),
                    state: 'capturing',
                    source: 'voice',
                    text: text,
                    ts: (typeof Date !== 'undefined' && Date.now) ? Date.now() : 0,
                  };
                } else if (s.outbox.source === 'voice' &&
                           s.outbox.state === 'capturing') {
                  s.outbox.text = text;
                }
              } else if (s.outbox && s.outbox.source === 'voice' &&
                         s.outbox.state === 'capturing') {
                s.outbox = null;  // dictation cleared → collapse the tile
              }
            });
          }
        },

        destroy() {
          if (this._resizeHandler) {
            window.removeEventListener('resize', this._resizeHandler);
            if (window.visualViewport && typeof window.visualViewport.removeEventListener === 'function') {
              window.visualViewport.removeEventListener('resize', this._resizeHandler);
            }
          }
          this._teardownCapsuleGesture();
          this._teardownSheetGesture();
        },

        get voice() {
          return _voiceStore();
        },

        get showCapsule() {
          return _capsuleVisible();
        },

        get showCaption() {
          return _captionVisible();
        },

        get showSheet() {
          return _sheetVisible();
        },

        get showRebindConfirm() {
          return _rebindVisible();
        },

        get hasBufferText() {
          var voice = this.voice;
          return !!(voice && typeof voice.bufferText === 'string' && voice.bufferText.trim().length > 0);
        },

        // Drives the sheet Send button: enabled with transcript text OR a
        // landed attachment, disabled while any upload is in flight.
        get canSendVoice() {
          return !!(this.voice && this.voice.canSend);
        },

        // Live word count of the capture buffer — the operator's "it's working"
        // proof while speaking (operator feedback c6e038a4: visible evidence
        // capture is live). Rendered as a small badge on the capsule Send action.
        get bufferWordCount() {
          var voice = this.voice;
          var t = (voice && typeof voice.bufferText === 'string' ? voice.bufferText : '').trim();
          if (!t) return 0;
          return t.split(/\s+/).filter(Boolean).length;
        },

        // Cross-session for the KEYBOARD sheet: input is bound to a different
        // session than the one being viewed, so a Send goes elsewhere. Reactive
        // off store props (boundSessionId + viewedSessionId the viewer publishes).
        get sheetCrossSession() {
          var v = this.voice;
          return !!(v && v.boundSessionId && v.viewedSessionId &&
                    v.boundSessionId !== v.viewedSessionId);
        },
        get sheetCrossTargetTitle() {
          var v = this.voice;
          if (!v || !v.boundSessionId) return '';
          try {
            var sessions = (typeof Alpine !== 'undefined' && Alpine.store) ? Alpine.store('sessions') : null;
            var s = sessions && sessions[v.boundSessionId];
            if (s && s.label) return s.label;
          } catch (_e) {}
          return v.boundSessionId;
        },

        get sendPulse() {
          var voice = this.voice;
          return !!(voice && voice.micMode === 'vad_paused' && this.hasBufferText);
        },

        get captionPlaceholder() {
          return 'Live captions';
        },

        get captionText() {
          var voice = this.voice;
          return _previewWords((voice && voice.bufferText) || '', CAPTION_PREVIEW_WORDS);
        },

        get hasCaptionText() {
          return this.captionText.length > 0;
        },

        get sheetMode() {
          var voice = this.voice;
          return (voice && voice.sheetMode) || 'partial';
        },

        get capsuleStyle() {
          var node = this.$refs && this.$refs.capsule;
          var position = this.capsulePosition;
          if (!position && !node) return {};
          position = position ? this._clampCapsulePosition(position, node) : this._defaultCapsulePosition(node);
          // Return an OBJECT, not a style string. A string sets the entire
          // style attribute and clobbers the `display:none` that
          // x-show="showCapsule" writes — which is why the capsule was always
          // display:flex (visible) even unbound. An object is merged per
          // property by Alpine and leaves display (x-show's) untouched.
          return {
            left: position.x + 'px',
            top: position.y + 'px',
            right: 'auto',
            bottom: 'auto',
          };
        },

        get rebindCopy() {
          var voice = this.voice;
          if (!voice || !voice.boundSessionId) return 'End voice on the current session?';
          var sessions = _sessionsStore();
          var row = sessions && sessions[voice.boundSessionId];
          var name = (row && row.label) || voice.boundSessionId;
          return 'End voice on ' + name + '?';
        },

        capsuleIcon(action) {
          if (action === 'type') return _typeIcon();
          if (action === 'mic') {
            var voice = this.voice;
            if (voice && (voice.connState === 'reconnecting' || voice.connState === 'disconnected')) {
              return _micSlashIcon();   // not actually capturing — show it off
            }
            var mode = voice ? voice.micMode : 'idle';
            var open = this.capsulePttActive || mode === 'listening' || mode === 'vad_paused';
            return open ? _micOnIcon() : _micSlashIcon();
          }
          return _sendIcon();
        },

        // Mic button skin: gray (muted) / blue (listening) / orange (push-to-talk
        // while held). PTT is a transient UI flag, not a micMode value.
        get capsuleMicClass() {
          var voice = this.voice;
          var pressed = this.capsulePressedAction === 'mic';
          var conn = voice ? voice.connState : 'ok';
          if (conn === 'reconnecting' || conn === 'disconnected') {
            return {
              'voice-capsule__mic--reconnecting': conn === 'reconnecting',
              'voice-capsule__mic--disconnected': conn === 'disconnected',
              'voice-capsule__action--pressed': pressed,
            };
          }
          var mode = voice ? voice.micMode : 'idle';
          var state = 'muted';
          if (this.capsulePttActive) state = 'ptt';
          else if (mode === 'listening' || mode === 'vad_paused') state = 'listening';
          return {
            'voice-capsule__mic--muted': state === 'muted',
            'voice-capsule__mic--listening': state === 'listening',
            'voice-capsule__mic--ptt': state === 'ptt',
            'voice-capsule__action--pressed': pressed,
          };
        },

        // ── push-to-talk + hold-to-clear drain ───────────────────────
        _beginCapsulePtt() {
          var voice = this.voice;
          if (!voice) return;
          // Hold only does something from muted; from listening it's a no-op.
          if (voice.micMode === 'muted' || voice.micMode === 'idle') {
            this.capsulePttActive = true;
            if (typeof voice.setMicMode === 'function') voice.setMicMode('listening');
          }
        },
        _endCapsulePtt() {
          var voice = this.voice;
          if (this.capsulePttActive && voice && typeof voice.setMicMode === 'function') {
            voice.setMicMode('muted');
          }
          this.capsulePttActive = false;
        },
        _sendFillEl() {
          if (this.$refs && this.$refs.capsuleSendFill) return this.$refs.capsuleSendFill;
          // Fallback: the gesture handlers run as window listeners, where $refs
          // can be out of scope on some Alpine versions — query the DOM directly.
          if (typeof document !== 'undefined' && document.querySelector) {
            return document.querySelector('.voice-capsule__send-fill');
          }
          return null;
        },
        _setSendFill(pct, ms) {
          var el = this._sendFillEl();
          if (!el) return;
          el.style.transition = 'height ' + ms + 'ms linear';
          // force reflow so the transition runs from the current height
          void el.offsetHeight;
          el.style.height = pct + '%';
        },
        _beginCapsuleClear() {
          // The Send fill has fully drained over CAPSULE_CLEAR_MS; now wipe the
          // buffer (which collapses the word-count badge) and refill the Send.
          if (this.voice && typeof this.clearBuffer === 'function') this.clearBuffer();
          var self = this;
          setTimeout(function () { self._setSendFill(100, 280); }, 240);
        },
        _capsuleSendClaimable() {
          // Returns the viewed session id when the Send button is in the violet
          // cross-session state (dictating to a DIFFERENT session than the one
          // being viewed), so a long-press can claim dictation to it. Else ''.
          if (typeof document === 'undefined' || !document.body) return '';
          if (!document.body.classList.contains('sv-cross-session-dictation')) return '';
          return document.body.dataset.svComposerSession || '';
        },
        _beginCapsuleClaim(target) {
          // Long-press on the violet Send → re-bind dictation to the session being
          // viewed. bindSession carries the live buffer over (#23 switch-takes-
          // buffer); viewed === bound now, so the fill drains violet → blue.
          if (!target || !this.voice || typeof this.voice.bindSession !== 'function') return;
          this.voice.bindSession(target);
          var self = this;
          setTimeout(function () { self._setSendFill(100, 280); }, 240);
        },
        _clearCapsuleHold() {
          if (this._capsuleHoldTimer) {
            clearTimeout(this._capsuleHoldTimer);
            this._capsuleHoldTimer = null;
          }
        },

        refreshViewport() {
          this.viewportWidth = _viewportWidth();
          this.viewportHeight = _viewportHeight();
          if (!this.capsulePosition || !this.$refs || !this.$refs.capsule) return;
          var clamped = this._clampCapsulePosition(this.capsulePosition, this.$refs.capsule);
          if (clamped.x === this.capsulePosition.x && clamped.y === this.capsulePosition.y) return;
          this.capsulePosition = clamped;
          if (this.voice && typeof this.voice.setCapsulePosition === 'function') {
            this.voice.setCapsulePosition(clamped);
          }
        },

        openVoiceFilePicker() {
          if (this.$refs && this.$refs.voiceFileInput &&
              typeof this.$refs.voiceFileInput.click === 'function') {
            this.$refs.voiceFileInput.click();
          }
          return true;
        },

        onVoicePickFiles(event) {
          if (!this.voice || typeof this.voice.addAttachmentFiles !== 'function') return false;
          var input = event && event.target;
          if (input && input.files) this.voice.addAttachmentFiles(input.files);
          if (input) input.value = '';   // allow re-picking the same file
          return true;
        },

        removeVoiceAttachment(id) {
          if (!this.voice || typeof this.voice.removeAttachment !== 'function') return false;
          this.voice.removeAttachment(id);
          return true;
        },

        clearBuffer() {
          if (!this.voice || typeof this.voice.clearBuffer !== 'function') return false;
          this.voice.clearBuffer();
          // Collapse the dictation tile too. The reactive capturing effect that
          // normally nulls the outbox gates on _viewerComposerActive(), which can
          // be false when the operator clears from a different view — leaving a
          // stale tile showing the old text (operator-reported). Null the
          // capturing voice outbox directly here so Clear always wipes the tile.
          var bound = this.voice.boundSessionId;
          if (bound && typeof window !== 'undefined' &&
              typeof window.getSessionStore === 'function') {
            var s = window.getSessionStore(bound);
            if (s && s.outbox && s.outbox.source === 'voice' &&
                s.outbox.state === 'capturing') {
              s.outbox = null;
            }
          }
          // Do NOT focus the editor — focusing pops the iOS keyboard, and the
          // operator wants Clear to just empty the buffer, not start typing.
          return true;
        },

        dismissSheet() {
          if (!this.voice || typeof this.voice.dismissSheet !== 'function') return false;
          return this.voice.dismissSheet();
        },

        onSheetBackdrop() {
          // Tap-outside dismiss. Bound to @click (not @pointerdown) so the
          // backdrop stays present through the whole tap — the click targets
          // the backdrop and cannot fall through to the session card beneath
          // (the navigation regression). The open-time guard ignores the
          // trailing synthesized click from the tap that just opened the sheet.
          var voice = this.voice;
          var openedAt = voice && voice.sheetOpenedAt;
          if (openedAt && typeof Date !== 'undefined' && Date.now &&
              (Date.now() - openedAt) < SHEET_BACKDROP_GUARD_MS) {
            return false;
          }
          return this.dismissSheet();
        },

        // Commit the dictation. Inside the viewer with an active outbox tile,
        // hand off to the durability path: set the final text, flip the outbox
        // to 'sending' (auto-0530's watcher owns the POST + reconcile), and
        // clear the voice buffer — NO direct POST (no double-send). The
        // capturing effect gates on state==='capturing', so it won't touch the
        // flipped outbox, and clearing the buffer still drives the WhisperLive
        // cutoff via the existing send-clear effect. Outside the viewer, or
        // with no tile, fall back to the direct send.
        _commitBuffer() {
          var voice = this.voice;
          if (!voice) return false;
          if (_viewerComposerActive() && typeof window !== 'undefined' &&
              typeof window.getSessionStore === 'function' && voice.boundSessionId) {
            var s = window.getSessionStore(voice.boundSessionId);
            if (s && s.outbox && s.outbox.source === 'voice' &&
                s.outbox.state === 'capturing') {
              if (voice.attachmentsPending) {
                voice.sheetError = 'Attachment still uploading…';
                return false;
              }
              var body = typeof voice._buildSendBody === 'function'
                ? voice._buildSendBody()
                : (voice.bufferText || '').trim();
              if (!body) return false;
              s.outbox.text = body;
              s.outbox.state = 'sending';
              if (typeof voice.clearBuffer === 'function') voice.clearBuffer();
              if (typeof voice.clearAttachments === 'function') voice.clearAttachments();
              return true;
            }
          }
          return voice.sendBuffer();
        },

        async sendBuffer() {
          if (!this.voice) return false;
          var ok = await this._commitBuffer();
          if (!ok && this.$refs && this.$refs.sheetInput) this.$refs.sheetInput.focus();
          return ok;
        },

        confirmRebind() {
          if (!this.voice || typeof this.voice.confirmRebind !== 'function') return false;
          return this.voice.confirmRebind();
        },

        cancelRebind() {
          if (!this.voice || typeof this.voice.cancelRebind !== 'function') return false;
          this.voice.cancelRebind();
          return true;
        },

        onSheetInput() {
          if (!this.voice) return;
          this.voice.sheetError = '';
        },

        onCapsuleKey(action) {
          this.runCapsuleAction(action);
          return true;
        },

        runCapsuleAction(action) {
          if (!this.voice) return false;
          if (action === 'mic') {
            var conn = this.voice.connState;
            if (conn === 'disconnected' || conn === 'reconnecting') {
              // Tap the red mic to retry the connection now.
              if (typeof window !== 'undefined' && window.Autonomy && window.Autonomy.voiceCapture &&
                  typeof window.Autonomy.voiceCapture.retryReconnect === 'function') {
                window.Autonomy.voiceCapture.retryReconnect();
              }
              return true;
            }
            if (typeof this.voice.toggleMic === 'function') return this.voice.toggleMic();  // tap = mute ⇄ unmute
            return false;
          }
          if (action === 'type' && typeof this.voice.openSheet === 'function') {
            return this.voice.openSheet();
          }
          if (action === 'send' && typeof this.voice.sendBuffer === 'function') {
            return this._commitBuffer();
          }
          return false;
        },

        _capsuleBottomInset() {
          if (!this.showCaption) return 16;
          return CAPTION_RESERVED_HEIGHT + CAPSULE_BOTTOM_GAP;
        },

        _defaultCapsulePosition(node) {
          var rect = node && typeof node.getBoundingClientRect === 'function'
            ? node.getBoundingClientRect()
            : { width: 114, height: 54 };
          var width = rect.width || 114;
          var height = rect.height || 54;
          var safeBottom = this._capsuleBottomInset();
          var x = Math.max(8, this.viewportWidth - width - 14);
          var y = Math.max(8, this.viewportHeight - height - safeBottom);
          return { x: x, y: y };
        },

        _clampCapsulePosition(position, node) {
          var rect = node && typeof node.getBoundingClientRect === 'function'
            ? node.getBoundingClientRect()
            : { width: 114, height: 54 };
          var width = rect.width || 114;
          var height = rect.height || 54;
          // Keep the capsule below the iOS status bar/notch so it can't be
          // dragged under it and become ungrabbable (operator-reported).
          var minY = Math.max(8, _safeAreaInsetTop() + 8);
          var maxX = Math.max(8, this.viewportWidth - width - 8);
          var maxY = Math.max(minY, this.viewportHeight - height - this._capsuleBottomInset());
          return {
            x: Math.min(maxX, Math.max(8, position.x)),
            y: Math.min(maxY, Math.max(minY, position.y)),
          };
        },

        _currentCapsulePosition(node) {
          if (this.capsulePosition) {
            return this._clampCapsulePosition(this.capsulePosition, node);
          }
          return this._defaultCapsulePosition(node);
        },

        onCapsulePointerDown(event) {
          if (!this.showCapsule || !this.$refs || !this.$refs.capsule) return false;
          if (event && typeof event.preventDefault === 'function') event.preventDefault();
          var node = this.$refs.capsule;
          // Pointer capture is what makes the WHOLE surface reliably draggable.
          // Without it, pointermove is delivered by hit-testing each frame, so
          // the moment the finger crosses another element mid-drag iOS stops
          // sending move events and the drag dies — the "sometimes it works,
          // sometimes it doesn't" bug. Capturing the pointer routes every
          // move/up for THIS finger to the capsule until release, no matter
          // where it travels.
          if (event && event.pointerId != null && typeof node.setPointerCapture === 'function') {
            try { node.setPointerCapture(event.pointerId); } catch (_e) {}
          }
          var actionNode = event && event.target && typeof event.target.closest === 'function'
            ? event.target.closest('[data-voice-action]')
            : null;
          var action = actionNode ? (actionNode.getAttribute('data-voice-action') || '') : '';
          var origin = this._currentCapsulePosition(node);
          this.capsulePosition = origin;
          this.capsulePressedAction = action;
          this._capsuleGesture = {
            action: action,
            startX: event.clientX,
            startY: event.clientY,
            originX: origin.x,
            originY: origin.y,
            dragging: false,
            held: false,
          };
          var self = this;
          // Press-and-hold: mic → push-to-talk; keyboard → clear (the Send fill
          // drains over the hold, then the count poofs as the buffer clears).
          this._clearCapsuleHold();
          // While the mic is red (reconnecting/disconnected) a hold is meaningless
          // — only a tap retries — so don't arm the PTT timer for it.
          var conn = this.voice ? this.voice.connState : 'ok';
          var micHoldable = action === 'mic' && conn !== 'reconnecting' && conn !== 'disconnected';
          // Long-press the violet (cross-session) Send to claim dictation to the
          // session being viewed. Only armed while actually cross-session.
          var sendClaimTarget = action === 'send' ? this._capsuleSendClaimable() : '';
          if (micHoldable || action === 'type' || sendClaimTarget) {
            // Drain the Send fill over the hold as a progress cue (clear AND claim).
            if (action === 'type' || sendClaimTarget) this._setSendFill(0, CAPSULE_CLEAR_MS);
            this._capsuleHoldTimer = setTimeout(function () {
              if (!self._capsuleGesture || self._capsuleGesture.dragging) return;
              self._capsuleGesture.held = true;
              self.capsulePressedAction = '';
              if (action === 'mic') self._beginCapsulePtt();
              else if (action === 'type') self._beginCapsuleClear();
              else if (action === 'send') self._beginCapsuleClaim(sendClaimTarget);
            }, action === 'mic' ? CAPSULE_HOLD_MS : CAPSULE_CLEAR_MS);
          }
          this._capsuleMoveHandler = function (moveEvent) {
            if (!self._capsuleGesture) return;
            var deltaX = moveEvent.clientX - self._capsuleGesture.startX;
            var deltaY = moveEvent.clientY - self._capsuleGesture.startY;
            if (!self._capsuleGesture.dragging &&
                (Math.abs(deltaX) > CAPSULE_DRAG_THRESHOLD || Math.abs(deltaY) > CAPSULE_DRAG_THRESHOLD)) {
              self._capsuleGesture.dragging = true;
              self.capsulePressedAction = '';
              self._clearCapsuleHold();
              if (self._capsuleGesture.action === 'type' || self._capsuleGesture.action === 'send') self._setSendFill(100, 160);
            }
            if (!self._capsuleGesture.dragging) return;
            self.capsulePosition = self._clampCapsulePosition({
              x: self._capsuleGesture.originX + deltaX,
              y: self._capsuleGesture.originY + deltaY,
            }, node);
          };
          this._capsuleUpHandler = function () {
            if (!self._capsuleGesture) return;
            var gesture = self._capsuleGesture;
            self._clearCapsuleHold();
            self._teardownCapsuleGesture();
            // PTT release ALWAYS wins, even if the capsule was dragged mid-hold.
            // Without this, a held-then-dragged gesture took the dragging branch
            // below and returned before ending PTT — leaving the mic stuck orange
            // after the finger lifted. End it first, then handle the drag/position.
            if (gesture.held && gesture.action === 'mic') self._endCapsulePtt();
            if (gesture.dragging) {
              if (self.voice && typeof self.voice.setCapsulePosition === 'function' && self.capsulePosition) {
                self.voice.setCapsulePosition(self.capsulePosition);
              }
              self.capsulePressedAction = '';
              return;
            }
            if (gesture.held) {
              // The hold already fired: PTT was released above (mic) or the
              // keyboard hold-to-clear ran. No tap action either way.
              self.capsulePressedAction = '';
              return;
            }
            // Tap (released before the hold threshold).
            if (gesture.action === 'type' || gesture.action === 'send') self._setSendFill(100, 160);  // undo any partial drain
            var actionName = gesture.action;
            self.capsulePressedAction = '';
            if (actionName) self.runCapsuleAction(actionName);
          };
          window.addEventListener('pointermove', this._capsuleMoveHandler);
          window.addEventListener('pointerup', this._capsuleUpHandler);
          window.addEventListener('pointercancel', this._capsuleUpHandler);
          return true;
        },

        _teardownCapsuleGesture() {
          if (this._capsuleMoveHandler) {
            window.removeEventListener('pointermove', this._capsuleMoveHandler);
            window.removeEventListener('pointerup', this._capsuleUpHandler);
            window.removeEventListener('pointercancel', this._capsuleUpHandler);
          }
          this._capsuleGesture = null;
          this._capsuleMoveHandler = null;
          this._capsuleUpHandler = null;
        },

        startSheetGesture(event) {
          var handle = event && event.target && typeof event.target.closest === 'function'
            ? event.target.closest('.voice-sheet__handle')
            : null;
          if (!handle || !this.voice) return false;
          // preventDefault + pointer capture so the swipe-up reliably produces
          // a pointerup with the right clientY even as the finger travels up
          // the screen — without these iOS treats it as a scroll and the
          // expand never fires (same fix as the capsule drag).
          if (event && typeof event.preventDefault === 'function') event.preventDefault();
          if (event && event.pointerId != null && typeof handle.setPointerCapture === 'function') {
            try { handle.setPointerCapture(event.pointerId); } catch (_e) {}
          }
          var initialMode = this.sheetMode;
          this._sheetGesture = {
            startY: event.clientY,
            initialMode: initialMode,
          };
          var self = this;
          this._sheetMoveHandler = function () {};
          this._sheetUpHandler = function (upEvent) {
            if (!self._sheetGesture) return;
            var deltaY = upEvent.clientY - self._sheetGesture.startY;
            var mode = self._sheetGesture.initialMode;
            self._teardownSheetGesture();
            if (deltaY <= SHEET_EXPAND_THRESHOLD) {
              if (self.voice && typeof self.voice.expandSheet === 'function') self.voice.expandSheet();
              return;
            }
            if (mode === 'full' && deltaY >= SHEET_COLLAPSE_THRESHOLD) {
              if (self.voice && typeof self.voice.collapseSheet === 'function') self.voice.collapseSheet();
              return;
            }
            if (mode === 'partial' && deltaY >= SHEET_DISMISS_THRESHOLD) {
              if (self.voice && typeof self.voice.dismissSheet === 'function') self.voice.dismissSheet();
            }
          };
          window.addEventListener('pointermove', this._sheetMoveHandler);
          window.addEventListener('pointerup', this._sheetUpHandler);
          window.addEventListener('pointercancel', this._sheetUpHandler);
          return true;
        },

        _teardownSheetGesture() {
          if (this._sheetMoveHandler) {
            window.removeEventListener('pointermove', this._sheetMoveHandler);
            window.removeEventListener('pointerup', this._sheetUpHandler);
            window.removeEventListener('pointercancel', this._sheetUpHandler);
          }
          this._sheetGesture = null;
          this._sheetMoveHandler = null;
          this._sheetUpHandler = null;
        },
      };
    },
  };

  document.addEventListener('alpine:init', function () {
    Alpine.data('voiceShell', function () {
      return window.Autonomy.voice.shell.create();
    });
  });
})();
