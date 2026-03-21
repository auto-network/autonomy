// Sessions page Alpine component.
// Registered via alpine:init so it's available when the fragment is injected and
// Alpine.initTree() is called by the SPA router.
//
// Subscribes to SSE 'sessions' topic for active sessions (pushed by session monitor).
// Fetches recent sessions from DAO endpoint on init + periodic refresh.

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
        // Subscribe to SSE 'sessions' topic for active session data
        this._sseHandler = (data) => {
          const mapped = (Array.isArray(data) ? data : []).map(_mapActive);
          this.interactive = mapped.filter(s =>
            s.tmux_session && (s.type === 'terminal' || s.type === 'chatwith')
          );
          this.agents = mapped.filter(s =>
            !s.tmux_session || (s.type !== 'terminal' && s.type !== 'chatwith')
          ).filter(s =>
            !s.session_id.startsWith('agent-') && s.project !== 'subagents'
          );
          this.loading = false;
        };
        window.registerHandler('sessions', this._sseHandler);

        // Fetch recent sessions (from graph.db, not monitor)
        this._fetchRecent();
        this._recentTimer = setInterval(() => this._fetchRecent(), 30000);

        // If SSE hasn't delivered data yet, do one initial fetch for active sessions
        // as a fallback (e.g., first page load before monitor broadcasts)
        if (!window._sseCache || !window._sseCache.sessions) {
          this._fetchActiveFallback();
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
          if (data && (!this.interactive.length && !this.agents.length)) {
            this._sseHandler(data);
          }
        } catch (e) {
          console.warn('[sessionsPage] fallback fetch error', e);
        }
      },

      destroy() {
        if (this._sseHandler) {
          window.unregisterHandler('sessions', this._sseHandler);
        }
        if (this._recentTimer) clearInterval(this._recentTimer);
      },
    }));
  });
})();
