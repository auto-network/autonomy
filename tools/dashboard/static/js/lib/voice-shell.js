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
  var CAPTION_PREVIEW_WORDS = 12;
  var CAPTION_RESERVED_HEIGHT = 72;
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
    return !!(
      voice &&
      voice.enabled === true &&
      voice.sheetOpen === true &&
      voice.boundSessionId &&
      _isMobileViewport() &&
      _collapseEnabled()
    );
  }

  function _capsuleVisible() {
    var voice = _voiceStore();
    if (!voice || voice.enabled !== true || !_isMobileViewport()) return false;
    if (!voice.boundSessionId || voice.sheetOpen === true) return false;
    if (_isViewerPage() && !_collapseEnabled()) return false;
    return true;
  }

  function _sheetVisible() {
    var voice = _voiceStore();
    if (!voice || voice.enabled !== true || !_isMobileViewport()) return false;
    if (!voice.boundSessionId || voice.sheetOpen !== true) return false;
    if (_isViewerPage() && !_collapseEnabled()) return false;
    return true;
  }

  function _captionVisible() {
    var voice = _voiceStore();
    if (!voice || voice.enabled !== true || !_isMobileViewport()) return false;
    if (!voice.boundSessionId || voice.sheetOpen === true) return false;
    if (_isViewerPage() && !_collapseEnabled()) return false;
    return true;
  }

  function _rebindVisible() {
    var voice = _voiceStore();
    return !!(voice && voice.enabled === true && voice.pendingRebindTarget);
  }

  function _captionPreview(text) {
    if (typeof text !== 'string') return '';
    var words = text.trim().split(/\s+/).filter(Boolean);
    if (!words.length) return '';
    return words.slice(-CAPTION_PREVIEW_WORDS).join(' ');
  }

  function _sendIcon() {
    return (
      '<svg class="voice-capsule__icon" viewBox="0 0 24 24" aria-hidden="true">' +
      '<path d="M3.478 2.405a.75.75 0 0 0-.926.94l2.432 7.905H13.5a.75.75 0 0 1 0 1.5H4.984l-2.432 7.905a.75.75 0 0 0 .926.94l18.04-8.5a.75.75 0 0 0 0-1.38l-18.04-8.5Z" fill="currentColor"></path>' +
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
    create: function () {
      return {
        viewportWidth: _viewportWidth(),
        viewportHeight: _viewportHeight(),
        capsulePosition: null,
        capsulePressedAction: '',
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

        get captionPlaceholder() {
          return 'Live captions';
        },

        get captionText() {
          var voice = this.voice;
          return _captionPreview((voice && voice.bufferText) || '');
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
          if (!position && !node) return '';
          position = position ? this._clampCapsulePosition(position, node) : this._defaultCapsulePosition(node);
          return 'left:' + position.x + 'px;top:' + position.y + 'px;right:auto;bottom:auto;';
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
          return action === 'type' ? _typeIcon() : _sendIcon();
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

        clearBuffer() {
          if (!this.voice || typeof this.voice.clearBuffer !== 'function') return false;
          this.voice.clearBuffer();
          if (this.$refs && this.$refs.sheetInput) this.$refs.sheetInput.focus();
          return true;
        },

        dismissSheet() {
          if (!this.voice || typeof this.voice.dismissSheet !== 'function') return false;
          return this.voice.dismissSheet();
        },

        async sendBuffer() {
          if (!this.voice || typeof this.voice.sendBuffer !== 'function') return false;
          var ok = await this.voice.sendBuffer();
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
          if (action === 'type' && typeof this.voice.openSheet === 'function') {
            return this.voice.openSheet();
          }
          if (action === 'send' && typeof this.voice.sendBuffer === 'function') {
            return this.voice.sendBuffer();
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
          var maxX = Math.max(8, this.viewportWidth - width - 8);
          var maxY = Math.max(8, this.viewportHeight - height - this._capsuleBottomInset());
          return {
            x: Math.min(maxX, Math.max(8, position.x)),
            y: Math.min(maxY, Math.max(8, position.y)),
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
          };
          var self = this;
          this._capsuleMoveHandler = function (moveEvent) {
            if (!self._capsuleGesture) return;
            var deltaX = moveEvent.clientX - self._capsuleGesture.startX;
            var deltaY = moveEvent.clientY - self._capsuleGesture.startY;
            if (!self._capsuleGesture.dragging &&
                (Math.abs(deltaX) > CAPSULE_DRAG_THRESHOLD || Math.abs(deltaY) > CAPSULE_DRAG_THRESHOLD)) {
              self._capsuleGesture.dragging = true;
              self.capsulePressedAction = '';
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
            self._teardownCapsuleGesture();
            if (gesture.dragging) {
              if (self.voice && typeof self.voice.setCapsulePosition === 'function' && self.capsulePosition) {
                self.voice.setCapsulePosition(self.capsulePosition);
              }
              return;
            }
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
