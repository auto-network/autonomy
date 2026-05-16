/**
 * Operator-controlled feature flags — Alpine.store('flags') + window.Autonomy.flags.
 *
 * Reads `dashboard.feature_flags#1` via Schema.of('dashboard.feature_flags').
 * Live-invalidates on remote changes via Schema.prototype.onChange(),
 * which routes through the existing window.dashboardEvents.onSettingChanged
 * channel and re-dispatches as the page-local CustomEvent
 * 'autonomy:flag-changed' for any Alpine consumer to react to.
 *
 * No client-side cache beyond what Schema.of itself caches (proxy per
 * page lifetime). The store maintains a synchronous {name: bool} cache
 * populated on initial _load() so consumers can read flags reactively
 * inside Alpine x-show / :class bindings without awaiting on every render.
 *
 * Async API (use during first-load, not in render hot paths):
 *   await window.Autonomy.flags.get('voice.client_enabled')   // bool
 *   await window.Autonomy.flags.all()                         // {name: payload}
 *
 * Alpine sync API (use in templates after store has loaded):
 *   Alpine.store('flags').get('voice.client_enabled')         // bool
 *   Alpine.store('flags').isLoaded                            // bool
 *
 * Missing rows always read False. Schema validation rejects non-bool
 * `enabled` at write time; this module also defensively gates on
 * `=== true` so a corrupted row can't silently turn a feature on.
 *
 * Spec: graph://40dd9d7a-23a (S0).
 */
(function() {
  'use strict';

  var FEATURE_FLAGS_SET_ID = 'dashboard.feature_flags';
  var FEATURE_FLAGS_REVISION = 1;
  var CHANGE_EVENT = 'autonomy:flag-changed';

  var _proxyPromise = null;
  var _onChangeUnsub = null;

  function _getProxy() {
    if (_proxyPromise) return _proxyPromise;
    if (typeof window === 'undefined' || !window.Schema || typeof window.Schema.of !== 'function') {
      return Promise.reject(new Error('feature-flags: Schema.of is unavailable'));
    }
    _proxyPromise = window.Schema.of(FEATURE_FLAGS_SET_ID, { revision: FEATURE_FLAGS_REVISION });
    return _proxyPromise;
  }

  function _bindOnChange(proxy) {
    if (_onChangeUnsub || typeof proxy.onChange !== 'function') return;
    _onChangeUnsub = proxy.onChange(function(evt) {
      if (typeof window === 'undefined' || typeof window.dispatchEvent !== 'function') return;
      var detail = evt || {};
      try {
        window.dispatchEvent(new CustomEvent(CHANGE_EVENT, { detail: detail }));
      } catch (err) {
        if (typeof console !== 'undefined' && console.warn) {
          console.warn('[feature-flags] dispatch failed:', err);
        }
      }
    });
  }

  async function _getFlag(name) {
    var proxy = await _getProxy();
    _bindOnChange(proxy);
    var row = await proxy.read(name);
    if (!row || !row.payload) return false;
    return row.payload.enabled === true;
  }

  async function _allFlags() {
    var proxy = await _getProxy();
    _bindOnChange(proxy);
    var rows = await proxy.all();
    var out = {};
    if (!Array.isArray(rows)) return out;
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      if (r && r.key) out[r.key] = r.payload || {};
    }
    return out;
  }

  // ── window.Autonomy.flags facade ─────────────────────────────

  if (typeof window !== 'undefined') {
    window.Autonomy = window.Autonomy || {};
    window.Autonomy.flags = {
      get: _getFlag,
      all: _allFlags,
      // Test-only seam — clears the cached proxy promise so a fresh
      // Schema.of call lands the next time. Production code does not
      // need this; Schema.of already caches across the page lifetime.
      _resetForTests: function() {
        _proxyPromise = null;
        if (_onChangeUnsub) { try { _onChangeUnsub(); } catch (_) {} }
        _onChangeUnsub = null;
      },
    };
  }

  // ── Alpine.store('flags') reactive sync cache ───────────────

  function _installAlpineStore() {
    if (typeof window === 'undefined' || !window.Alpine
        || typeof window.Alpine.store !== 'function') return;
    window.Alpine.store('flags', {
      _values: {},
      isLoaded: false,

      get: function(name) {
        return this._values[name] === true;
      },

      _load: async function() {
        var snap;
        try {
          snap = await _allFlags();
        } catch (err) {
          if (typeof console !== 'undefined' && console.warn) {
            console.warn('[feature-flags] store load failed:', err);
          }
          this.isLoaded = true;   // mark loaded even on failure so consumers don't stall
          return;
        }
        var next = {};
        for (var name in snap) {
          if (Object.prototype.hasOwnProperty.call(snap, name)) {
            next[name] = snap[name] && snap[name].enabled === true;
          }
        }
        this._values = next;
        this.isLoaded = true;
      },

      _refresh: function() {
        return this._load();
      },
    });
    // Kick the initial load asynchronously; consumers see isLoaded=false
    // until it resolves. Subsequent change events re-_load() the cache.
    window.Alpine.store('flags')._load();
    // Live-update listener: when any flag changes server-side, re-_load().
    window.addEventListener(CHANGE_EVENT, function() {
      var store = window.Alpine.store('flags');
      if (store && typeof store._refresh === 'function') store._refresh();
    });
  }

  if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
    document.addEventListener('alpine:init', _installAlpineStore);
  }
})();
