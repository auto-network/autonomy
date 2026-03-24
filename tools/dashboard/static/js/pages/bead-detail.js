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
      bead: null,
      primer: null,

      // Derived
      isApproved: false,
      isRunning: false,
      approving: false,
      runDir: null,
      dispatchRun: null,
      experienceReport: null,
      depBlockers: [],
      depDependents: [],

      formatTs(ts) {
        return _formatTs(ts);
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

      async approve() {
        this.approving = true;
        try {
          const res = await fetch(`/api/bead/${this.id}/approve`, { method: 'POST' });
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
        if (!this.id) {
          this.state = 'notFound';
          return;
        }

        try {
          // Fetch bead + primer concurrently
          const [beadRes, primerRes] = await Promise.all([
            fetch(`/api/dao/bead/${this.id}`),
            fetch(`/api/primer/${this.id}`),
          ]);

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

          // Primer may fail (graph not available, bead not indexed) — that's OK
          try {
            const primerData = await primerRes.json();
            if (primerData && !primerData.error) {
              this.primer = primerData;
            }
          } catch (_) {
            // Primer unavailable — sections will not render (all x-if guarded by primer &&)
          }

          this.state = 'ready';

          // Fetch dependency data (blockers + dependents)
          try {
            const depRes = await fetch(`/api/bead/${this.id}/deps`);
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
