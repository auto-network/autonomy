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
    // Only a NON-ZERO reading is cacheable. env(safe-area-inset-top) reads 0 until
    // iOS resolves the notch inset (after the standalone/fullscreen viewport is
    // established), so a premature 0 must NOT get locked into the cache — that left
    // the capsule top-clamp at minY=8 and let it be dragged under the notch
    // (intermittent, depending on first-probe timing). Re-probe until it resolves.
    if (_cachedSafeTop) return _cachedSafeTop;
    var v = 0;
    try {
      if (typeof document !== 'undefined' && document.body) {
        var probe = document.createElement('div');
        probe.style.cssText = 'position:fixed;top:0;left:0;width:0;height:env(safe-area-inset-top,0px);visibility:hidden;pointer-events:none;';
        document.body.appendChild(probe);
        v = probe.getBoundingClientRect().height || 0;
        if (probe.parentNode) probe.parentNode.removeChild(probe);
      }
    } catch (_e) { v = 0; }
    if (v > 0) _cachedSafeTop = v;
    return v;
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
    if (voice.surfaceClaim && voice.surfaceClaim.controls === 'plugin') return false;
    if (!voice.boundSessionId || voice.sheetOpen === true) return false;
    return true;
  }

  function _sheetVisible() {
    var voice = _voiceStore();
    if (!voice || voice.enabled !== true) return false;
    if (voice.surfaceClaim && voice.surfaceClaim.controls === 'plugin') return false;
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
    if (voice.surfaceClaim && voice.surfaceClaim.caption === 'plugin') return false;
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

  function _speakerIcon(active) {
    return (
      '<svg class="voice-capsule__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<polygon points="11 5 6 9 3 9 3 15 6 15 11 19 11 5"></polygon>' +
      '<path d="M15 9.25a4 4 0 0 1 0 5.5"></path>' +
      (active ? '<path d="M18 6.5a8 8 0 0 1 0 11"></path>' : '') +
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

  function _syncViewerOutboxCapture() {
    var voice = _voiceStore();
    if (!voice) return false;
    var bound = voice.boundSessionId;
    if (typeof window === 'undefined' ||
        typeof window.getSessionStore !== 'function') return false;
    if (!bound || !_viewerComposerActive()) return false;
    var s = window.getSessionStore(bound);
    if (!s) return false;
    // Voiceover questions are private to the read-only meta layer. Never mirror
    // them into the coding session's capturing outbox tile; switching modes
    // also removes a capture preview created while Session was selected.
    if (voice.voiceoverActive === true) {
      if (s.outbox && s.outbox.source === 'voice' && s.outbox.state === 'capturing') {
        s.outbox = null;
        return true;
      }
      return false;
    }
    var text = typeof voice.bufferText === 'string' ? voice.bufferText : '';
    var trimmed = text.trim();
    if (s.outbox && (typeof s.outbox.text !== 'string' ||
                     !s.outbox.text.trim())) {
      s.outbox = null;
    }
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
        return true;
      }
      if (s.outbox.source === 'voice' && s.outbox.state === 'capturing') {
        s.outbox.text = text;
        return true;
      }
      return false;
    }
    if (s.outbox && s.outbox.source === 'voice' &&
        s.outbox.state === 'capturing') {
      s.outbox = null;
      return true;
    }
    return false;
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

  window.Autonomy = window.Autonomy || {};
  window.Autonomy.voice = window.Autonomy.voice || {};
  window.Autonomy.voice.shell = {
    hideInlineComposer: _hideInlineComposer,
    previewWords: _previewWords,
    syncViewerOutboxCapture: _syncViewerOutboxCapture,
    create: function () {
      return {
        viewportWidth: _viewportWidth(),
        viewportHeight: _viewportHeight(),
        capsulePosition: null,
        capsulePressedAction: '',
        capsulePttActive: false,
        sheetHeightPx: null,
        sheetDragging: false,
        keyboardVisible: false,
        _restingViewportHeight: 0,
        _sheetWasOpen: false,
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
          this._restingViewportHeight = Math.max(
            (typeof window !== 'undefined' && window.innerHeight) || 0,
            _viewportHeight()
          );
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
            // A live feature-flag change removes Voiceover immediately and
            // returns the capsule to its default Session destination.
            Alpine.effect(function () {
              var st = _voiceStore();
              if (!st) return;
              var _voiceoverEnabled = st.voiceoverEnabled;
              if (!_voiceoverEnabled && st.deliveryMode === 'voiceover' &&
                  typeof st.setDeliveryMode === 'function') {
                st.setDeliveryMode('session');
              }
            });
            Alpine.effect(function () {
              document.body.classList.toggle('voice-caption-active', !!self.showCaption);
            });
            // Start every newly opened editor at the beginning of its draft.
            // After that, follow live dictation only while the textarea is not
            // focused; native typing owns caret/scroll placement while editing.
            Alpine.effect(function () {
              var st = _voiceStore();
              if (!st) return;
              var _buf = st.bufferText;  // track the transcript for reactivity
              var sheetOpen = self.showSheet;
              if (!sheetOpen) {
                self._sheetWasOpen = false;
                return;
              }
              var justOpened = !self._sheetWasOpen;
              self._sheetWasOpen = true;
              if (typeof requestAnimationFrame !== 'function') return;
              requestAnimationFrame(function () {
                var ta = self.$refs && self.$refs.sheetInput;
                if (justOpened && ta) {
                  ta.scrollTop = 0;
                  return;
                }
                // WebKit can paint the caret outside a fixed textarea when JS
                // changes scrollTop while that textarea owns focus. Native
                // typing already keeps its caret visible; only follow dictated
                // text when the editor is not actively being edited.
                if (ta && document.activeElement !== ta) {
                  ta.scrollTop = ta.scrollHeight;
                }
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
              var _bound = voice.boundSessionId;      // track for reactivity
              var _text = voice.bufferText;           // track for reactivity
              var _viewed = voice.viewedSessionId;    // track viewer composer changes
              var _delivery = voice.deliveryMode;     // keep Voiceover out of session outbox
              _syncViewerOutboxCapture();
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
          this._setKeyboardModal(false);
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

        get voiceoverMode() {
          return !!(this.voice && this.voice.voiceoverActive === true);
        },

        get voiceoverEnabled() {
          return !!(this.voice && this.voice.voiceoverEnabled === true);
        },

        get voiceoverBusy() {
          return !!(this.voice && this.voice.voiceoverBusy);
        },

        get voiceoverReply() {
          return (this.voice && this.voice.voiceoverReply) || '';
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
          return '';
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

        get effectiveSheetMode() {
          if (this.keyboardVisible) return 'full';
          if (this.sheetHeightPx != null) return 'custom';
          return this.sheetMode;
        },

        get sheetStyle() {
          if (this.keyboardVisible) {
            return {
              top: '0',
              bottom: 'auto',
              height: Math.round(this.viewportHeight) + 'px',
              transition: 'none',
            };
          }
          if (this.sheetHeightPx == null) return {};
          return {
            height: Math.round(this.sheetHeightPx) + 'px',
            top: 'auto',
            bottom: '0',
            transition: this.sheetDragging ? 'none' : 'height 180ms ease',
          };
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
          if (action === 'voiceover') return _speakerIcon(this.voiceoverMode);
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
          var previousViewportHeight = this.viewportHeight;
          this.viewportWidth = _viewportWidth();
          this.viewportHeight = _viewportHeight();
          var keyboardWasVisible = this.keyboardVisible;
          var inputFocused = !!(
            typeof document !== 'undefined' &&
            document.activeElement === (this.$refs && this.$refs.sheetInput)
          );
          // A resize without editor focus is browser chrome or orientation,
          // not the software keyboard. Make that geometry the new baseline so
          // opening the editor in landscape cannot be mistaken for a keyboard
          // that was already present.
          if (!inputFocused) this._restingViewportHeight = this.viewportHeight;
          this.keyboardVisible = !!(
            inputFocused &&
            this._restingViewportHeight - this.viewportHeight >= 100
          );
          if (this.keyboardVisible && (
            !keyboardWasVisible ||
            previousViewportHeight !== this.viewportHeight
          )) this._scrollSheetToTop();
          if (!this.capsulePosition || !this.$refs || !this.$refs.capsule) return;
          var clamped = this._clampCapsulePosition(this.capsulePosition, this.$refs.capsule);
          if (clamped.x === this.capsulePosition.x && clamped.y === this.capsulePosition.y) return;
          this.capsulePosition = clamped;
          if (this.voice && typeof this.voice.setCapsulePosition === 'function') {
            this.voice.setCapsulePosition(clamped);
          }
        },

        refreshKeyboardLayout() {
          this.refreshViewport();
        },

        _setKeyboardModal(active) {
          if (typeof document === 'undefined') return;
          if (document.documentElement && document.documentElement.classList) {
            document.documentElement.classList.toggle('voice-keyboard-modal', !!active);
          }
          if (document.body && document.body.classList) {
            document.body.classList.toggle('voice-keyboard-modal', !!active);
          }
        },

        onSheetInputFocus() {
          // Lock the document before iOS begins its automatic focus pan. The
          // textarea is the sole scroll surface while the keyboard is visible.
          this._setKeyboardModal(true);
          this.refreshKeyboardLayout();
        },

        _scrollSheetToTop() {
          var self = this;
          var reset = function () {
            var ta = self.$refs && self.$refs.sheetInput;
            if (ta) ta.scrollTop = 0;
          };
          reset();
          if (typeof requestAnimationFrame === 'function') requestAnimationFrame(reset);
        },

        onSheetInputBlur() {
          var self = this;
          setTimeout(function () {
            var stillFocused = !!(
              typeof document !== 'undefined' &&
              document.activeElement === (self.$refs && self.$refs.sheetInput)
            );
            if (!stillFocused) self._setKeyboardModal(false);
            self.refreshViewport();
          }, 80);
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
          this.voice.clearBuffer('clear');
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
          _resetVoiceCaptureEpoch('clear');
          // Do NOT focus the editor — focusing pops the iOS keyboard, and the
          // operator wants Clear to just empty the buffer, not start typing.
          return true;
        },

        dismissSheet() {
          if (!this.voice || typeof this.voice.dismissSheet !== 'function') return false;
          var dismissed = this.voice.dismissSheet();
          if (dismissed) {
            this.sheetHeightPx = null;
            this.sheetDragging = false;
            this.keyboardVisible = false;
          }
          return dismissed;
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

        _commitBuffer() {
          var voice = this.voice;
          if (!voice) return false;
          if (voice.voiceoverActive === true) {
            if (typeof voice.askVoiceover !== 'function') return false;
            return voice.askVoiceover();
          }
          if (typeof voice.sendBuffer !== 'function') return false;
          return voice.sendBuffer();
        },

        async sendBuffer() {
          if (!this.voice) return false;
          var ok = await this._commitBuffer();
          if (!ok && this.$refs && this.$refs.sheetInput) this.$refs.sheetInput.focus();
          return ok;
        },

        setVoiceDelivery(mode) {
          if (!this.voice || typeof this.voice.setDeliveryMode !== 'function') return false;
          return this.voice.setDeliveryMode(mode);
        },

        toggleVoiceover() {
          if (!this.voice || this.voice.voiceoverEnabled !== true ||
              typeof this.voice.setDeliveryMode !== 'function') return false;
          return this.voice.setDeliveryMode(this.voiceoverMode ? 'session' : 'voiceover');
        },

        replayVoiceover() {
          if (!this.voice || typeof this.voice.speakVoiceover !== 'function') return false;
          return this.voice.speakVoiceover(this.voice.voiceoverReply || '');
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

        switchSheetToCurrent() {
          var voice = this.voice;
          if (!voice || !voice.viewedSessionId ||
              voice.viewedSessionId === voice.boundSessionId ||
              typeof voice.bindSession !== 'function') return false;
          var priorMode = voice.micMode;
          voice.bindSession(voice.viewedSessionId);
          // Opening the keyboard mutes capture. Preserve that state across the
          // explicit target switch; if the operator used the sheet mic button
          // to resume capture, bindSession's listening state is already right.
          if (priorMode === 'muted' && typeof voice.setMicMode === 'function') {
            voice.setMicMode('muted');
          }
          return true;
        },

        onSheetInput() {
          if (!this.voice) return;
          this.voice.sheetError = '';
          if (typeof this.voice.publishBuffer === 'function') {
            this.voice.publishBuffer('edit');
          }
        },

        onCapsuleKey(action) {
          this.runCapsuleAction(action);
          return true;
        },

        runCapsuleAction(action) {
          if (!this.voice) return false;
          if (action === 'mic') {
            var conn = this.voice.connState;
            var health = this.voice.effectiveState || '';
            if (health === 'enable_required' || conn === 'disconnected' || conn === 'reconnecting') {
              // One user gesture reauthorizes iOS capture + Wake Lock and
              // replaces stale browser/server state.
              if (typeof window !== 'undefined' && window.Autonomy && window.Autonomy.voiceCapture &&
                  typeof window.Autonomy.voiceCapture.enableFromGesture === 'function') {
                window.Autonomy.voiceCapture.enableFromGesture();
              } else if (typeof window !== 'undefined' && window.Autonomy && window.Autonomy.voiceCapture &&
                         typeof window.Autonomy.voiceCapture.retryReconnect === 'function') {
                window.Autonomy.voiceCapture.retryReconnect();
              }
              return true;
            }
            if (this.voice.micMode === 'muted' && typeof window !== 'undefined' &&
                window.Autonomy && window.Autonomy.voiceCapture &&
                typeof window.Autonomy.voiceCapture.activateFromGesture === 'function') {
              window.Autonomy.voiceCapture.activateFromGesture();
            }
            if (typeof this.voice.toggleMic === 'function') return this.voice.toggleMic();  // tap = mute ⇄ unmute
            return false;
          }
          if (action === 'type' && typeof this.voice.openSheet === 'function') {
            this.sheetHeightPx = null;
            this.sheetDragging = false;
            return this.voice.openSheet();
          }
          if (action === 'voiceover') return this.toggleVoiceover();
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
              // Fire the clear haptic HERE — this pointerup is a synchronous user
              // gesture, which iOS requires for the switch haptic; the clear ran
              // from the hold TIMER (no gesture), so a tap there was ignored (#35).
              if (gesture.action === 'type' && typeof window !== 'undefined' &&
                  window.Autonomy && typeof window.Autonomy.haptic === 'function') {
                window.Autonomy.haptic();
              }
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
          var sheet = this.$refs && this.$refs.sheet;
          if (!handle || !sheet || !this.voice || this.keyboardVisible) return false;
          this._teardownSheetGesture();
          if (event && typeof event.preventDefault === 'function') event.preventDefault();
          if (event && event.pointerId != null && typeof handle.setPointerCapture === 'function') {
            try { handle.setPointerCapture(event.pointerId); } catch (_e) {}
          }
          var viewportHeight = _viewportHeight();
          var minHeight = Math.max(160, Math.min(360, viewportHeight - 8));
          var maxHeight = Math.max(minHeight, viewportHeight - 16);
          this._sheetGesture = {
            pointerId: event.pointerId,
            handle: handle,
            startY: event.clientY,
            startHeight: sheet.getBoundingClientRect().height,
            minHeight: minHeight,
            maxHeight: maxHeight,
          };
          this.sheetHeightPx = this._sheetGesture.startHeight;
          this.sheetDragging = true;
          var self = this;
          this._sheetMoveHandler = function (moveEvent) {
            var gesture = self._sheetGesture;
            if (!gesture || (gesture.pointerId != null && moveEvent.pointerId !== gesture.pointerId)) return;
            if (typeof moveEvent.preventDefault === 'function') moveEvent.preventDefault();
            var next = gesture.startHeight + gesture.startY - moveEvent.clientY;
            self.sheetHeightPx = Math.max(gesture.minHeight, Math.min(gesture.maxHeight, next));
          };
          this._sheetUpHandler = function (upEvent) {
            var gesture = self._sheetGesture;
            if (!gesture || (gesture.pointerId != null && upEvent.pointerId !== gesture.pointerId)) return;
            if (typeof upEvent.preventDefault === 'function') upEvent.preventDefault();
            if (upEvent.type !== 'pointercancel') {
              var finalHeight = gesture.startHeight + gesture.startY - upEvent.clientY;
              self.sheetHeightPx = Math.max(gesture.minHeight, Math.min(gesture.maxHeight, finalHeight));
            }
            try { gesture.handle.releasePointerCapture(gesture.pointerId); } catch (_e) {}
            var height = self.sheetHeightPx;
            self.sheetDragging = false;
            self._teardownSheetGesture();
            if (height >= gesture.maxHeight - 24) {
              self.sheetHeightPx = null;
              self._scrollSheetToTop();
              if (self.voice && typeof self.voice.expandSheet === 'function') self.voice.expandSheet();
            } else if (height <= gesture.minHeight + 24) {
              self.sheetHeightPx = null;
              if (self.voice && typeof self.voice.collapseSheet === 'function') self.voice.collapseSheet();
            }
          };
          window.addEventListener('pointermove', this._sheetMoveHandler, { passive: false });
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
