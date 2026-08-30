// Sessions page Alpine component.
// Registered via alpine:init so it's available when the fragment is injected and
// Alpine.initTree() is called by the SPA router.
//
// Reads active sessions from Alpine.store('sessions') (fed by session:registry
// and session:messages SSE topics). Fetches recent sessions from DAO endpoint.

(function () {

  // ── Shared card helper methods ──────────────────────────────────────────
  // Exposed as window globals so both sessionsPage and designPage can use them.
  // The session-card.html partial references these by name.

  function _borderCls(t) {
    if (t === 'host') return 'session-card-host';
    if (t === 'dispatch' || t === 'agentic') return 'session-card-dispatch';
    if (t === 'librarian') return 'session-card-librarian';
    if (t === 'chatwith') return 'session-card-chatwith';
    return 'session-card-container';
  }

  function _typeBadge(t) {
    // 'agentic' badges as its own kind but styles with dispatch — the
    // recent list's dispatch bucket covers both (host dump defect 3:
    // agentic rows rendered badge-less, reading as interactive).
    if (t === 'agentic') return 'Agentic';
    if (t === 'dispatch') return 'Dispatch';
    if (t === 'librarian') return 'Librarian';
    if (t === 'host') return 'Host';
    if (t === 'chatwith') return 'Chat-with';
    return '';
  }

  function _typeCls(t) {
    if (t === 'host') return 'sc-type-host';
    if (t === 'dispatch' || t === 'agentic') return 'sc-type-dispatch';
    if (t === 'librarian') return 'sc-type-librarian';
    if (t === 'chatwith') return 'sc-type-chatwith';
    return '';
  }

  // Stat formatters live in /static/js/lib/session-stats.js (window.SessionStats)
  // so the session-viewer can reuse them. Thin aliases keep Alpine-scoped callsites
  // identical.
  function _turnsStr(s) { return window.SessionStats.turnsStr(s); }
  function _ctxStr(s) { return window.SessionStats.ctxStr(s); }
  function _idleStr(s) { return window.SessionStats.idleStr(s); }
  function _ctxWarn(s) { return window.SessionStats.ctxWarn(s); }
  function _recencyColor(s) { return window.SessionStats.recencyColor(s); }

  // Format ISO timestamp → "YYYY-MM-DD HH:MM" in operator-local timezone.
  function _fmtEndedAt(s) {
    var ts = s.ended_at || s.last_activity_at;
    if (!ts) return '—';
    var d = new Date(ts);
    if (isNaN(d.getTime())) return '—';
    var pad = function(n) { return n < 10 ? '0' + n : '' + n; };
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
      + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  // Footer column 3: 'idle' (relative duration) for live, 'ended' (absolute) for dead.
  function _endedOrIdleLabel(s) { return s.is_live ? 'idle' : 'ended'; }
  function _endedOrIdleValue(s) {
    return s.is_live ? (_idleStr(s) || '—') : _fmtEndedAt(s);
  }
  // Footer column 4: tmux is meaningful for interactive sessions but synthetic
  // for dispatch/librarian — hide entirely on dead dispatch/librarian rows.
  function _showTmuxColumn(s) {
    if (s.is_live) return true;
    return s.session_type !== 'dispatch' && s.session_type !== 'librarian';
  }

  // Expose helpers globally for session-card.html partial (used by sessionsPage and designPage)
  window.sessionCardHelpers = {
    borderCls: _borderCls,
    typeBadge: _typeBadge,
    typeCls: _typeCls,
    turnsStr: _turnsStr,
    ctxStr: _ctxStr,
    idleStr: _idleStr,
    ctxWarn: _ctxWarn,
    recencyColor: _recencyColor,
    endedOrIdleLabel: _endedOrIdleLabel,
    endedOrIdleValue: _endedOrIdleValue,
    showTmuxColumn: _showTmuxColumn,
  };

  // ── Private utilities ───────────────────────────────────────────────────

  function _setNag(tmux, interval, message) {
    fetch('/api/session/' + encodeURIComponent(tmux) + '/nag', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: true, interval: interval, message: message || ''}),
    });
  }

  // Derive role from explicit field or label patterns (always capitalized)
  function _deriveRole(s) {
    var raw = s.role || '';
    if (!raw) {
      var label = (s.label || '').toLowerCase();
      if (label.indexOf('coordinator') !== -1) raw = 'Coordinator';
      else if (label.indexOf('reviewer') !== -1 || label.indexOf('review') !== -1) raw = 'Reviewer';
      else if (label.indexOf('builder') !== -1 || label.indexOf('build') !== -1) raw = 'Builder';
      else if (label.indexOf('designer') !== -1 || label.indexOf('design') !== -1) raw = 'Designer';
    }
    if (raw) return raw.charAt(0).toUpperCase() + raw.slice(1);
    return '';
  }

  // Build a human-readable title for a librarian recent-card row.
  // The DAO populates librarian_type + librarian_target_bead_id from
  // librarian_jobs.payload. We format as:
  //   "{type} · {bead_id}"         — when target resolves
  //   "{type}"                     — type known but no target
  //   null                         — neither: caller falls back to r.title
  // Separate from the bead title which renders in muted color next to the id.
  function _librarianTitle(r) {
    var type = r.librarian_type;
    var target = r.librarian_target_bead_id;
    if (!type && !target) return null;
    if (type && target) return type + ' · ' + target;
    return type || null;
  }

  // ── Recent history projection ──────────────────────────────────────────
  //
  // This belongs to the PWA shell, not an individual Sessions component.
  // Hydrate it once after a full reload; organization/type/sort/since are
  // local views, and registry SSE applies the only lifecycle deltas. Keeping
  // the projection here also means desktop SPA remounts do not fetch history
  // again merely because the operator left and returned to /sessions.
  // Mirrors the server's per-org history floor (dao/sessions.py
  // include_org_floor): a selected org always shows at least this many of
  // its newest sessions, however old they are.
  var RECENT_ORG_FLOOR = 10;

  var _recentHistory = {
    rows: [],
    loaded: false,
    loading: false,
    error: '',
    promise: null,
    pendingRegistry: [],
    pendingEnded: [],
  };

  // The server stamps session_group (dao/sessions.py) — the single source
  // of truth for chip/quota bucketing. The fallback below exists ONLY for
  // payloads that predate the stamp (e.g. session:ended SSE rows); it must
  // mirror the server's _SESSION_TYPE_GROUPS and is the one client-side
  // copy. Never bucket off session_type anywhere else — two hand-synced
  // mappings is how agentic rows rendered in no chip at all (2026-08-29).
  function _rowGroup(s) {
    if (s && s.session_group) return s.session_group;
    var t = (s && s.session_type) || '';
    if (t === 'dispatch' || t === 'agentic' || t === 'agent-run') return 'dispatch';
    if (t === 'librarian') return 'librarian';
    return 'interactive';
  }

  function _mapRecentRow(r) {
    var sessionId = r.tmux_session;
    if (!sessionId) return null;
    var lastTs = r.last_activity_at || r.created_at || '';
    var label = r.title || '';
    if (r.session_type === 'librarian') {
      var libLabel = _librarianTitle(r);
      if (libLabel) label = libLabel;
    }
    return {
      id: r.id,
      session_id: sessionId,
      label: label,
      session_type: r.session_type || 'interactive',
      session_group: r.session_group || null,
      type: r.type || 'container',
      is_live: !!r.is_live,
      project: r.project || '',
      topics: [],
      latest: '',
      entry_count: r.entry_count || r.total_turns || 0,
      context_tokens: r.context_tokens || r.total_tokens || 0,
      last_activity: lastTs ? Math.round(new Date(lastTs).getTime() / 1000) : 0,
      created_at: r.created_at || '',
      last_activity_at: r.last_activity_at || '',
      ended_at: r.ended_at || r.last_activity_at || '',
      tmux_session: sessionId,
      nag_enabled: false,
      dispatch_nag_enabled: false,
      role: r.role || '',
      resumable: r.resumable || false,
      bead_id: r.bead_id || '',
      org: r.org || null,
      librarian_type: r.librarian_type || null,
      librarian_target_bead_id: r.librarian_target_bead_id || null,
      librarian_target_bead_title: r.librarian_target_bead_title || '',
      harness: r.harness || null,
      model: r.model || null,
      disk_bytes: r.disk_bytes != null ? r.disk_bytes : null,
      disk_detail: r.disk_detail || null,
      setup_phase: r.setup_phase || 'pending',
      harness_phase: r.harness_phase || 'pending',
      harness_state: r.harness_state || {},
      resolved: true,
    };
  }

  function _emitRecentHistoryChanged() {
    window.dispatchEvent(new CustomEvent('recent-sessions:changed', {
      detail: {
        rows: _recentHistory.rows,
        loaded: _recentHistory.loaded,
        loading: _recentHistory.loading,
        error: _recentHistory.error,
      },
    }));
  }

  function _applyRecentRegistry(detail) {
    detail = detail || {};
    var active = new Set(detail.activeSessionIds || []);
    var next = _recentHistory.rows.filter(function(row) {
      return !active.has(row.session_id);
    });
    _recentHistory.rows = next;
  }

  function _applyRecentEnded(payload) {
    var row = _mapRecentRow(payload || {});
    if (!row) return;
    _recentHistory.rows = _recentHistory.rows.filter(function(existing) {
      return existing.session_id !== row.session_id;
    });
    _recentHistory.rows.unshift(row);
  }

  function _applyOrBufferRecentRegistry(detail) {
    // The Active-first sequencing window exists before the snapshot starts;
    // retain lifecycle deltas there too so no session can disappear between
    // the last roster event and the history response.
    if (_recentHistory.loading || !_recentHistory.loaded) {
      _recentHistory.pendingRegistry.push(detail || {});
      return;
    }
    _applyRecentRegistry(detail);
    _emitRecentHistoryChanged();
  }

  function _applyOrBufferRecentEnded(payload) {
    // Do not let an in-flight bootstrap overwrite a terminal transition. Once
    // the request has settled, an explicit lifecycle event always renders,
    // even if the bootstrap itself failed.
    if (_recentHistory.loading) {
      _recentHistory.pendingEnded.push(payload || {});
      return;
    }
    _applyRecentEnded(payload);
    _emitRecentHistoryChanged();
  }

  // Recent is a shared projection. Actions such as Resume must update this
  // canonical array too, rather than only the mounted Alpine component, or a
  // later registry event would resurrect the card that was just moved Active.
  function _removeRecentHistoryRow(row) {
    var index = _recentHistory.rows.indexOf(row);
    if (index === -1) return -1;
    _recentHistory.rows.splice(index, 1);
    _emitRecentHistoryChanged();
    return index;
  }

  function _restoreRecentHistoryRow(row, index) {
    if (_recentHistory.rows.indexOf(row) !== -1) return;
    if (index >= 0) _recentHistory.rows.splice(index, 0, row);
    else _recentHistory.rows.unshift(row);
    _emitRecentHistoryChanged();
  }

  function _loadRecentHistory() {
    if (_recentHistory.loaded) return Promise.resolve(_recentHistory.rows);
    if (_recentHistory.promise) return _recentHistory.promise;
    _recentHistory.loading = true;
    _recentHistory.error = '';
    _emitRecentHistoryChanged();
    _recentHistory.promise = fetch('/api/dao/recent_sessions?snapshot=1')
      .then(function(response) {
        if (!response.ok) throw new Error('history request failed (' + response.status + ')');
        return response.json();
      })
      .then(function(data) {
        if (!Array.isArray(data)) throw new Error('history response was not a list');
        _recentHistory.rows = data.map(_mapRecentRow).filter(function(row) { return row !== null; });
        _recentHistory.loaded = true;
        var pending = _recentHistory.pendingRegistry;
        _recentHistory.pendingRegistry = [];
        pending.forEach(_applyRecentRegistry);
        var pendingEnded = _recentHistory.pendingEnded;
        _recentHistory.pendingEnded = [];
        pendingEnded.forEach(_applyRecentEnded);
        return _recentHistory.rows;
      })
      .catch(function(error) {
        _recentHistory.error = error.message || 'Could not load history';
        console.warn('[sessionsPage] recent history fetch error', error);
        var pendingEnded = _recentHistory.pendingEnded;
        _recentHistory.pendingEnded = [];
        pendingEnded.forEach(_applyRecentEnded);
        return _recentHistory.rows;
      })
      .finally(function() {
        _recentHistory.loading = false;
        _recentHistory.promise = null;
        _emitRecentHistoryChanged();
      });
    return _recentHistory.promise;
  }

  window.addEventListener('sessions:registry-changed', function(event) {
    _applyOrBufferRecentRegistry(event && event.detail);
  });
  if (typeof window.registerHandler === 'function') {
    window.registerHandler('session:ended', _applyOrBufferRecentEnded);
  }

  // Derive canonical session_type from store sessionType
  function _deriveSessionType(s) {
    var t = s.sessionType || 'terminal';
    if (t === 'host') return 'host';
    if (t === 'chatwith') return 'chatwith';
    if (t === 'container' && s.beadId) return 'dispatch';
    if (t === 'terminal') return 'interactive';
    return 'interactive';
  }

  // --- Zoom level persistence ---
  var _savedZoom = localStorage.getItem('sessionZoom') || 'normal';
  function _applyZoom(level) {
    document.body.classList.remove('zoom-compact', 'zoom-normal', 'zoom-expanded');
    document.body.classList.add('zoom-' + level);
  }
  _applyZoom(_savedZoom);

  // Expose setZoom globally so template buttons can call it
  window.setZoom = function(level) {
    localStorage.setItem('sessionZoom', level);
    _applyZoom(level);
    Alpine.store('sessionZoom', level);
  };

  document.addEventListener('alpine:init', () => {
    Alpine.store('sessionZoom', _savedZoom);

    Alpine.data('sessionsPage', () => ({
      interactive: [],
      recent: [],
      recentLoading: true,
      recentError: '',
      loading: true,
      _creating: false,
      zoom: localStorage.getItem('sessionZoom') || 'normal',

      // Workspace-changes state per tmux name. Populated by
      // _fetchWorkspaceStatus and consumed by ``wsStatus(s)`` /
      // ``hasWorkspaceChanges(s)`` so the session-card partial can
      // surface the same ⌥ indicator the page-mode header shows.
      _workspaceStatusByTmux: {},

      hasWorkspaceChanges(s) {
        var key = (s && (s.tmux_session || s.id)) || '';
        var ws = this._workspaceStatusByTmux[key];
        return !!(ws && ws.hasChanges);
      },
      workspaceStatusTooltip(s) {
        var key = (s && (s.tmux_session || s.id)) || '';
        var ws = this._workspaceStatusByTmux[key];
        if (!ws || !ws.hasChanges) return '';
        var bits = [];
        if (ws.commitsAhead > 0) {
          bits.push(ws.commitsAhead + ' commit' + (ws.commitsAhead === 1 ? '' : 's') + ' to review');
        }
        if (ws.dirtyCount > 0) {
          bits.push(ws.dirtyCount + ' dirty file' + (ws.dirtyCount === 1 ? '' : 's'));
        }
        return 'Workspace: ' + bits.join(', ') + ' — open Worktrees review';
      },
      workspaceStatusHref(s) {
        var key = (s && (s.tmux_session || s.id)) || '';
        return '/worktrees?session=' + encodeURIComponent(key);
      },

      // --- Per-session resource metrics (design d2250266 meta2 row) ---
      // Pushed over the shared SSE bus: the collector broadcasts its
      // latest samples on the 'resources' topic after each ~6s tick
      // (dedup'd server-side, last-value replayed on registration — same
      // contract as the 'worktrees' topic). Keyed by tmux name. The
      // sparkline ring buffer lives HERE, client-side: one
      // /api/resources?history=1 hydrate on init backfills it, then each
      // pushed sample is appended. Ended cards read the final persisted
      // footprint off the row itself (disk_bytes / disk_detail, written
      // by the collector's death-path measure).
      resources: {},
      _resourceTipOpen: null,   // tmux name whose disk tooltip is tapped open
      _diskRefreshing: {},      // tmux name → true while force-refresh runs
      _SPARK_SAMPLES: 100,      // client ring buffer cap (matches collector)

      _applyResourceRows(rows) {
        var next = {};
        for (var key in rows) {
          var r = rows[key];
          var prev = this.resources[key];
          var hist;
          if (r.history && r.history.length) {
            // REST hydrate: the server ring buffer is the base. It must WIN
            // over any prev entry — the SSE handler's cached replay fires
            // synchronously on registration, so by the time this fetch
            // returns, prev already exists with 1-2 points; preferring prev
            // here silently discarded the full history on every page load
            // (sparklines looked "reset" on fresh desktop loads while
            // long-open mobile pages kept their own buffer). Points that
            // arrived over SSE during the fetch are grafted on the end.
            hist = r.history.slice();
            if (prev && prev.history && prev.history.length) {
              var baseTs = hist[hist.length - 1][0];
              for (var i = 0; i < prev.history.length; i++) {
                if (prev.history[i][0] > baseTs) hist.push(prev.history[i]);
              }
            }
          } else {
            // SSE push: append the latest sample to the client-side ring.
            hist = (prev && prev.history) || [];
            if (r.sampled_at && r.cpu_pct != null) {
              var lastTs = hist.length ? hist[hist.length - 1][0] : 0;
              if (r.sampled_at > lastTs) {
                hist = hist.concat([[r.sampled_at, r.cpu_pct, r.mem_bytes]]);
              }
            }
          }
          if (hist.length > this._SPARK_SAMPLES) {
            hist = hist.slice(hist.length - this._SPARK_SAMPLES);
          }
          next[key] = Object.assign({}, r, { history: hist });
        }
        this.resources = next;
      },
      async _hydrateResources() {
        try {
          var res = await fetch('/api/resources?history=1');
          if (res.ok) {
            this._applyResourceRows((await res.json()).sessions || {});
          }
        } catch (e) { /* SSE pushes will populate from here */ }
      },

      resourceFor(s) {
        var key = (s && (s.tmux_session || s.tmux_name || s.id)) || '';
        return this.resources[key] || null;
      },
      fmtBytes(n) {
        if (n == null || isNaN(n)) return '';
        if (n >= 1e9) return (n / 1e9).toFixed(n >= 1e10 ? 0 : 1) + 'GB';
        if (n >= 1e6) return (n / 1e6).toFixed(0) + 'MB';
        if (n >= 1e3) return (n / 1e3).toFixed(0) + 'KB';
        return n + 'B';
      },
      cpuStr(s) {
        var r = this.resourceFor(s);
        return (r && r.cpu_pct != null) ? r.cpu_pct.toFixed(r.cpu_pct >= 10 ? 0 : 1) + '%' : '—';
      },
      ramStr(s) {
        var r = this.resourceFor(s);
        return (r && r.mem_bytes != null) ? this.fmtBytes(r.mem_bytes) : '—';
      },
      diskStr(s) {
        if (s.is_live) {
          var r = this.resourceFor(s);
          return (r && r.disk && r.disk.total != null) ? this.fmtBytes(r.disk.total) : '—';
        }
        return s.disk_bytes != null ? this.fmtBytes(s.disk_bytes) : '';
      },
      // Breakdown rows for the disk tooltip: [['Run dir','2.4MB'], ...].
      // Live cards read the collector's latest merged detail; ended cards
      // parse the persisted JSON. Component keys → operator labels.
      diskDetail(s) {
        var detail = null;
        if (s.is_live) {
          var r = this.resourceFor(s);
          detail = r && r.disk;
        } else if (s.disk_detail) {
          try { detail = JSON.parse(s.disk_detail); } catch (e) { detail = null; }
        }
        if (!detail || !detail.components) return [];
        // Design d2250266 order + vocabulary: Image / Worktree / Output.
        // (Image = the container's writable layer; Output = the run dir
        // under agent-runs; Transcript = host-session JSONL.)
        var order = [['container_fs', 'Image'], ['worktrees', 'Worktree'],
                     ['run_dir', 'Output'], ['jsonl', 'Transcript']];
        var rows = [];
        for (var i = 0; i < order.length; i++) {
          var k = order[i][0];
          if (detail.components[k] != null) {
            rows.push([order[i][1], this.fmtBytes(detail.components[k])]);
          }
        }
        rows.push(['Total', this.fmtBytes(detail.total)]);
        return rows;
      },
      resourceTipKey(s) {
        return (s && (s.tmux_session || s.tmux_name || s.id)) || '';
      },
      // "started → ended · duration" line for ended cards (design
      // d2250266) — replaces the ENDED footer column. Accepts epoch
      // seconds (session store rows) or ISO strings (recent DAO rows).
      _whenEpoch(v) {
        if (v == null || v === '') return 0;
        if (typeof v === 'number') return v > 1e12 ? v / 1000 : v;
        var t = Date.parse(v);
        return isNaN(t) ? 0 : t / 1000;
      },
      _whenFmt(epoch) {
        if (!epoch) return '';
        var d = new Date(epoch * 1000);
        function p(n) { return (n < 10 ? '0' : '') + n; }
        return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
          + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
      },
      _whenDur(secs) {
        if (secs < 90) return Math.round(secs) + 's';
        if (secs < 5400) return Math.round(secs / 60) + 'm';
        if (secs < 129600) return (secs / 3600).toFixed(1).replace(/\.0$/, '') + 'h';
        return (secs / 86400).toFixed(1).replace(/\.0$/, '') + 'd';
      },
      whenLine(s) {
        if (s.is_live) return '';
        var t0 = this._whenEpoch(s.created_at);
        var t1 = this._whenEpoch(s.ended_at || s.last_activity_at || s.last_activity);
        if (!t0 && !t1) return '';
        var out = (this._whenFmt(t0) || '—') + ' → ' + (this._whenFmt(t1) || '—');
        if (t0 && t1 && t1 > t0) out += ' · ' + this._whenDur(t1 - t0);
        return out;
      },
      toggleResourceTip(s) {
        var key = this.resourceTipKey(s);
        this._resourceTipOpen = this._resourceTipOpen === key ? null : key;
      },
      async refreshDisk(s) {
        var key = this.resourceTipKey(s);
        if (!key || this._diskRefreshing[key]) return;
        this._diskRefreshing[key] = true;
        try {
          var res = await fetch('/api/resources/' + encodeURIComponent(key) + '/refresh',
                                { method: 'POST' });
          if (res.ok) {
            var body = await res.json();
            if (this.resources[key]) {
              this.resources[key] = Object.assign({}, this.resources[key], { disk: body.disk });
            } else {
              this.resources[key] = { disk: body.disk };
            }
          }
        } catch (e) { /* transient — next poll repaints */ }
        this._diskRefreshing[key] = false;
      },
      // Dual CPU/RAM sparkline over the collector's history ring buffer.
      // Ported from design d2250266 v58 (Catmull-Rom smoothing, pulsing
      // endpoint), with scaling rules from operator review:
      // - y-range is floored (CPU 100 percentage points, RAM 500MB) so
      //   idle noise doesn't auto-scale into fake spikes;
      // - RAM is centered in its floored span so the (usually near-flat)
      //   blue line rides mid-height instead of hiding under the CPU line
      //   at the baseline;
      // - CPU samples under 1% are not drawn at all: an idle session shows
      //   no amber trace rather than a flat line along the bottom. The
      //   line breaks into segments around the gaps; isolated single
      //   active samples render as dots.
      sparkSvg(s) {
        var r = this.resourceFor(s);
        var hist = (r && r.history) || [];
        if (hist.length < 2) return '';
        var W = 180, H = 42, pad = 6;
        function scaled(data, minSpan, center) {
          data = data.filter(function (n) { return !isNaN(n); });
          if (data.length < 2) return null;
          var mn = Math.min.apply(null, data), mx = Math.max.apply(null, data);
          var sp = Math.max(mx - mn, minSpan || 1);
          var base = center ? mn - (sp - (mx - mn)) / 2 : mn;
          var n = data.length;
          return data.map(function (v, i) {
            return [pad + (i / (n - 1)) * (W - pad * 2),
                    H - pad - ((v - base) / sp) * (H - pad * 2), v];
          });
        }
        function path(points) {
          var d = 'M' + points[0][0].toFixed(1) + ' ' + points[0][1].toFixed(1);
          for (var i = 0; i < points.length - 1; i++) {
            var p0 = points[i - 1] || points[i], p1 = points[i],
                p2 = points[i + 1], p3 = points[i + 2] || p2;
            d += ' C' + (p1[0] + (p2[0] - p0[0]) / 6).toFixed(1) + ' ' + (p1[1] + (p2[1] - p0[1]) / 6).toFixed(1) +
                 ' ' + (p2[0] - (p3[0] - p1[0]) / 6).toFixed(1) + ' ' + (p2[1] - (p3[1] - p1[1]) / 6).toFixed(1) +
                 ' ' + p2[0].toFixed(1) + ' ' + p2[1].toFixed(1);
          }
          return d;
        }
        function stroke(points, color) {
          return '<path d="' + path(points) + '" fill="none" stroke="' + color + '" stroke-width="1.6" ' +
                 'stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>';
        }
        function pulse(pt, color) {
          return '<circle cx="' + pt[0].toFixed(1) + '" cy="' + pt[1].toFixed(1) + '" r="2.2" fill="' + color + '" vector-effect="non-scaling-stroke">' +
                 '<animate attributeName="opacity" values="1;0.5;1" dur="2.4s" repeatCount="indefinite"/></circle>';
        }
        // history rows are [ts, cpu_pct, mem_bytes]
        var out = '';
        var ram = scaled(hist.map(function (h) { return Number(h[2]); }), 500e6, true);
        if (ram) out += stroke(ram, '#60a5fa') + pulse(ram[ram.length - 1], '#60a5fa');
        var cpu = scaled(hist.map(function (h) { return Number(h[1]); }), 100, false);
        if (cpu) {
          var seg = [];
          for (var i = 0; i <= cpu.length; i++) {
            if (i < cpu.length && cpu[i][2] >= 1) { seg.push(cpu[i]); continue; }
            if (seg.length >= 2) out += stroke(seg, '#fbbf24');
            else if (seg.length === 1) out += '<circle cx="' + seg[0][0].toFixed(1) + '" cy="' + seg[0][1].toFixed(1) + '" r="1.6" fill="#fbbf24"/>';
            seg = [];
          }
          if (cpu[cpu.length - 1][2] >= 1) out += pulse(cpu[cpu.length - 1], '#fbbf24');
        }
        if (!out) return '';
        return '<svg class="sc-spark-svg" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" aria-hidden="true">' + out + '</svg>';
      },
      // --- Workspace dropdown state (fetched from /api/projects) ---
      // orgGroups[i].org is a resolved identity object
      // {slug,name,color,favicon,initial,resolved} — the header renders a
      // glyph + name and inline-colors the text from org.color. The launch
      // menu consumes visibleOrgGroups so it follows the page-level org
      // filter without discarding the full fetched registry.
      projects: [],
      orgGroups: [],

      get visibleOrgGroups() {
        var selectedOrg = this.selectedOrg;
        if (!selectedOrg) return this.orgGroups;
        return this.orgGroups.filter(function(group) {
          return !!(group.org && group.org.slug === selectedOrg);
        });
      },

      // --- Org filter (toolbar dropdown, between zoom + launch button) ---
      // '' means "All orgs" (default). Filters both the Active and Recent
      // sections by comparing against the resolved s.org.slug already
      // present on every session row (see _updateFromStore / history snapshot).
      selectedOrg: localStorage.getItem('sessionsOrgFilter') || '',
      orgFilterList: [],
      orgFilterOpen: false,

      async _fetchOrgFilterList() {
        try {
          const data = await fetch('/api/orgs').then(r => r.ok ? r.json() : { orgs: [] });
          this.orgFilterList = (data.orgs || []).map(function(e) {
            var org = (e && e.org) || {};
            var ident = (e && e.identity_resolved) || {};
            var slug = org.slug || ident.slug || '';
            return {
              slug: slug,
              name: ident.name || slug,
              color: ident.color || '#4b5563',
              favicon: ident.favicon || null,
              initial: ident.initial || (slug ? slug[0].toUpperCase() : '?'),
            };
          }).filter(function(o) { return o.slug; });
        } catch (e) {
          console.warn('[sessionsPage] orgs fetch error', e);
          this.orgFilterList = [];
        }
      },

      get orgFilterPicked() {
        var self = this;
        if (!this.selectedOrg) return null;
        return (this.orgFilterList || []).find(function(o) { return o.slug === self.selectedOrg; }) || null;
      },

      pickOrgFilter(slug) {
        this.orgFilterOpen = false;
        slug = slug || '';
        if (slug === this.selectedOrg) return;
        this.selectedOrg = slug;
        localStorage.setItem('sessionsOrgFilter', slug);
      },

      _matchesOrg(s) {
        if (!this.selectedOrg) return true;
        return !!(s.org && s.org.slug === this.selectedOrg);
      },

      // --- Card helper methods (referenced by session-card.html partial) ---
      borderCls: _borderCls,
      typeBadge: _typeBadge,
      typeCls: _typeCls,
      turnsStr: _turnsStr,
      ctxStr: _ctxStr,
      idleStr: _idleStr,
      ctxWarn: _ctxWarn,
      recencyColor: _recencyColor,
      endedOrIdleLabel: _endedOrIdleLabel,
      endedOrIdleValue: _endedOrIdleValue,
      showTmuxColumn: _showTmuxColumn,

      // --- Active Sessions: client-side sort mode ---
      // Options match the dropdown-toggle partial contract: [{k, l}, ...]
      activeSortOptions: [
        {k: 'lastActivity', l: 'Recent Activity'},
        {k: 'recentInput',  l: 'Recent Input'},
        {k: 'idle',         l: 'Longest Idle'},
        {k: 'turns',        l: 'Most Turns'},
        {k: 'ctx',          l: 'Most Context'},
      ],
      activeSort: (function() {
        var saved = localStorage.getItem('sessionsActiveSort');
        var allowed = ['lastActivity', 'recentInput', 'idle', 'turns', 'ctx'];
        return allowed.indexOf(saved) !== -1 ? saved : 'lastActivity';
      })(),
      activeSortDirection: localStorage.getItem('sessionsActiveSortDirection') === 'asc' ? 'asc' : 'desc',
      _activeOrder: [],
      _activeOrderReady: false,
      _activeOrderNeedsInitialData: true,
      // Snapshot of the cards currently rendered in Launching. It lets the
      // frozen Active order distinguish a launch completion (insert at the
      // top) from an unrelated membership arrival (append without disturbing
      // the cards the operator is already scanning).
      _launchingSessionIds: {},

      // Booting sessions: optimistic pending tiles + any live session the
      // lifecycle derivation still marks as starting up (startupVisible).
      // Rendered in a dedicated "Launching" section pinned to the top of
      // the list, newest-first, so a booting session is always reachable
      // regardless of the Active sort (turns/idle/ctx).
      _isLaunching(s) {
        if (s._launching) return true;
        var L = window.Autonomy && window.Autonomy.lifecycle;
        if (!L) return false;
        // Closing is not launching: a STOPPING session (stopping/cleaning
        // chip) must stay in the Active grid on its way down, not jump into
        // the Launching section (startupVisible is the card-strip gate and
        // is true for any non-ready/non-dead state, including teardown).
        var key = L.lifecycleState(s);
        if (key === 'stopping' || key === 'cleaning') return false;
        return !!L.startupVisible(s);
      },
      get launching() {
        var self = this;
        var arr = this.interactive.filter(function(s) { return self._isLaunching(s) && self._matchesOrg(s); });
        arr.sort(function(a, b) { return (b.created_at || 0) - (a.created_at || 0); });
        return arr;
      },
      get activeInteractive() {
        var self = this;
        return this.interactive.filter(function(s) { return !self._isLaunching(s) && self._matchesOrg(s); });
      },

      _currentLaunchingSessionIds() {
        var self = this;
        var ids = {};
        for (var i = 0; i < this.interactive.length; i++) {
          var session = this.interactive[i];
          if (self._isLaunching(session)) ids[session.session_id] = true;
        }
        return ids;
      },

      get sortedInteractive() {
        var arr = this.activeInteractive.slice();
        if (!this._activeOrderReady) return arr;
        var positions = {};
        for (var i = 0; i < this._activeOrder.length; i++) {
          positions[this._activeOrder[i]] = i;
        }
        arr.sort(function(a, b) {
          var ai = positions[a.session_id];
          var bi = positions[b.session_id];
          if (ai === undefined) ai = Number.MAX_SAFE_INTEGER;
          if (bi === undefined) bi = Number.MAX_SAFE_INTEGER;
          return ai - bi;
        });
        return arr;
      },

      _activeSortMetric(s) {
        switch (this.activeSort) {
          case 'recentInput':
            // Never-contacted sessions are oldest: first in ascending order,
            // last in descending order.
            return s.last_input_at || -Infinity;
          case 'idle':
            // Negating activity makes an older timestamp the larger metric,
            // so the default descending direction still means Longest Idle.
            return -(s.last_activity || s.created_at || 0);
          case 'turns':
            return s.entry_count || 0;
          case 'ctx':
            return s.context_tokens || 0;
          case 'lastActivity':
          default:
            return s.last_activity || -Infinity;
        }
      },

      refreshActiveOrder() {
        var self = this;
        var arr = this.interactive.filter(function(s) { return !self._isLaunching(s); });
        var prior = {};
        for (var i = 0; i < this._activeOrder.length; i++) prior[this._activeOrder[i]] = i;
        var direction = this.activeSortDirection === 'asc' ? 1 : -1;
        arr.sort(function(a, b) {
          var av = self._activeSortMetric(a);
          var bv = self._activeSortMetric(b);
          if (av < bv) return -1 * direction;
          if (av > bv) return 1 * direction;
          var ai = prior[a.session_id];
          var bi = prior[b.session_id];
          if (ai !== undefined || bi !== undefined) {
            if (ai === undefined) ai = Number.MAX_SAFE_INTEGER;
            if (bi === undefined) bi = Number.MAX_SAFE_INTEGER;
            return ai - bi;
          }
          return (b.created_at || 0) - (a.created_at || 0);
        });
        this._activeOrder = arr.map(function(s) { return s.session_id; });
        this._launchingSessionIds = this._currentLaunchingSessionIds();
        this._activeOrderReady = true;
        if (arr.length > 0) this._activeOrderNeedsInitialData = false;
      },

      _reconcileActiveOrder() {
        if (!this._activeOrderReady) return;
        var self = this;
        var active = this.interactive.filter(function(s) { return !self._isLaunching(s); });
        var current = {};
        for (var i = 0; i < active.length; i++) current[active[i].session_id] = true;
        var next = this._activeOrder.filter(function(id) { return !!current[id]; });
        var seen = {};
        for (var j = 0; j < next.length; j++) seen[next[j]] = true;
        var promoted = [];
        var appended = [];
        // Membership changes are allowed to appear immediately, but never
        // disturb the relative positions of cards the operator is watching.
        // A card the operator just watched finish Launching is the one
        // intentional exception: carry it across the section boundary at the
        // top, where it was already visually anchored.
        for (var k = 0; k < active.length; k++) {
          var id = active[k].session_id;
          if (!seen[id]) {
            if (this._launchingSessionIds[id]) promoted.push(id);
            else appended.push(id);
          }
        }
        next = promoted.concat(next, appended);
        this._launchingSessionIds = this._currentLaunchingSessionIds();
        var changed = next.length !== this._activeOrder.length;
        for (var n = 0; !changed && n < next.length; n++) {
          if (next[n] !== this._activeOrder[n]) changed = true;
        }
        if (changed) this._activeOrder = next;
      },

      toggleActiveSortDirection() {
        this.activeSortDirection = this.activeSortDirection === 'desc' ? 'asc' : 'desc';
        localStorage.setItem('sessionsActiveSortDirection', this.activeSortDirection);
        this._updateFromStore();
        this.refreshActiveOrder();
      },

      activeSortDirectionTitle() {
        return this.activeSortDirection === 'desc'
          ? 'Descending order. Tap for ascending.'
          : 'Ascending order. Tap for descending.';
      },

      // --- Recent Sessions: filter + resume state ---
      recentFilter: localStorage.getItem('recentSessionFilter') || 'all',
      filterOptions: [
        {key: 'all', label: 'All'},
        {key: 'interactive', label: 'Interactive'},
        {key: 'dispatch', label: 'Dispatch'},
        {key: 'librarian', label: 'Librarian'},
      ],
      // --- Recent Sessions: sort + since (per design acb2829b-4fc0 rev b39626f2) ---
      recentSortOptions: [
        {k: 'lastActivity', l: 'End time'},
        {k: 'created',      l: 'Start time'},
        {k: 'turns',        l: 'Most Turns'},
        {k: 'ctx',          l: 'Most Context'},
        {k: 'duration',     l: 'Duration'},
      ],
      recentSinceOptions: [
        {k: '6h',  l: '6 hours'},
        {k: '1d',  l: '1 day'},
        {k: '1w',  l: '1 week'},
        {k: 'all', l: 'All time'},
      ],
      recentSort: (function() {
        var saved = localStorage.getItem('recentSort');
        var allowed = ['lastActivity', 'created', 'turns', 'ctx', 'duration'];
        return allowed.indexOf(saved) !== -1 ? saved : 'lastActivity';
      })(),
      recentSince: (function() {
        var saved = localStorage.getItem('recentSince');
        var allowed = ['6h', '1d', '1w', 'all'];
        return allowed.indexOf(saved) !== -1 ? saved : '1d';
      })(),
      resuming: {},
      resumeError: {},
      resumed: {},

      setRecentFilter(f) {
        if (this.recentFilter === f) return;
        this.recentFilter = f;
        localStorage.setItem('recentSessionFilter', f);
      },

      get filtered() {
        var self = this;
        var base = this.recentFilter === 'all'
          ? this.recent
          : this.recent.filter(function(s) { return self._matchesFilter(s, self.recentFilter); });
        base = base.filter(function(s) { return self._matchesOrg(s); });
        var windowed = base.filter(function(s) { return self._matchesRecentSince(s); });
        // A selected organization is guaranteed its 10 most-recent sessions
        // regardless of the Since window. The server ships an age-independent
        // ten-row floor per org for exactly this (dao/sessions.py
        // include_org_floor); letting the Since projection hide those rows is
        // how a quiet org rendered an empty Recent list (2026-08-30).
        if (this.selectedOrg && windowed.length < RECENT_ORG_FLOOR && base.length > windowed.length) {
          var haveIds = {};
          windowed.forEach(function(s) { haveIds[s.id] = true; });
          var backfill = base
            .filter(function(s) { return !haveIds[s.id]; })
            .sort(function(a, b) {
              // Most-recent first, mirroring the server floor's contract —
              // the floor is "the org's newest N", not "top N by view sort".
              return self._recentEpoch(b.last_activity_at || b.created_at || b.last_activity)
                - self._recentEpoch(a.last_activity_at || a.created_at || a.last_activity);
            })
            .slice(0, RECENT_ORG_FLOOR - windowed.length);
          windowed = windowed.concat(backfill);
        }
        return this._capAndSortRecent(windowed);
      },

      _recentEpoch(value) {
        if (value == null || value === '') return 0;
        if (typeof value === 'number') return value > 1e12 ? value / 1000 : value;
        var parsed = Date.parse(value);
        return isNaN(parsed) ? 0 : parsed / 1000;
      },
      _matchesRecentSince(s) {
        if (this.recentSince === 'all') return true;
        var seconds = { '6h': 6 * 3600, '1d': 24 * 3600, '1w': 7 * 24 * 3600 }[this.recentSince] || 0;
        return this._recentEpoch(s.last_activity_at || s.created_at || s.last_activity) >= (Date.now() / 1000) - seconds;
      },
      _recentSortValue(s) {
        if (this.recentSort === 'created') return this._recentEpoch(s.created_at);
        if (this.recentSort === 'turns') return Number(s.entry_count) || 0;
        if (this.recentSort === 'ctx') return Number(s.context_tokens) || 0;
        if (this.recentSort === 'duration') {
          return this._recentEpoch(s.ended_at || s.last_activity_at || s.last_activity) - this._recentEpoch(s.created_at);
        }
        return this._recentEpoch(s.last_activity_at || s.last_activity || s.created_at);
      },
      _capAndSortRecent(rows) {
        var self = this;
        var sorted = rows.slice().sort(function(a, b) { return self._recentSortValue(b) - self._recentSortValue(a); });
        if (this.recentFilter !== 'all') return sorted.slice(0, 50);
        var quotas = { interactive: 20, dispatch: 10, librarian: 10 };
        var counts = { interactive: 0, dispatch: 0, librarian: 0 };
        return sorted.filter(function(row) {
          var group = _rowGroup(row);
          if (counts[group] >= quotas[group]) return false;
          counts[group] += 1;
          return true;
        });
      },

      _matchesFilter(s, f) {
        if (f === 'dispatch' || f === 'librarian') return _rowGroup(s) === f;
        return _rowGroup(s) === 'interactive';
      },

      // Empty-state sentence naming the filters that actually hid the rows.
      // Never interpolate the raw chip key: recentFilter === 'all' rendered
      // the literal text "No all sessions" (2026-08-30).
      recentEmptyMessage() {
        var chip = this.recentFilter !== 'all' ? this.recentFilter + ' ' : '';
        var org = '';
        if (this.selectedOrg) {
          var picked = this.orgFilterPicked;
          org = ' for ' + ((picked && picked.name) || this.selectedOrg);
        }
        var since = {
          '6h': ' in the last 6 hours',
          '1d': ' in the last day',
          '1w': ' in the last week',
        }[this.recentSince] || '';
        return 'No ' + chip + 'sessions' + org + since;
      },

      async resumeSession(s, $event) {
        $event.preventDefault();
        $event.stopPropagation();
        if (this.resuming[s.id]) return;
        this.resuming[s.id] = true;
        this.resumeError[s.id] = '';

        // ── Optimistic UI: move card from Recent → Active ──
        var recentIdx = _removeRecentHistoryRow(s);

        // Placeholder key — will be replaced by real tmux_name from API
        var placeholderKey = 'resume-' + s.id.slice(0, 8);
        var store = window.getSessionStore(placeholderKey);
        store.isLive = false;    // gray dot until monitor picks it up
        store._resuming = true;  // bypass isLive filter in _updateFromStore
        store.label = s.label || '';
        // Map DAO session_type → store sessionType that appears in active grid
        var typeMap = {dispatch: 'container', librarian: 'container', interactive: 'host'};
        store.sessionType = typeMap[s.session_type] || 'container';
        store.project = (s.project || '').replace(/^\[|\]$/g, '');
        store.entries = [];
        store.entryCount = 0;
        store.contextTokens = 0;
        store.startedAt = Date.now() / 1000;

        try {
          var res = await fetch('/api/session/resume', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({source_id: s.id}),
          });
          if (!res.ok) {
            var err = await res.json().catch(function() { return {}; });
            throw new Error(err.error || 'Resume failed');
          }
          var data = await res.json();
          this.resuming[s.id] = false;
          this.resumed[s.id] = true;

          // API returns real tmux_name — migrate store if different from placeholder
          var realKey = data.tmux_name || placeholderKey;
          if (realKey !== placeholderKey) {
            var realStore = window.getSessionStore(realKey);
            realStore.isLive = false;
            realStore._resuming = true;
            realStore.label = data.label || s.label || '';
            realStore.sessionType = store.sessionType;
            realStore.project = store.project;
            realStore.startedAt = store.startedAt;
            // Remove placeholder
            delete Alpine.store('sessions')[placeholderKey];
          }
        } catch (e) {
          this.resuming[s.id] = false;
          this.resumeError[s.id] = e.message || 'Failed';

          // ── Rollback: remove from store, re-insert into recent ──
          delete Alpine.store('sessions')[placeholderKey];
          _restoreRecentHistoryRow(s, recentIdx);

          var self = this;
          setTimeout(function() { self.resumeError[s.id] = ''; }, 3000);
        }
      },

      setZoom(level) {
        this.zoom = level;
        localStorage.setItem('sessionZoom', level);
        document.body.classList.remove('zoom-compact', 'zoom-normal', 'zoom-expanded');
        document.body.classList.add('zoom-' + level);
      },
      _scheduleStoreSync() {
        if (this._storeSyncQueued) return;
        this._storeSyncQueued = true;
        var self = this;
        var raf = window.requestAnimationFrame || function(cb) { return setTimeout(cb, 0); };
        raf(function() {
          self._storeSyncQueued = false;
          self._updateFromStore();
        });
      },
      init() {
        this.$watch('activeSort', (v) => {
          localStorage.setItem('sessionsActiveSort', v);
          this._updateFromStore();
          this.refreshActiveOrder();
        });
        this.$watch('recentSort', (v) => {
          localStorage.setItem('recentSort', v);
        });
        this.$watch('recentSince', (v) => {
          localStorage.setItem('recentSince', v);
        });

        // Ensure global SSE handlers are registered
        window.ensureSessionMessages();

        // Build session list from the shared session store.
        this._updateFromStore();
        this.refreshActiveOrder();
        var self = this;
        this._onStoreChanged = function() { self._scheduleStoreSync(); };
        window.addEventListener('sessions:store-changed', this._onStoreChanged);

        // Desktop remounts this component when Sessions is entered; mobile
        // preserves it behind the session-view overlay. Refresh at the shared
        // foreground signal so both navigation models produce a fresh order
        // before the operator starts scanning the list.
        this._onSessionsNavigated = function(e) {
          if (e && e.detail && e.detail.path === '/sessions') {
            self._updateFromStore();
            self.refreshActiveOrder();
          }
        };
        window.addEventListener('app:navigated', this._onSessionsNavigated);
        this._onSessionsVisible = function() {
          if (document.visibilityState === 'visible' && window.location.pathname === '/sessions') {
            self._updateFromStore();
            self.refreshActiveOrder();
          }
        };
        document.addEventListener('visibilitychange', this._onSessionsVisible);

        // Active has already painted above. Adopt the persistent Recent
        // projection and start its one background snapshot only afterwards.
        // The module-level registry listener buffers SSE deltas during this
        // handoff and keeps the projection current even across SPA remounts.
        this._onRecentHistoryChanged = function(e) {
          var detail = (e && e.detail) || {};
          self.recent = detail.rows || [];
          self.recentLoading = !!detail.loading;
          self.recentError = detail.error || '';
          if (self.recent.length && window.Autonomy && window.Autonomy.sessionContributions) {
            window.Autonomy.sessionContributions.load(
              self.recent.map(function(s) { return s.session_id; })
            );
          }
        };
        window.addEventListener('recent-sessions:changed', this._onRecentHistoryChanged);
        this._onRecentHistoryChanged({ detail: _recentHistory });
        // Active is the primary above-the-fold surface. Wait for its initial
        // roster request to settle, then yield a frame for that list to paint
        // before beginning the independent history bootstrap. This is a
        // sequencing boundary, not polling or an arbitrary timer.
        Promise.resolve(window.sessionStoreReady).finally(function() {
          var nextPaint = window.requestAnimationFrame || function(callback) { setTimeout(callback, 0); };
          nextPaint(function() { _loadRecentHistory(); });
        });

        // Launching-tile sweep. _updateFromStore holds the placeholder
        // reconcile + TTL expiry, but it only runs on registry/store events —
        // and after a reconnect those can go quiet, leaving a placeholder
        // stuck as a "starting up" card. Re-run it on a timer ONLY while a
        // pending tile exists, so every placeholder eventually retires (by its
        // bound real session or the TTL) without needing a fresh event.
        this._launchSweep = setInterval(function() {
          var store = Alpine.store('sessions');
          for (var k in store) {
            if (k.indexOf('pending-') === 0) { self._updateFromStore(); break; }
          }
        }, 10000);

        // Workspace-changes status per session (drives the ⌥ indicator
        // on each session card). Pushed live via the ``worktrees`` SSE
        // topic — the bus replays cached state on registerHandler so
        // the cards paint immediately on mount, and updates flow
        // through the same path within ~5s of the backend monitor
        // detecting a change.
        this._workspaceHandler = function (rows) { self._applyWorkspaceRows(rows); };
        if (typeof window.registerHandler === 'function') {
          window.registerHandler('worktrees', this._workspaceHandler);
        }

        // Per-session CPU/RAM/disk pushed by the resource collector after
        // each ~6s tick. One REST hydrate backfills sparkline history;
        // every subsequent point arrives on the 'resources' topic and is
        // appended to the client-side ring buffer.
        this._resourceHandler = function (rows) { self._applyResourceRows(rows); };
        if (typeof window.registerHandler === 'function') {
          window.registerHandler('resources', this._resourceHandler);
        }
        this._hydrateResources();
        // One-shot fallback for the cold-start window before the
        // backend has emitted its first 'worktrees' broadcast.
        if (!Object.keys(this._workspaceStatusByTmux).length) {
          this._fetchWorkspaceStatus();
        }

        // Fetch workspace registry for the launch dropdown
        this._fetchProjects();
        // Fetch org list for the toolbar filter dropdown
        this._fetchOrgFilterList();

        // Handle new terminal creation from the + button dropdown.
        // Session creation goes through POST /api/session/create, which is the
        // sole creation path. After the session exists, ws_terminal is used
        // only for PTY bridging (attach).
        //   detail.project   → workspace container
        //   detail.type='host' → host session
        //   (empty)          → default autonomy container
        this._onCreateTerminal = async (e) => {
          var detail = e.detail || {};
          this._creating = true;
          // ── Optimistic launching tile (auto-yfcoc PART 2) ──
          // Render a tile the instant the workspace is picked — before the
          // POST returns — so the operator gets immediate feedback and the
          // session appears in the Launching section at the top of the list.
          // Reconciled away in _updateFromStore when the real
          // session:registry row for this workspace arrives, or expired
          // after the TTL if creation failed.
          var isHost = detail.type === 'host' && !detail.project;
          var pendingId = 'pending-' + (
            (window.crypto && crypto.randomUUID)
              ? crypto.randomUUID()
              : String(Date.now()) + '-' + Math.floor(Math.random() * 1e9)
          );
          try {
            var org = null;
            if (detail.project) {
              for (var pi = 0; pi < (this.projects || []).length; pi++) {
                if (this.projects[pi].graph_project === detail.project) {
                  org = this.projects[pi].org || null; break;
                }
              }
            }
            var pstore = window.getSessionStore(pendingId);
            pstore.isLive = true;
            pstore.project = detail.project || '';
            pstore.label = detail.label || (isHost ? 'New host session' : (detail.project || 'New session'));
            pstore.sessionType = isHost ? 'host' : 'container';
            pstore.startedAt = Date.now() / 1000;
            pstore.setupPhase = 'pending';
            pstore.harnessPhase = 'pending';
            pstore.activityState = 'thinking';
            pstore.org = org;
            pstore._launching = true;
            // [lc] Optimistic insert. Emit BEFORE _updateFromStore so the
            // timeline shows the synchronous handler boundary regardless
            // of how fast the rebuild + Alpine reactivity downstream is.
            var L = window.Autonomy && window.Autonomy.lifecycle;
            if (L && L.emit) {
              L.emit({
                sid: pendingId, surface: 'list-card', event: 'optimistic-insert',
                from: null, to: 'pending',
                project: pstore.project, sessionType: pstore.sessionType,
                reason: 'create-terminal handler synchronous insert',
              });
            }
            this._updateFromStore();
          } catch (_) { /* optimistic tile is best-effort */ }
          try {
            var body = {};
            if (detail.project) body.project = detail.project;
            else if (detail.type === 'host') body.type = 'host';
            var res = await fetch('/api/session/create', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify(body),
            });
            if (!res.ok && res.status !== 202) {
              var err = await res.json().catch(function () { return {}; });
              throw new Error(err.error || 'Session create failed');
            }
            var created = await res.json();
            // Bind the optimistic placeholder to the REAL session id the
            // create returned. The reconcile then retires the tile by exact
            // tmux_name (live OR dead) instead of the fuzzy live-only
            // project|type match — which orphaned the tile as a stuck
            // "starting up" card once that session died (the registry never
            // re-fired _updateFromStore to expire it).
            if (created && created.tmux_name) {
              var ph = Alpine.store('sessions')[pendingId];
              if (ph) ph._realSession = created.tmux_name;
            }
          } catch (err) {
            // Create failed — drop the optimistic tile immediately so it
            // doesn't linger as a ghost in the Launching section.
            try {
              delete Alpine.store('sessions')[pendingId];
              this._updateFromStore();
            } catch (_) {}
            console.warn('[sessionsPage] create-terminal failed', err);
          } finally {
            this._creating = false;
          }
        };
        window.addEventListener('create-terminal', this._onCreateTerminal);
      },

      async _fetchProjects() {
        try {
          const data = await fetch('/api/projects').then(r => r.json());
          const projects = Array.isArray(data.projects) ? data.projects : [];
          this.projects = projects;
          // Group by graph_project, preserving first-seen order. Each
          // group's `org` is the resolved identity object returned by
          // /api/projects, ready for the picker header to render.
          const order = [];
          const byOrg = {};
          const orgObjs = {};
          for (const p of projects) {
            const slug = (p.org && p.org.slug) || p.graph_project || 'other';
            if (!byOrg[slug]) {
              byOrg[slug] = [];
              order.push(slug);
              orgObjs[slug] = p.org || {slug, name: slug, color: '#4b5563', favicon: null, initial: '?', resolved: false};
            }
            byOrg[slug].push(p);
          }
          this.orgGroups = order.map(slug => ({org: orgObjs[slug], projects: byOrg[slug]}));
        } catch (e) {
          console.warn('[sessionsPage] projects fetch error', e);
          this.projects = [];
          this.orgGroups = [];
        }
      },

      _updateFromStore() {
        var allSessions = Alpine.store('sessions');
        // ── Reconcile / expire optimistic launching tiles (auto-yfcoc) ──
        // Drop a pending tile once a real (non-pending) live session for the
        // same workspace has arrived via registry, or after _LAUNCH_TTL_S
        // if creation never produced one (failed POST, lost broadcast).
        var _LAUNCH_TTL_S = 120;
        var nowS = Date.now() / 1000;
        var realByKey = {};
        for (var rid in allSessions) {
          if (rid.indexOf('pending-') === 0) continue;
          var rs = allSessions[rid];
          if (rs && rs.isLive) {
            var rk = (rs.project || '') + '|' + (rs.sessionType || '');
            var rst = rs.startedAt || 0;
            if (realByKey[rk] === undefined || rst > realByKey[rk]) realByKey[rk] = rst;
          }
        }
        var _lcLib = window.Autonomy && window.Autonomy.lifecycle;
        for (var pid in allSessions) {
          if (pid.indexOf('pending-') !== 0) continue;
          var p = allSessions[pid];
          var pk = (p.project || '') + '|' + (p.sessionType || '');
          // Deterministic retire: the create POST bound this tile to its real
          // session id, so retire as soon as that row exists in the store —
          // LIVE OR DEAD. This is the stuck-tile fix: the fuzzy match below
          // only sees LIVE reals, so a session that died left the tile
          // orphaned as a "starting up" card until a manual refresh.
          var bound = !!(p._realSession && allSessions[p._realSession]);
          // A real session created at/after this tile (5s skew tolerance)
          // means the launch resolved — retire the placeholder.
          var matched = bound || (realByKey[pk] !== undefined && realByKey[pk] >= (p.startedAt || 0) - 5);
          var expired = (nowS - (p.startedAt || 0)) > _LAUNCH_TTL_S;
          if (matched || expired) {
            if (_lcLib && _lcLib.emit) {
              _lcLib.emit({
                sid: pid, surface: 'list-card', event: 'reconcile',
                from: 'pending', to: null,
                reason: bound ? ('bound real session ' + p._realSession + ' present')
                  : (matched ? 'real session arrived for ' + pk : 'TTL expired (' + _LAUNCH_TTL_S + 's)'),
                matched: matched, expired: expired,
                pending_started_at: p.startedAt || 0,
              });
            }
            delete allSessions[pid];
          }
        }
        var all = [];
        for (var id in allSessions) {
          var s = allSessions[id];
          if (!s.isLive && !s._resuming) continue;
          // Newest DISPLAYABLE entry, not merely the newest. Trailing
          // internal markers (codex_task_complete) and tool_use/tool_result
          // rows carry no operator-visible text; taking them verbatim blanked
          // the preview the moment a turn completed, and because lastEntry was
          // truthy the `|| s.lastMessage` fallback below never fired.
          var lastEntry = null;
          for (var _i = s.entries.length - 1; _i >= 0; _i--) {
            var _e = s.entries[_i];
            if (!_e || _e.internal) continue;
            if (!(_e.content || '').trim()) continue;
            lastEntry = _e; break;
          }
          var sizeVal = s.sizeMB ? parseFloat(s.sizeMB) : 0;
          var hasData = s.entries.length > 0 || (s.sizeMB && sizeVal > 0) || s.entryCount > 0 || s.lastActivity > 0;
          var role = _deriveRole(s);
          all.push({
            id: id,
            session_id: id,
            project: s.project || '',
            label: s.label || '',
            role: role,
            is_live: s.isLive,
            created_at: s.startedAt || 0,
            last_activity: s.lastActivity || 0,
            last_input_at: s.lastInputAt || 0,
            latest: lastEntry ? (lastEntry.content || '').slice(0, 150) : (s.lastMessage || ''),
            type: s.sessionType || 'terminal',
            session_type: _deriveSessionType(s),
            tmux_session: id,
            bead_id: s.beadId || '',
            entry_count: s.entryCount || s.entries.length,
            context_tokens: s.contextTokens || 0,
            topics: s.topics || [],
            nag_enabled: s.nagEnabled || false,
            nag_interval: s.nagInterval || 15,
            nag_message: s.nagMessage || '',
            dispatch_nag_enabled: s.dispatchNagEnabled || false,
            activity_state: s.activityState || 'idle',
            org: s.org || null,
            // Live sessions are not "resumable" in the same sense as
            // dead-and-ingested sessions; they are already alive. The
            // store row carries no JSONL-path concept for the resumable
            // check. Dead recent sessions get their resumable flag from
            // the server-side DAO (dao_sessions.get_recent_sessions
            // computes it from file_path existence).
            resumable: s.resumable === true,
            // auto-ngis4: harness + model from session store
            // (graph://553c7437-036 icon-rail).
            harness: s.harness || null,
            model: s.model || null,
            // auto-yfcoc: startup-phase fields. The store carries
            // camelCase; the partial expects snake_case so we map
            // here. Defaults match the store-creation defaults so
            // missing fields don't break the lifecycle derivation.
            // ``resolved`` (carried as-is from the registry payload)
            // is the migration-artifact guard for pre-existing
            // sessions whose setup_phase + harness_phase columns
            // defaulted to 'pending' from the schema migration —
            // see lifecycle.js for the bypass.
            // Unified startup FSM. NULL = not in launching.
            startup_state: s.startupState || null,
            state: s.state || null,
            attention: s.attention || null,
            harness_state: s.harnessState || {},
            resolved: s.resolved === true,
            // auto-ja51w: transient sub-phase progress from
            // SessionMonitor.update_phase(progress=...). Drives the
            // "Preparing workspace N/M" chip label in phaseChip().
            // Omitted when no progress is active.
            phase_progress: s.phaseProgress || null,
            // auto-yfcoc PART 2: optimistic launching-tile marker. Drives
            // placement into the Launching section (and the chip suppresses
            // navigation until the real session reconciles in).
            _launching: s._launching === true,
            _hasData: !!hasData,
          });
        }
        if (all.length > 0 || !this.loading) {
          // Sort by creation time descending — stable across navigations
          all.sort(function(a, b) { return (b.created_at || 0) - (a.created_at || 0); });
          var interactiveTypes = ['terminal', 'chatwith', 'host', 'container'];
          this.interactive = all.filter(s =>
            s.session_id && interactiveTypes.indexOf(s.type) !== -1
          );
          if (window.Autonomy && window.Autonomy.sessionContributions) {
            window.Autonomy.sessionContributions.load(
              this.interactive.map(function(s) { return s.session_id; })
            );
          }
          if (this._activeOrderNeedsInitialData) this.refreshActiveOrder();
          else this._reconcileActiveOrder();
          this.loading = false;

          // [lc] phase-change + launching-membership transitions. We diff
          // the lifecycle summary AND the _isLaunching verdict per
          // session-id against the previous _updateFromStore pass so the
          // capture timeline shows EXACTLY when the card moved phases or
          // crossed the Launching/Active boundary. Memo is forgotten when
          // a session leaves `interactive` (so re-entry logs a fresh
          // baseline).
          if (_lcLib && _lcLib.emit && _lcLib.summarize) {
            if (!this._lcMemo) this._lcMemo = {};
            var seen = {};
            for (var ii = 0; ii < this.interactive.length; ii++) {
              var ss = this.interactive[ii];
              var sid = ss.session_id;
              seen[sid] = true;
              var cur = _lcLib.summarize(ss);
              var launchingNow = !!ss._launching || cur.visible;
              var prev = this._lcMemo[sid];
              if (!prev) {
                _lcLib.emit({
                  sid: sid, surface: 'list-card', event: 'registry-update',
                  from: null, to: cur.state,
                  setup_phase: cur.setup_phase, harness_phase: cur.harness_phase,
                  resolved: cur.resolved, chip_label: cur.chip, chip_tone: cur.tone,
                  _launching: launchingNow,
                  reason: 'first seen this session in interactive list',
                });
              } else {
                if (prev.state !== cur.state) {
                  _lcLib.emit({
                    sid: sid, surface: 'list-card', event: 'phase-change',
                    from: prev.state, to: cur.state,
                    setup_phase: cur.setup_phase, harness_phase: cur.harness_phase,
                    resolved: cur.resolved, chip_label: cur.chip, chip_tone: cur.tone,
                  });
                }
                if (prev.chip !== cur.chip || prev.tone !== cur.tone) {
                  _lcLib.emit({
                    sid: sid, surface: 'list-card', event: 'chip-render',
                    from: prev.chip || null, to: cur.chip || null,
                    chip_tone: cur.tone,
                  });
                }
                if (prev.launching !== launchingNow) {
                  _lcLib.emit({
                    sid: sid, surface: 'list-card', event: 'launching-membership',
                    from: prev.launching ? 'launching' : 'active',
                    to: launchingNow ? 'launching' : 'active',
                    visible: cur.visible, _launching: !!ss._launching,
                  });
                }
              }
              cur.launching = launchingNow;
              this._lcMemo[sid] = cur;
            }
            for (var msid in this._lcMemo) {
              if (!seen[msid]) {
                _lcLib.emit({
                  sid: msid, surface: 'list-card', event: 'reconcile',
                  from: this._lcMemo[msid].state, to: null,
                  reason: 'left interactive list',
                });
                delete this._lcMemo[msid];
              }
            }
          }
        }
      },

      // Update _workspaceStatusByTmux from a worktree-rows payload.
      // Aggregate per tmux_name — a session can own multiple repo
      // worktrees (autonomy + enterprise_ng), and the indicator
      // should reflect the union of dirty + commits-ahead.
      _applyWorkspaceRows(rows) {
        if (!Array.isArray(rows)) return;
        var by = {};
        for (var i = 0; i < rows.length; i++) {
          var r = rows[i];
          var key = r.session_name;
          if (!key) continue;
          if (!by[key]) by[key] = { dirtyCount: 0, commitsAhead: 0 };
          if (r.is_dirty) by[key].dirtyCount += (typeof r.dirty_count === 'number')
            ? r.dirty_count
            : (r.dirty_files || []).length;
          by[key].commitsAhead += r.commits_ahead || 0;
        }
        for (var k in by) {
          by[k].hasChanges = (by[k].dirtyCount + by[k].commitsAhead) > 0;
        }
        this._workspaceStatusByTmux = by;
      },
      async _fetchWorkspaceStatus() {
        try {
          var rows = await fetch('/api/worktrees').then(function (r) {
            return r.ok ? r.json() : [];
          });
          this._applyWorkspaceRows(rows);
        } catch (_) {
          // Best-effort; sessions list shouldn't crash on a worktree fetch hiccup.
        }
      },

      navigate(s) {
        if (window.actionSheet.isOpen()) return;
        // Recent cards carry project wrapped in brackets for legacy display
        // (dao/sessions.py:get_recent_sessions). Strip them before routing.
        var proj = (s.project || '').replace(/^\[|\]$/g, '') || 'session';
        var path = '/session/' + encodeURIComponent(proj) + '/' + s.session_id
          + '?tmux=' + encodeURIComponent(s.session_id);
        navigateTo(path);
      },

      showSessionActions(s) {
        var label = s.label || s.session_id.slice(0, 12);
        var tmux = s.session_id;
        var actions = [];
        var self = this;

        if (s.is_live) {
          // ── Live session: nag toggle, nag presets, destructive close ──
          var nagLabel = s.nag_enabled ? 'Disable Nag' : 'Enable Nag (15m)';
          actions.push({
            label: nagLabel,
            handler: function() {
              fetch('/api/session/' + encodeURIComponent(tmux) + '/nag', {
                method: s.nag_enabled ? 'DELETE' : 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: s.nag_enabled ? undefined : JSON.stringify({enabled: true, interval: 15}),
              });
            },
          });

          if (s.nag_enabled) {
            [5, 15, 30, 60].forEach(function(mins) {
              if (mins !== s.nag_interval) {
                actions.push({
                  label: 'Nag every ' + mins + 'm',
                  handler: function() { _setNag(tmux, mins, s.nag_message); },
                });
              }
            });
          }

          actions.push({
            label: 'Restart Session',
            handler: async function() {
              if (!tmux) return;
              var res = await fetch(
                '/api/session/' + encodeURIComponent(tmux) + '/restart',
                { method: 'POST' }
              );
              if (!res.ok) {
                var err = await res.json().catch(function() { return {}; });
                console.warn('[sessionsPage] restart failed', err.error || res.statusText);
              }
            },
          });

          actions.push({
            label: 'Close Session',
            style: 'destructive',
            handler: async function() {
              if (tmux) {
                await fetch('/api/terminal/' + encodeURIComponent(tmux) + '/kill', { method: 'POST' });
              }
            },
          });
        } else {
          // ── Dead (recent) session: Open + optional Resume ──
          actions.push({
            label: 'Open',
            handler: function() { self.navigate(s); },
          });
          if (s.resumable) {
            actions.push({
              label: 'Resume',
              handler: function() {
                // resumeSession expects an $event object; synthesize one.
                var fake = {preventDefault: function(){}, stopPropagation: function(){}};
                self.resumeSession(s, fake);
              },
            });
          }
        }

        window.actionSheet.show({title: label, actions: actions});
      },

      destroy() {
        if (this._launchSweep) { clearInterval(this._launchSweep); this._launchSweep = null; }
        if (this._onStoreChanged) window.removeEventListener('sessions:store-changed', this._onStoreChanged);
        if (this._onRecentHistoryChanged) window.removeEventListener('recent-sessions:changed', this._onRecentHistoryChanged);
        if (this._onSessionsNavigated) window.removeEventListener('app:navigated', this._onSessionsNavigated);
        if (this._onSessionsVisible) document.removeEventListener('visibilitychange', this._onSessionsVisible);
        if (this._workspaceHandler && typeof window.unregisterHandler === 'function') {
          window.unregisterHandler('worktrees', this._workspaceHandler);
          this._workspaceHandler = null;
        }
        if (this._resourceHandler && typeof window.unregisterHandler === 'function') {
          window.unregisterHandler('resources', this._resourceHandler);
          this._resourceHandler = null;
        }
        if (this._onCreateTerminal) window.removeEventListener('create-terminal', this._onCreateTerminal);
      },
    }));
  });
})();
