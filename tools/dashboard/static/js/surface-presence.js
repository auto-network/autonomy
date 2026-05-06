// Surface Presence — JS substrate library helper (substrate.B).
//
// Companion to ``schemas.js``. Substrate.A shipped the Python +
// schema half (see ``tools/graph/surface.py``); this file is the
// matching JS surface so plugin pages can opt into multiplayer
// presence with one wrapping call:
//
//   function settingsNexus() {
//     return Schema.alpine(
//       Presence.alpine(
//         { surfaceId: 'settings-nexus',
//           onPing: (p) => this.scrollToTile(p.position_value) },
//         { tab: 'primary',
//           async tapThumb(tile, kind) { /* ... */ } }),
//       { schemas: { Scene: 'dashboard.nexus.scene',
//                    Tile: 'dashboard.nexus.tile' } });
//   }
//
// After ``init()`` runs:
//
//   this.participants               — reactive array of presence rows
//                                     for the current surface
//   this.amHere                     — bool, true once the operator's
//                                     row has been written
//   this.pingAgent(id, pos, msg)    — writes a SurfacePing directed
//                                     at the explicit participant id
//   this.acknowledgePing(ping_id)   — restamps the operator's row
//                                     with last_ping_id
//   this.setPresenceState(state, p) — switch ``present`` / ``working``
//                                     and update position/intent
//
// View-side participant color helper (lives on Presence):
//
//   Presence.participantColor(participantId)         — string ("hsl(...)")
//
// ``participantColor`` lives view-side per pitfall
// ``graph://73af2694-562``. Mirrors the Python helper in
// ``tools/graph/surface.py`` so plugin Python and the JS view layer
// agree on a participant's hue.
//
// Singleton operator-activity helpers (substrate.B ships stubs;
// substrate.D will make them real) live on a separate
// ``OperatorActivity`` global. They read the
// ``dashboard.operator.activity`` singleton row — "did the operator
// type anywhere recently?" — and have nothing to do with multiplayer
// surface presence. Each is async so the eventual real signature
// (server read) is the contract consumers code against today:
//
//   await OperatorActivity.isIdle({minutes: 30})    — bool   (stub: false)
//   await OperatorActivity.lastUserInput()           — Date|null (stub: null)
//   await OperatorActivity.activeWithin({hours: 1})  — bool   (stub: true)
//   await OperatorActivity.inputsLastHour()          — number (stub: 0)
//
// Bead: substrate.B (auto-7t98r). Signpost: graph://dff97eec-c59.

(function() {
  'use strict';

  // ── Set-id constants (kept in sync with tools/graph/surface.py) ─

  var SURFACE_PRESENCE_SET_ID = 'dashboard.surface.presence';
  var SURFACE_PING_SET_ID     = 'dashboard.surface.ping';
  var SCHEMA_REVISION         = 1;
  var DEFAULT_HEARTBEAT_MS    = 10000;

  // ── Schema runtime resolution ───────────────────────────────
  //
  // We do NOT ``require`` schemas.js at module top: in the browser
  // the file is loaded as a sibling <script> that exports
  // ``window.Schema``, and node tests load both via plain ``require``
  // and stash one onto ``globalThis.Schema``. Resolving lazily lets
  // both worlds work without a bundler.

  function _schema() {
    if (typeof globalThis !== 'undefined' && globalThis.Schema) {
      return globalThis.Schema;
    }
    if (typeof window !== 'undefined' && window.Schema) {
      return window.Schema;
    }
    return null;
  }

  function _nowIso() {
    // Drop sub-second precision so written timestamps match the
    // Python helper's format (``%Y-%m-%dT%H:%M:%SZ``).
    return new Date().toISOString().replace(/\.\d+Z$/, 'Z');
  }

  function _payloadOf(member) {
    if (!member) return null;
    return member.payload || member;
  }

  function _belongsToSurface(surfaceId) {
    return function(m) {
      var p = _payloadOf(m);
      return p && p.surface_id === surfaceId;
    };
  }

  // ── participantColor — deterministic HSL hash ───────────────
  //
  // Mirrors the Python helper in ``tools/graph/surface.py``:
  //
  //   sum(ord(c) * 31 ** i for i, c in enumerate(participant_id)) & 0xFFFFFFFF
  //   hsl(<h % 360> 70% 60%)
  //
  // We compute the same value modulo 2^32 using ``Math.imul`` so the
  // intermediate ``31**i`` factor stays in a 32-bit signed slot
  // instead of losing precision in JS doubles. Modular arithmetic is
  // associative, so collapsing to 32-bit per step gives the same
  // final ``& 0xFFFFFFFF`` value Python computes.

  function participantColor(participantId) {
    if (typeof participantId !== 'string') {
      participantId = String(participantId == null ? '' : participantId);
    }
    var h = 0;       // 32-bit signed accumulator
    var coef = 1;    // 31^i mod 2^32, signed
    for (var i = 0; i < participantId.length; i++) {
      h = (h + Math.imul(participantId.charCodeAt(i), coef)) | 0;
      coef = Math.imul(coef, 31);
    }
    var unsigned = h >>> 0;
    return 'hsl(' + (unsigned % 360) + ' 70% 60%)';
  }

  // ── Singleton operator-activity stubs (substrate.D real) ───
  //
  // These read the ``dashboard.operator.activity`` singleton row —
  // "did the operator type anywhere recently?" — so they take no
  // participant id. Async to match the eventual real signatures
  // (server reads). Stubs match the Python defaults so consumers that
  // gate work on ``!isIdle(...)`` keep behaving as they do today,
  // then improve automatically when substrate.D ships.

  async function isIdle(/* opts */) {
    return false;
  }

  async function lastUserInput() {
    return null;
  }

  async function activeWithin(/* opts */) {
    return true;
  }

  async function inputsLastHour() {
    return 0;
  }

  // ── Operator identity resolution ────────────────────────────
  //
  // Caller-provided ``opts.participantId`` always wins. Otherwise we
  // sniff ``window.Autonomy.operatorId`` (substrate.D may inject
  // this; today no one does, so the operator presence row is
  // skipped — consumers of pingAgent must therefore pass an
  // explicit participantId).

  function _resolveOperatorId(opts) {
    if (typeof opts.participantId === 'string' && opts.participantId) {
      return opts.participantId;
    }
    if (typeof window !== 'undefined' && window.Autonomy
        && typeof window.Autonomy.operatorId === 'string'
        && window.Autonomy.operatorId) {
      return window.Autonomy.operatorId;
    }
    return null;
  }

  function _resolveOperatorLabel(opts, participantId) {
    if (typeof opts.participantLabel === 'string' && opts.participantLabel) {
      return opts.participantLabel;
    }
    if (typeof window !== 'undefined' && window.Autonomy
        && typeof window.Autonomy.operatorLabel === 'string'
        && window.Autonomy.operatorLabel) {
      return window.Autonomy.operatorLabel;
    }
    return participantId || '';
  }

  // ── Presence.alpine — wrap an Alpine factory state ──────────

  function alpine(opts, state) {
    if (!opts || typeof opts !== 'object') {
      throw new TypeError('Presence.alpine requires an opts object');
    }
    if (typeof opts.surfaceId !== 'string' || !opts.surfaceId) {
      throw new TypeError('Presence.alpine: opts.surfaceId is required');
    }
    if (!state || typeof state !== 'object') {
      throw new TypeError('Presence.alpine requires a state object');
    }

    var surfaceId           = opts.surfaceId;
    var onPing              = (typeof opts.onPing === 'function')
                              ? opts.onPing : null;
    var onParticipantChange = (typeof opts.onParticipantChange === 'function')
                              ? opts.onParticipantChange : null;
    var heartbeatMs         = (typeof opts.heartbeatMs === 'number')
                              ? opts.heartbeatMs : DEFAULT_HEARTBEAT_MS;
    var acceptsPings        = (opts.acceptsPings === false) ? false : true;

    // Reactive surface state — Alpine will pick these up because
    // Alpine reads property descriptors at component-construction
    // time and proxies them. Default arrays/strings are fine.
    if (!Array.isArray(state.participants)) state.participants = [];
    state.amHere = false;
    state._presenceSurfaceId = surfaceId;
    state._presenceParticipantId = null;
    state._presenceParticipantLabel = '';
    state._presenceParticipantKind = opts.participantKind || 'operator';
    state._presenceClaimedState = 'present';
    state._presencePosition = { kind: 'none', value: '' };
    state._presenceIntent = '';
    state._presenceLastPingId = '';
    state._presenceUnsubs = [];
    state._presenceHeartbeat = null;
    state._presencePresenceProxy = null;
    state._presencePingProxy = null;

    // ── compose init/destroy ─────────────────────────────────

    var origInit = (typeof state.init === 'function') ? state.init : null;
    var origDestroy = (typeof state.destroy === 'function') ? state.destroy : null;

    state.init = async function() {
      var Schema = _schema();
      if (Schema && typeof Schema.of === 'function') {
        try {
          await this._presenceAttachSchemas();
          await this._presenceLoadParticipants();
          this._presenceSubscribe();
          var participantId = _resolveOperatorId(opts);
          if (participantId) {
            this._presenceParticipantId = participantId;
            this._presenceParticipantLabel = _resolveOperatorLabel(opts, participantId);
            await this._presenceWriteRow('present');
            this.amHere = true;
            this._presenceStartHeartbeat();
          }
        } catch (err) {
          if (typeof console !== 'undefined' && console.warn) {
            console.warn('[Presence.alpine] init failed for surface='
                         + surfaceId + ':', err);
          }
        }
      } else if (typeof console !== 'undefined' && console.warn) {
        console.warn('[Presence.alpine] Schema runtime unavailable; '
                     + 'presence is degraded for surface=' + surfaceId);
      }
      if (origInit) await origInit.call(this);
    };

    state.destroy = function() {
      this._presenceStopHeartbeat();
      var unsubs = this._presenceUnsubs || [];
      for (var i = 0; i < unsubs.length; i++) {
        try { unsubs[i](); }
        catch (err) {
          if (typeof console !== 'undefined' && console.warn) {
            console.warn('[Presence.alpine] unsub failed:', err);
          }
        }
      }
      this._presenceUnsubs = [];
      if (origDestroy) {
        try { origDestroy.call(this); }
        catch (err) {
          if (typeof console !== 'undefined' && console.warn) {
            console.warn('[Presence.alpine] origDestroy raised:', err);
          }
        }
      }
    };

    // ── proxy attachment + initial read ──────────────────────

    state._presenceAttachSchemas = async function() {
      var Schema = _schema();
      this._presencePresenceProxy = await Schema.of(
        SURFACE_PRESENCE_SET_ID, { revision: SCHEMA_REVISION },
      );
      this._presencePingProxy = await Schema.of(
        SURFACE_PING_SET_ID, { revision: SCHEMA_REVISION },
      );
    };

    state._presenceLoadParticipants = async function() {
      if (!this._presencePresenceProxy) return;
      var members = await this._presencePresenceProxy.all();
      this.participants = (members || [])
        .filter(_belongsToSurface(surfaceId))
        .map(_payloadOf);
    };

    state._presenceSubscribe = function() {
      // Schema.onChange always returns a callable (no-op if events.js
      // missing); the typeof guards we used to wrap each call with
      // were dead. We still push into _presenceUnsubs because this
      // module pre-dates Schema.alpine's auto-dispose seam — a future
      // migration to Schema.alpine drops the array entirely.
      var self = this;
      if (this._presencePresenceProxy) {
        this._presenceUnsubs.push(
          this._presencePresenceProxy.onChange(function(evt) {
            self._presenceHandlePresenceChange(evt);
          }),
        );
      }
      if (this._presencePingProxy) {
        this._presenceUnsubs.push(
          this._presencePingProxy.onChange(function(evt) {
            self._presenceHandlePingEvent(evt);
          }),
        );
      }
    };

    state._presenceHandlePresenceChange = function(evt) {
      // SSE setting.changed event arrived. Refresh the participants
      // array. v1 re-fetches the full surface slice; a future
      // optimisation can splice a single row from evt.payload.
      var self = this;
      this._presenceLoadParticipants().then(function() {
        if (onParticipantChange) {
          try { onParticipantChange.call(self, self.participants, evt); }
          catch (err) {
            if (typeof console !== 'undefined' && console.warn) {
              console.warn('[Presence.alpine] onParticipantChange raised:', err);
            }
          }
        }
      }).catch(function(err) {
        if (typeof console !== 'undefined' && console.warn) {
          console.warn('[Presence.alpine] refresh failed:', err);
        }
      });
    };

    state._presenceHandlePingEvent = function(evt) {
      if (!onPing || !evt) return;
      var payload = _payloadOf(evt);
      if (!payload || payload.surface_id !== surfaceId) return;
      // Only fire onPing for pings actually targeted at us.
      if (this._presenceParticipantId
          && payload.to_participant_id !== this._presenceParticipantId) {
        return;
      }
      try { onPing.call(this, payload, evt); }
      catch (err) {
        if (typeof console !== 'undefined' && console.warn) {
          console.warn('[Presence.alpine] onPing raised:', err);
        }
      }
    };

    // ── operator's own row + heartbeat ───────────────────────

    state._presenceBuildPayload = function() {
      return {
        surface_id: surfaceId,
        participant_kind: this._presenceParticipantKind || 'operator',
        participant_id: this._presenceParticipantId || '',
        participant_label: this._presenceParticipantLabel
                           || this._presenceParticipantId || '',
        accepts_pings: acceptsPings,
        state: this._presenceClaimedState || 'present',
        position_kind: (this._presencePosition && this._presencePosition.kind) || 'none',
        position_value: (this._presencePosition && this._presencePosition.value) || '',
        intent: this._presenceIntent || '',
        heartbeat_at: _nowIso(),
        last_ping_id: this._presenceLastPingId || '',
      };
    };

    state._presenceWriteRow = async function(claimedState) {
      if (!this._presencePresenceProxy || !this._presenceParticipantId) {
        return null;
      }
      this._presenceClaimedState = claimedState || this._presenceClaimedState;
      var payload = this._presenceBuildPayload();
      var key = surfaceId + ':' + this._presenceParticipantId;
      // SurfacePresence is @keyed_per_entity — the pattern extension
      // attaches .upsert deterministically. Tests that detach
      // extensions for isolation re-register them with
      // _registerExtension, so the runtime branch was dead.
      return this._presencePresenceProxy.upsert(key, payload);
    };

    state._presenceStartHeartbeat = function() {
      var self = this;
      if (heartbeatMs <= 0 || this._presenceHeartbeat) return;
      this._presenceHeartbeat = setInterval(function() {
        self._presenceWriteRow(self._presenceClaimedState).catch(function(err) {
          if (typeof console !== 'undefined' && console.warn) {
            console.warn('[Presence.alpine] heartbeat write failed:', err);
          }
        });
      }, heartbeatMs);
    };

    state._presenceStopHeartbeat = function() {
      if (this._presenceHeartbeat) {
        clearInterval(this._presenceHeartbeat);
        this._presenceHeartbeat = null;
      }
    };

    // ── Public methods (the bead's API surface) ──────────────

    state.setPresenceState = function(claimedState, posOrOpts) {
      var p = posOrOpts || {};
      if (typeof p.kind === 'string') this._presencePosition.kind = p.kind;
      if (typeof p.value === 'string') this._presencePosition.value = p.value;
      if (typeof p.intent === 'string') this._presenceIntent = p.intent;
      return this._presenceWriteRow(claimedState || this._presenceClaimedState);
    };

    state.pingAgent = async function(targetId, position, message) {
      if (typeof targetId !== 'string' || !targetId) {
        throw new TypeError('pingAgent: targetId is required');
      }
      if (!this._presencePingProxy) {
        throw new Error('Presence.alpine: ping proxy not attached '
                        + '(init() has not run, or schemas unavailable)');
      }
      if (!this._presenceParticipantId) {
        throw new Error('Presence.alpine: cannot ping without an '
                        + 'operator participant id (pass opts.participantId)');
      }
      var pos = position || {};
      var posKind = (typeof pos.kind === 'string' && pos.kind) ? pos.kind : 'tile';
      var posValue = (pos.value != null) ? String(pos.value) : '';
      var payload = {
        surface_id: surfaceId,
        from_participant_id: this._presenceParticipantId,
        to_participant_id: targetId,
        position_kind: posKind,
        position_value: posValue,
        message: message || '',
        sent_at: _nowIso(),
      };
      // SurfacePing is @append_only_log — the pattern extension
      // attaches .append, which generates the UUID key for us.
      return this._presencePingProxy.append(payload);
    };

    state.acknowledgePing = async function(pingId) {
      if (typeof pingId !== 'string' || !pingId) return null;
      this._presenceLastPingId = pingId;
      return this._presenceWriteRow(this._presenceClaimedState);
    };

    return state;
  }

  // ── Dual export ─────────────────────────────────────────────

  var PresenceNS = {
    alpine: alpine,
    participantColor: participantColor,
    SURFACE_PRESENCE_SET_ID: SURFACE_PRESENCE_SET_ID,
    SURFACE_PING_SET_ID: SURFACE_PING_SET_ID,
    SCHEMA_REVISION: SCHEMA_REVISION,
  };

  var OperatorActivityNS = {
    isIdle: isIdle,
    lastUserInput: lastUserInput,
    activeWithin: activeWithin,
    inputsLastHour: inputsLastHour,
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = PresenceNS;
    module.exports.OperatorActivity = OperatorActivityNS;
  } else if (typeof window !== 'undefined') {
    window.Presence = PresenceNS;
    window.OperatorActivity = OperatorActivityNS;
  }
})();
