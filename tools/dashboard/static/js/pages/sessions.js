// Sessions page Alpine component.
// Registered via alpine:init so it's available when the fragment is injected and
// Alpine.initTree() is called by the SPA router.
//
// Reads active sessions from Alpine.store('sessions') (fed by session:registry
// and session:messages SSE topics). Fetches recent sessions from DAO endpoint.

(function () {

  function _formatAge(secs) {
    if (secs < 60) return secs + 's ago';
    return Math.round(secs / 60) + 'm ago';
  }

  function _formatSize(bytes) {
    return (bytes / 1024 / 1024).toFixed(1) + ' MB';
  }

  function _formatProject(project) {
    // Strip /home/<user>/workspace/ path prefix (encoded as dashes)
    const cleaned = project
      .replace(/^-home-[^-]+-workspace-/, '')
      .replace(/^-home-[^-]+-/, '')
      .replace(/^-+/, '');
    return cleaned || 'home';
  }

  function _mapActive(s) {
    return {
      ...s,
      _ageStr: _formatAge(s.age_seconds),
      _sizeStr: _formatSize(s.size_bytes),
      _project: _formatProject(s.project),
    };
  }

  document.addEventListener('alpine:init', () => {
    Alpine.data('sessionsPage', () => ({
      interactive: [],
      agents: [],
      recent: [],
      loading: true,

      init() {
        // Ensure global SSE handlers are registered
        window.ensureSessionMessages();

        // Build session list from store (poll every 500ms)
        this._updateFromStore();
        this._storeWatcher = setInterval(() => this._updateFromStore(), 500);

        // Fetch recent sessions (from graph.db, not monitor)
        this._fetchRecent();
        this._recentTimer = setInterval(() => this._fetchRecent(), 30000);

        // Seed stores from HTTP if SSE hasn't delivered registry yet
        this._fetchActiveFallback();
      },

      _updateFromStore() {
        var allSessions = Alpine.store('sessions');
        var now = Date.now() / 1000;
        var all = [];
        for (var id in allSessions) {
          var s = allSessions[id];
          if (!s.isLive) continue;
          var lastEntry = s.entries.length > 0 ? s.entries[s.entries.length - 1] : null;
          all.push({
            session_id: id,
            project: s.project || '',
            size_bytes: s.sizeMB ? parseFloat(s.sizeMB) * 1048576 : 0,
            age_seconds: Math.round(now - (s.startedAt || now)),
            active: s.lastActivity > 0 && (now - s.lastActivity) < 60,
            latest: lastEntry ? (lastEntry.content || '').slice(0, 150) : '',
            type: s.sessionType || 'terminal',
            tmux_session: s.tmuxSession || '',
          });
        }
        if (all.length > 0 || !this.loading) {
          var mapped = all.map(_mapActive);
          this.interactive = mapped.filter(s =>
            s.tmux_session && (s.type === 'terminal' || s.type === 'chatwith')
          );
          this.agents = mapped.filter(s =>
            !s.tmux_session || (s.type !== 'terminal' && s.type !== 'chatwith')
          ).filter(s =>
            !s.session_id.startsWith('agent-') && s.project !== 'subagents'
          );
          this.loading = false;
        }
      },

      async _fetchRecent() {
        try {
          const data = await fetch('/api/dao/recent_sessions?limit=20').then(r => r.json());
          this.recent = Array.isArray(data) ? data : [];
        } catch (e) {
          console.warn('[sessionsPage] recent fetch error', e);
        }
      },

      async _fetchActiveFallback() {
        try {
          const data = await fetch('/api/dao/active_sessions').then(r => r.json());
          if (Array.isArray(data)) {
            for (var i = 0; i < data.length; i++) {
              var s = data[i];
              var store = window.getSessionStore(s.session_id);
              store.project = s.project || '';
              store.sessionType = s.type || '';
              store.tmuxSession = s.tmux_session || '';
              store.isLive = s.is_live !== false;
              store.startedAt = s.started_at || 0;
            }
          }
        } catch (e) {
          console.warn('[sessionsPage] fallback fetch error', e);
        }
      },

      destroy() {
        if (this._storeWatcher) clearInterval(this._storeWatcher);
        if (this._recentTimer) clearInterval(this._recentTimer);
      },
    }));
  });
})();
