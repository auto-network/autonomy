// Bead detail page Alpine component.
// Registered via alpine:init so it's available when the fragment is injected and
// Alpine.initTree() is called by the SPA router.
//
// Reads the bead ID from window.location.pathname (/bead/{id}) on init.
// Fetches bead data from /api/dao/bead/{id} (DAO-backed) and structured primer
// from /api/primer/{id} in parallel.
//
// state machine: 'loading' → 'ready' | 'error' | 'notFound'
//

(function () {
  function _formatTs(ts) {
    if (!ts) return '';
    try {
      return new Date(ts).toLocaleString();
    } catch (_) {
      return ts;
    }
  }

  function _beadIdFromPath() {
    const m = window.location.pathname.match(/^\/bead\/(.+)$/);
    return m ? m[1] : '';
  }

  document.addEventListener('alpine:init', () => {
    Alpine.data('beadDetailPage', () => ({
      // State machine
      state: 'loading',   // 'loading' | 'ready' | 'error' | 'notFound'
      errorMsg: '',

      // Data
      id: '',
      org: '',
      bead: null,
      primer: null,

      // Derived
      isApproved: false,
      isRunning: false,
      approving: false,
      runDir: null,
      dispatchRun: null,
      experienceReport: null,
      authorSession: null,
      depBlockers: [],
      depDependents: [],

      formatTs(ts) {
        return _formatTs(ts);
      },

      orgQuery() {
        return this.org ? '?org=' + encodeURIComponent(this.org) : '';
      },

      creatorIcon(created_by) {
        if (!created_by) return '';
        if (created_by.startsWith('librarian:')) return '📖';
        if (created_by.startsWith('terminal:')) return '💻';
        if (created_by.startsWith('dispatch:')) return '🤖';
        return '🧑';
      },

      creatorLabel(created_by) {
        if (!created_by) return '';
        if (created_by.startsWith('librarian:')) return 'Librarian';
        if (created_by.startsWith('terminal:')) return created_by;
        if (created_by.startsWith('dispatch:')) return created_by;
        return created_by;
      },

      authorValue() {
        if (!this.bead) return '';
        return this.bead.created_by || this.bead.owner || '';
      },

      authorHeading() {
        if (!this.bead) return 'Author';
        return this.bead.created_by ? 'Author' : 'Owner';
      },

      authorDisplay() {
        const author = this.authorValue();
        if (!author) return '';
        const icon = this.creatorIcon(author);
        return icon ? `${icon} ${author}` : author;
      },

      authorSessionId() {
        const author = this.authorValue();
        if (!author || !author.startsWith('terminal:')) return '';
        return author.slice('terminal:'.length);
      },

      authorHref() {
        if (!this.authorSession || !this.authorSession.is_live || !this.authorSession.project) {
          return '';
        }
        return `/session/${this.authorSession.project}/${this.authorSession.session_id}`;
      },

      async hydratePrimer() {
        try {
          const primerRes = await fetch(`/api/primer/${this.id}${this.orgQuery()}`);
          const primerData = await primerRes.json();
          if (primerData && !primerData.error) {
            this.primer = primerData;
          }
        } catch (_) {
          // Primer unavailable — primer-backed sections stay hidden.
        }
      },

      async hydrateAuthorSession() {
        const sessionId = this.authorSessionId();
        if (!sessionId) return;
        try {
          const res = await fetch('/api/dao/active_sessions');
          const rows = await res.json();
          if (!Array.isArray(rows)) return;
          const match = rows.find((row) =>
            row &&
            row.is_live &&
            (row.session_id === sessionId || row.tmux_session === sessionId)
          );
          if (match && match.project) {
            this.authorSession = match;
          }
        } catch (_) {
          // Author link enhancement is best-effort only.
        }
      },

      async approve() {
        this.approving = true;
        try {
          const res = await fetch(`/api/bead/${this.id}/approve${this.orgQuery()}`, { method: 'POST' });
          const data = await res.json();
          if (data.ok) {
            this.isApproved = true;
          } else {
            alert(`Failed to approve: ${data.error}`);
          }
        } catch (e) {
          alert(`Failed to approve: ${e.message}`);
        } finally {
          this.approving = false;
        }
      },

      async init() {
        this.id = _beadIdFromPath();
        this.org = new URLSearchParams(window.location.search).get('org') || '';
        if (!this.id) {
          this.state = 'notFound';
          return;
        }

        try {
          // Primer is optional. Start it immediately, but do not block first paint on it.
          void this.hydratePrimer();
          const beadRes = await fetch(`/api/dao/bead/${this.id}${this.orgQuery()}`);

          if (beadRes.status === 404) {
            this.state = 'notFound';
            return;
          }

          const beadData = await beadRes.json();

          if (beadData && beadData.error) {
            this.errorMsg = beadData.error;
            this.state = 'error';
            return;
          }

          const bead = Array.isArray(beadData) ? beadData[0] : beadData;
          if (!bead) {
            this.state = 'notFound';
            return;
          }

          this.bead = bead;
          this.isApproved = (bead.labels || []).includes('readiness:approved');
          this.isRunning = (bead.labels || []).some(l =>
            l.startsWith('dispatch:running') ||
            l.startsWith('dispatch:launching') ||
            l.startsWith('dispatch:collecting')
          );

          this.state = 'ready';
          void this.hydrateAuthorSession();

          // Fetch dependency data (blockers + dependents)
          try {
            const depRes = await fetch(`/api/bead/${this.id}/deps${this.orgQuery()}`);
            const depData = await depRes.json();
            this.depBlockers = (depData.blockers || []).filter(d => d.dependency_type !== 'parent-child');
            this.depDependents = (depData.dependents || []).filter(d => d.dependency_type !== 'parent-child');
          } catch (_) {}

          // Fetch runDir for all dispatched beads (trace link for completed, live panel for running)
          try {
            const runs = await fetch('/api/dispatch/runs').then(r => r.json());
            const runsList = Array.isArray(runs) ? runs : [];
            const beadRun = runsList.find(r => r.bead_id === this.id);
            if (beadRun) {
              this.runDir = beadRun.dir;
              this.dispatchRun = beadRun;
              if (this.isRunning && window.showLivePanel) {
                showLivePanel(beadRun.dir);
              }
              // Fetch experience report if available
              if (beadRun.has_experience_report) {
                try {
                  const searchRes = await fetch('/api/search?q=' + encodeURIComponent(beadRun.dir) + '&limit=3');
                  const hits = await searchRes.json();
                  const expReport = hits.find(h => h.source_title && h.source_title.includes('Experience Report'));
                  if (expReport) {
                    this.experienceReport = {
                      source_id: expReport.source_id,
                      title: expReport.source_title,
                      preview: (expReport.content || '').slice(0, 200),
                    };
                  }
                } catch (_) {}
              }
            }
          } catch (_) {}
        } catch (e) {
          this.errorMsg = e.message || 'Failed to load bead';
          this.state = 'error';
        }
      },

      destroy() {},
    }));
  });
})();
