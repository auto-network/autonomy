// Schema proxy runtime — the JS-side counterpart to the substrate's
// ``autonomy.schema#1`` meta-Setting. Plugins call ``Schema.of(setId)``
// at page-init time and get a typed proxy for the named Settings shape;
// the proxy carries the schema's metadata (fields, required, access
// pattern, key strategy, variants tree) and exposes the generic
// substrate primitives consumers need to read, write, and subscribe.
//
// This file ships the GENERIC runtime only. Pattern-aware convenience
// methods (``.append`` / ``.set`` / ``.upsert``) and per-variant
// convenience methods (``.thumb_yes`` / ``.choice`` / etc.) are added
// by later beads via the extension seam declared at the bottom of
// this module — see ``Schema._registerExtension``. The seam reads the
// schema's metadata (``access_pattern``, ``variants``) and decides
// what to add per proxy.
//
// Bead: auto-2A in the codegen migration sprint (graph://b8a3ae75-e5f).
// Design: graph://865295b3-5cc § "JS Schema proxy".

(function() {
  'use strict';

  // ── Module-level state ──────────────────────────────────────

  var _proxyCache = new Map();   // set_id → Schema (per-page-lifetime)
  var _extensions = [];          // 2B/2C augment the proxy here
  var _fetchOverride = null;     // tests inject a stub fetch

  // ── Schema class ────────────────────────────────────────────

  function Schema(payload) {
    if (!payload || typeof payload !== 'object') {
      throw new TypeError('Schema requires a meta-Setting payload object');
    }
    this.set_id = payload.set_id;
    this.revision = payload.schema_revision;
    this.fields = payload.properties || {};
    this.required = Array.isArray(payload.required) ? payload.required : [];
    this.access_pattern = payload.access_pattern || null;
    this.key_strategy = payload.key_strategy || null;
    this.variants = payload.variants || {};
    // Hold on to the raw payload so extensions (2B/2C) can read whatever
    // they need without us having to expose every field on the prototype.
    this._payload = payload;
  }

  // ── Generic read paths ──────────────────────────────────────

  Schema.prototype.read = async function(key, opts) {
    if (typeof key !== 'string' || !key) {
      throw new TypeError('Schema.read requires a string key');
    }
    opts = opts || {};
    var url = '/api/graph/settings/' + encodeURIComponent(this.set_id)
            + '/' + encodeURIComponent(key);
    if (opts.target_revision) {
      url += '?target_revision=' + encodeURIComponent(opts.target_revision);
    }
    var res = await _fetch(url, { credentials: 'same-origin' });
    if (!res.ok) return null;
    var body = await res.json().catch(function() { return null; });
    return body || null;
  };

  Schema.prototype.all = async function(opts) {
    opts = opts || {};
    var url = '/api/graph/settings/' + encodeURIComponent(this.set_id);
    if (opts.target_revision) {
      url += '?target_revision=' + encodeURIComponent(opts.target_revision);
    }
    var res = await _fetch(url, { credentials: 'same-origin' });
    if (!res.ok) return [];
    var body = await res.json().catch(function() { return {}; });
    return Array.isArray(body.members) ? body.members : [];
  };

  // ── Generic write — escape hatch ─────────────────────────────
  //
  // Pattern-aware sugar (.append for append-only logs, .set for
  // singletons, .upsert for keyed-per-entity) lands in 2B; per-variant
  // dispatch (.thumb_yes / .choice / etc.) lands in 2C. Both use this
  // primitive under the hood.

  Schema.prototype.write = async function(args) {
    if (!args || typeof args !== 'object') {
      throw new TypeError('Schema.write requires {key, payload}');
    }
    if (typeof args.key !== 'string' || !args.key) {
      throw new TypeError('Schema.write requires a string key');
    }
    var res = await _fetch('/api/graph/setting', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        set_id: this.set_id,
        schema_revision: this.revision,
        key: args.key,
        payload: args.payload,
      }),
      credentials: 'same-origin',
    });
    if (!res.ok) {
      var err = await res.json().catch(function() { return {}; });
      throw new Error('Schema.write failed: '
                      + (err && err.error ? err.error : ('status ' + res.status)));
    }
    return res.json().catch(function() { return null; });
  };

  // ── Subscription ────────────────────────────────────────────
  //
  // Routes through the existing ``window.dashboardEvents.onSettingChanged``
  // helper from auto-5mz65 / auto-obo63. In environments without
  // events.js (e.g. unit tests outside the browser shell) the call
  // returns a no-op unsubscriber so callers can write
  // ``const unsub = proxy.onChange(cb); ... unsub();`` unconditionally.

  Schema.prototype.onChange = function(callback) {
    if (typeof callback !== 'function') return function() {};
    var helper = (typeof window !== 'undefined'
                  && window.dashboardEvents
                  && window.dashboardEvents.onSettingChanged);
    if (typeof helper !== 'function') return function() {};
    return helper(this.set_id, callback);
  };

  // ── Schema.of — factory + cache ──────────────────────────────

  async function of(setId, opts) {
    if (typeof setId !== 'string' || !setId) {
      throw new TypeError('Schema.of requires a string set_id');
    }
    opts = opts || {};
    if (!opts.force) {
      var cached = _proxyCache.get(setId);
      if (cached) return cached;
    }
    var revision = (typeof opts.revision === 'number') ? opts.revision : 1;
    var payload = await _fetchSchemaPayload(setId, revision);
    var proxy = new Schema(payload);
    // Extension seam — 2B and 2C augment the proxy based on
    // access_pattern + variants. Failures inside extensions are
    // logged but do not break the generic surface.
    for (var i = 0; i < _extensions.length; i++) {
      try {
        _extensions[i](proxy, payload);
      } catch (err) {
        if (typeof console !== 'undefined' && console.warn) {
          console.warn('[Schema] extension failed for ' + setId + ':', err);
        }
      }
    }
    _proxyCache.set(setId, proxy);
    return proxy;
  }

  async function _fetchSchemaPayload(setId, revision) {
    var key = setId + '#' + revision;
    var url = '/api/graph/settings/'
            + encodeURIComponent('autonomy.schema')
            + '/'
            + encodeURIComponent(key);
    var res = await _fetch(url, { credentials: 'same-origin' });
    if (!res.ok) {
      throw new Error('Schema.of: failed to fetch payload for '
                      + setId + ' (status ' + res.status + ')');
    }
    var body = await res.json().catch(function() { return null; });
    if (!body || !body.payload || typeof body.payload !== 'object') {
      throw new Error('Schema.of: empty or malformed payload for ' + setId);
    }
    return body.payload;
  }

  // ── Extension seam ──────────────────────────────────────────
  //
  // ``_registerExtension(fn)`` registers a function called once per
  // proxy at construction time with ``(proxy, payload)``. 2B uses this
  // to attach pattern-aware methods (.append / .set / .upsert) keyed
  // off ``proxy.access_pattern``. 2C uses this to attach per-variant
  // methods derived from ``proxy.variants``.
  //
  // Extensions are additive — multiple registrations stack, and the
  // order in the registry is the order applied. Tests call
  // ``_clearExtensions`` between cases to keep isolation.

  function _registerExtension(fn) {
    if (typeof fn === 'function') _extensions.push(fn);
  }

  function _clearExtensions() {
    _extensions.length = 0;
  }

  // ── Internal helpers ────────────────────────────────────────

  function _fetch(path, opts) {
    if (_fetchOverride) return _fetchOverride(path, opts);
    if (typeof window !== 'undefined'
        && window.Autonomy
        && typeof window.Autonomy.fetch === 'function') {
      return window.Autonomy.fetch(path, opts);
    }
    if (typeof fetch === 'function') return fetch(path, opts);
    return Promise.reject(new Error('Schema runtime: no fetch available'));
  }

  // ── Test seams ──────────────────────────────────────────────

  function _setFetchOverride(fn) { _fetchOverride = fn; }
  function _clearFetchOverride() { _fetchOverride = null; }
  function _clearCache() { _proxyCache.clear(); }

  // ── Dual export ─────────────────────────────────────────────

  var SchemaNS = {
    of: of,
    Schema: Schema,
    _registerExtension: _registerExtension,
    _clearExtensions: _clearExtensions,
    _setFetchOverride: _setFetchOverride,
    _clearFetchOverride: _clearFetchOverride,
    _clearCache: _clearCache,
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = SchemaNS;
  } else if (typeof window !== 'undefined') {
    window.Schema = SchemaNS;
  }
})();
