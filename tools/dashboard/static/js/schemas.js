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
    var revision = (typeof opts.revision === 'number') ? opts.revision : 1;
    // Cache by ``(set_id, revision)`` composite — multiple revisions
    // of the same set_id describe different shapes (different field
    // sets, different defaults, different validation rules), so they
    // MUST not alias to the same proxy. ``force: true`` refetches the
    // same composite key.
    var cacheKey = setId + '#' + revision;
    if (!opts.force) {
      var cached = _proxyCache.get(cacheKey);
      if (cached) return cached;
    }
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
    _proxyCache.set(cacheKey, proxy);
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

  // ── Pattern-aware extension (bead 2B) ───────────────────────
  //
  // Reads ``proxy.access_pattern`` and ``proxy.key_strategy`` (set by
  // the @decorators in auto-ruuyc) and attaches the matching
  // convenience method:
  //
  //   append_only_log    → proxy.append(payload)        (UUID key)
  //   singleton          → proxy.set(payload)           (fixed key)
  //   keyed_per_entity   → proxy.upsert(key, payload)   (caller key)
  //
  // Each method routes through the generic ``proxy.write``. Undecorated
  // schemas get no convenience methods — codegen consumers fall back
  // to ``proxy.write({key, payload})`` which is the substrate's
  // documented escape hatch.
  //
  // Registered at module load below so every ``Schema.of`` call gets
  // pattern methods automatically. Tests that need an isolated proxy
  // can call ``_clearExtensions()`` and then re-register
  // ``_patternExtension`` if they want this layer back.

  function _generateAppendKey(strategy) {
    if (strategy === 'uuid_v4' || strategy === null || strategy === undefined) {
      return _uuidV4();
    }
    // Future strategies (snowflake_id, ulid, ...) plug in here. For
    // now anything else is unrecognized; warn loudly and fall back
    // to UUID so the write still succeeds.
    if (typeof console !== 'undefined' && console.warn) {
      console.warn('[Schema] unknown append_only_log key_strategy '
                   + JSON.stringify(strategy) + '; using uuid_v4');
    }
    return _uuidV4();
  }

  function _resolveSingletonKey(strategy) {
    if (typeof strategy === 'string' && strategy.indexOf('fixed:') === 0) {
      return strategy.slice('fixed:'.length);
    }
    if (typeof console !== 'undefined' && console.warn) {
      console.warn('[Schema] unrecognized singleton key_strategy '
                   + JSON.stringify(strategy) + '; using "default"');
    }
    return 'default';
  }

  function _uuidV4() {
    if (typeof globalThis !== 'undefined'
        && globalThis.crypto
        && typeof globalThis.crypto.randomUUID === 'function') {
      return globalThis.crypto.randomUUID();
    }
    // RFC4122-shaped fallback for environments without crypto.randomUUID.
    // Random source is Math.random — sufficient for substrate keys but
    // not cryptographically strong; modern node/browsers will use the
    // primary path above.
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
      var r = Math.random() * 16 | 0;
      var v = (c === 'x') ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  function _patternExtension(proxy /*, payload */) {
    var pattern = proxy.access_pattern;
    if (!pattern) return;
    if (pattern === 'append_only_log') {
      proxy.append = function(payload) {
        return proxy.write({
          key: _generateAppendKey(proxy.key_strategy),
          payload: payload,
        });
      };
      return;
    }
    if (pattern === 'singleton') {
      var key = _resolveSingletonKey(proxy.key_strategy);
      proxy.set = function(payload) {
        return proxy.write({ key: key, payload: payload });
      };
      return;
    }
    if (pattern === 'keyed_per_entity') {
      proxy.upsert = function(key, payload) {
        if (typeof key !== 'string' || !key) {
          throw new TypeError('Schema.upsert requires a string key');
        }
        return proxy.write({ key: key, payload: payload });
      };
      return;
    }
    // Unknown access_pattern — log but don't crash. Future patterns
    // can extend this switch; the generic .write surface still works.
    if (typeof console !== 'undefined' && console.warn) {
      console.warn('[Schema] unknown access_pattern '
                   + JSON.stringify(pattern) + ' on '
                   + proxy.set_id + '; no convenience method attached');
    }
  }

  // Wire the pattern extension as a default. Tests that need a fully
  // generic proxy can ``_clearExtensions()`` before invoking
  // ``Schema.of``; tests that need it back can re-register
  // ``Schema._patternExtension`` after clearing.
  _registerExtension(_patternExtension);

  // ── Variant-aware extension (bead 2C) ───────────────────────
  //
  // Walks ``payload.variants`` (the recursive tree emitted by 1D)
  // and attaches per-variant convenience methods to the proxy. Each
  // variant slug becomes a method that auto-injects ``{kind: slug}``
  // into the payload and routes through the pattern method that 2B
  // attached (``.append`` / ``.set`` / ``.upsert``) or — when no
  // pattern is present — through the generic ``.write``.
  //
  // Nested namespaces (the ``SourceControl.review.read`` shape from
  // capability contracts) become plain objects on the proxy with
  // recursive variant methods. The leaf slug is what gets stamped
  // into ``kind``; each level's slug names the path segment but the
  // wire ``kind`` field is the leaf only — matching the Python side
  // where ``_variant_slug`` is per-level and the substrate keeps
  // wire-level identifiers flat.
  //
  // Per-variant method signatures vary with the parent's access
  // pattern:
  //
  //   append_only_log    → variantMethod(payload)
  //   singleton          → variantMethod(payload)
  //   keyed_per_entity   → variantMethod(key, payload)
  //   no pattern         → variantMethod(key, payload)  (mirrors .write)
  //
  // ``kind`` always wins over any user-supplied ``kind`` field —
  // variant identity is the contract.

  function _mergeKind(userPayload, slug) {
    var merged = Object.assign({}, userPayload || {});
    merged.kind = slug;
    return merged;
  }

  function _makeVariantMethod(proxy, slug) {
    return function(arg0, arg1) {
      if (proxy.access_pattern === 'keyed_per_entity') {
        return proxy.upsert(arg0, _mergeKind(arg1, slug));
      }
      var payload = _mergeKind(arg0, slug);
      if (typeof proxy.append === 'function') return proxy.append(payload);
      if (typeof proxy.set === 'function') return proxy.set(payload);
      // No pattern method available — caller must supply a key,
      // mirroring the ``.write({key, payload})`` shape.
      return proxy.write({ key: arg0, payload: _mergeKind(arg1, slug) });
    };
  }

  function _attachVariantMethods(target, proxy, variants) {
    if (!variants || typeof variants !== 'object') return;
    var slugs = Object.keys(variants);
    for (var i = 0; i < slugs.length; i++) {
      var slug = slugs[i];
      var variantPayload = variants[slug] || {};
      var nested = variantPayload.variants;
      if (nested && typeof nested === 'object' && Object.keys(nested).length > 0) {
        // Nested namespace — create a sub-object and recurse. The
        // intermediate node is not itself a callable (you reach a
        // leaf to write); only leaf variants stamp ``kind``.
        var subNamespace = {};
        _attachVariantMethods(subNamespace, proxy, nested);
        target[slug] = subNamespace;
      } else {
        target[slug] = _makeVariantMethod(proxy, slug);
      }
    }
  }

  function _variantExtension(proxy, payload) {
    _attachVariantMethods(proxy, proxy, payload.variants);
  }

  // Register the variant extension AFTER the pattern extension so
  // ``proxy.append`` / ``.set`` / ``.upsert`` exist by the time
  // variant methods reference them. Tests that want a clean slate
  // call ``_clearExtensions()`` and re-register selectively.
  _registerExtension(_variantExtension);

  // ── Schema.alpine() reflection wrapper (bead 2D) ────────────
  //
  // Wraps an Alpine factory's state object with schema-backed proxy
  // attachment. Plugin authors declare which schemas they need by
  // friendly name, and Schema.alpine arranges for each name to point
  // at a live ``Schema.of(setId)`` proxy by the time ``init()`` runs.
  //
  // Usage from a plugin's page.js:
  //
  //   function coordinatorBoard() {
  //     return Schema.alpine({
  //       tab: 'primary',
  //       async tapThumb(tile, kind) {
  //         await this.Decision[kind]({ tile_id: tile.session });
  //       },
  //     }, {
  //       schemas: {
  //         Canvas:        'dashboard.coordinator-canvas',
  //         OperatorMsg:   'dashboard.operator-message-to-coordinator',
  //         Decision:      'dashboard.coordinator-decision',
  //       },
  //     });
  //   }
  //
  // After the wrapped ``init()`` runs, ``this.Canvas`` / ``this.Decision``
  // are live proxies — the plugin's UI methods reach them like any
  // other reactive state.
  //
  // 2D ships the explicit-schemas path. A future bead can read the
  // plugin's manifest's ``entrypoints.schemas`` from
  // ``window.Autonomy._activePluginId`` and autobind without an
  // explicit map; the page-shell injection seam below seeds that.

  // ── Per-component proxy tracking ────────────────────────────
  //
  // ``Schema.alpine`` attaches schema proxies onto consumer state by
  // ``Schema.of(...)``; consumers then subscribe via
  // ``this.SchemaName.onChange(handler)`` from inside their init.
  // Proxies themselves are cached per (set_id, revision) for the
  // page's lifetime, so multiple components sharing a set share the
  // same proxy — which means we can't dispose listeners by destroying
  // the proxy. Instead, we wrap each attached proxy in a delegating
  // shim that records the unsub functions returned by ``.onChange``
  // and releases them when the wrapper's destroy fires. Consumers
  // never see the shim — it inherits the underlying proxy via
  // prototype, so every other method (.read, .all, .upsert, .write,
  // pattern methods, variant methods) reaches the cached proxy
  // unmodified.

  function _wrapProxyForTracking(proxy) {
    var unsubs = [];
    var shim = Object.create(proxy);
    shim.onChange = function(callback) {
      var unsub = proxy.onChange(callback);
      unsubs.push(unsub);
      return unsub;
    };
    shim._disposeListeners = function() {
      for (var i = 0; i < unsubs.length; i++) {
        try { unsubs[i](); } catch (err) {
          if (typeof console !== 'undefined' && console.warn) {
            console.warn('[Schema.alpine] listener teardown raised:', err);
          }
        }
      }
      unsubs.length = 0;
    };
    return shim;
  }

  function alpine(state, opts) {
    if (!state || typeof state !== 'object') {
      throw new TypeError('Schema.alpine requires a state object');
    }
    opts = opts || {};
    var schemaMap = opts.schemas || {};
    if (typeof schemaMap !== 'object') {
      throw new TypeError('Schema.alpine: opts.schemas must be a name→set_id map');
    }

    var origInit = (typeof state.init === 'function') ? state.init : null;
    var origDestroy = (typeof state.destroy === 'function') ? state.destroy : null;
    // Tracking shims attached to ``this`` during init. Captured in
    // closure rather than on state so they don't leak into the
    // consumer's reactive surface or show up in Alpine.$data dumps.
    var trackedShims = [];

    state.init = async function() {
      var names = Object.keys(schemaMap);
      var pairs = await Promise.all(names.map(function(name) {
        var entry = schemaMap[name];
        // Each entry is either a string (set_id, default revision) or an
        // object ``{set_id, revision}`` for consumers that need to bind
        // a schema at a specific non-default revision (e.g. tile/thread
        // at v2 in coordinator-board). The object form delegates to
        // ``Schema.of(setId, {revision})`` — same caching semantics as
        // direct ``of()`` calls.
        var setId, ofOpts;
        if (typeof entry === 'string') {
          setId = entry;
          ofOpts = undefined;
        } else if (entry && typeof entry === 'object'
                   && typeof entry.set_id === 'string' && entry.set_id) {
          setId = entry.set_id;
          ofOpts = (typeof entry.revision === 'number')
            ? { revision: entry.revision }
            : undefined;
        } else {
          return Promise.reject(new TypeError(
            'Schema.alpine: entry for "' + name + '" must be a set_id string '
            + 'or {set_id, revision} object'
          ));
        }
        return of(setId, ofOpts).then(function(proxy) { return [name, proxy]; });
      }));
      for (var i = 0; i < pairs.length; i++) {
        var shim = _wrapProxyForTracking(pairs[i][1]);
        trackedShims.push(shim);
        this[pairs[i][0]] = shim;
      }
      if (origInit) await origInit.call(this);
    };

    state.destroy = function() {
      // Release any onChange listeners the consumer registered through
      // the tracking shims. Every shim's _disposeListeners walks its
      // captured unsubs and calls them; the underlying cached proxy
      // is untouched so other live components keep working. Run this
      // BEFORE the consumer's own destroy so handlers stop firing
      // while the consumer's teardown executes.
      for (var i = 0; i < trackedShims.length; i++) {
        try { trackedShims[i]._disposeListeners(); } catch (err) {
          if (typeof console !== 'undefined' && console.warn) {
            console.warn('[Schema.alpine] dispose hook raised:', err);
          }
        }
      }
      trackedShims.length = 0;
      if (origDestroy) {
        try { origDestroy.call(this); } catch (err) {
          if (typeof console !== 'undefined' && console.warn) {
            console.warn('[Schema.alpine] destroy hook raised:', err);
          }
        }
      }
    };

    // Convenience: surface the active plugin id (when injected by
    // the page shell) so plugin pages can read their own context
    // without the page shell having to thread it through manually.
    if (typeof window !== 'undefined'
        && window.Autonomy
        && typeof window.Autonomy._activePluginId === 'string'
        && !state._pluginId) {
      state._pluginId = window.Autonomy._activePluginId;
    }

    return state;
  }

  // ── Test seams ──────────────────────────────────────────────

  function _setFetchOverride(fn) { _fetchOverride = fn; }
  function _clearFetchOverride() { _fetchOverride = null; }
  function _clearCache() { _proxyCache.clear(); }

  // ── Dual export ─────────────────────────────────────────────

  var SchemaNS = {
    of: of,
    alpine: alpine,
    Schema: Schema,
    _registerExtension: _registerExtension,
    _clearExtensions: _clearExtensions,
    _setFetchOverride: _setFetchOverride,
    _clearFetchOverride: _clearFetchOverride,
    _clearCache: _clearCache,
    _patternExtension: _patternExtension,
    _variantExtension: _variantExtension,
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = SchemaNS;
  } else if (typeof window !== 'undefined') {
    window.Schema = SchemaNS;
  }
})();
