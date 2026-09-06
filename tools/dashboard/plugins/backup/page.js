// Backup plugin — frontend Alpine factory (bead auto-x6f7z).
//
// The page answers, in order: am I safe now (tier health hero), when
// was the last good copy (run stream, failures pinned), what exactly
// was captured (per-store table), does restore actually work (drill
// panel) — the §0 viewer analysis in graph://7c45a180-345.
//
// Reads (plain same-origin fetch; the API serves persisted Settings
// only — no request ever touches the backup destination):
//   GET /api/backup/summary
//   GET /api/backup/runs?tier=&limit=
//   GET /api/backup/drills
//
// Polling, not SSE, for the scaffold: run rows change at most once an
// hour; a 60 s poll is honest and cheap. The run-report reconciler
// bead (auto-yj2wa) owns any move to setting.changed events.

function backupRelativeAge(seconds) {
  if (seconds === null || seconds === undefined) return 'never';
  const s = Math.max(0, Math.floor(seconds));
  if (s < 90) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 172800) return `${(s / 3600).toFixed(1)}h ago`;
  return `${(s / 86400).toFixed(1)}d ago`;
}

function backupBytes(n) {
  let v = Number(n || 0);
  for (const unit of ['B', 'KB', 'MB', 'GB', 'TB']) {
    if (v < 1024 || unit === 'TB') {
      return unit === 'B' ? `${v.toFixed(0)} B` : `${v.toFixed(1)} ${unit}`;
    }
    v /= 1024;
  }
  return `${v.toFixed(1)} TB`;
}

function backupPage() {
  return {
    loading: true,
    error: '',
    summary: null,
    runs: [],
    drills: [],
    storeTier: 'hourly',
    _timer: null,

    async init() {
      await this.refresh();
      this._timer = setInterval(() => this.refresh(), 60_000);
    },
    destroy() {
      if (this._timer) clearInterval(this._timer);
    },

    drillMessage: '',

    // Start an on-demand restore drill; 409 = one already running.
    async runDrill() {
      this.drillMessage = '';
      try {
        const res = await fetch('/api/backup/drill', { method: 'POST' });
        if (res.status === 409) {
          this.drillMessage = 'A drill is already running.';
        } else if (res.status === 403) {
          this.drillMessage = 'Drills need operator authority.';
        } else if (!res.ok) {
          this.drillMessage = 'Could not start the drill.';
        } else {
          this.drillMessage = 'Drill started — restoring the latest snapshot…';
        }
      } catch (e) {
        this.drillMessage = 'Could not start the drill.';
      }
      await this.refresh();
    },

    // The operator's refresh also ingests the latest on-disk run
    // reports first (bounded server-side); a 403 (non-operator) or a
    // reconcile fault must never block rendering the stored state.
    async refreshFromDisk() {
      try {
        await fetch('/api/backup/reconcile', { method: 'POST' });
      } catch (e) { /* stored state still renders */ }
      await this.refresh();
    },

    async refresh() {
      try {
        const [summary, runs, drills] = await Promise.all([
          fetch('/api/backup/summary').then((r) => r.json()),
          fetch('/api/backup/runs?limit=30').then((r) => r.json()),
          fetch('/api/backup/drills?limit=10').then((r) => r.json()),
        ]);
        this.summary = summary;
        this.runs = runs.runs || [];
        this.drills = drills.drills || [];
        this.error = '';
      } catch (e) {
        this.error = 'Could not load backup state.';
      } finally {
        this.loading = false;
      }
    },

    age: backupRelativeAge,
    fmtBytes: backupBytes,

    // Failures pinned first, then newest first — the stream's rule.
    get runStream() {
      const key = (r) => (r.key || '').split(':').pop();
      const failed = this.runs.filter((r) => r.verdict === 'failed');
      const ok = this.runs.filter((r) => r.verdict !== 'failed');
      failed.sort((a, b) => key(b).localeCompare(key(a)));
      ok.sort((a, b) => key(b).localeCompare(key(a)));
      return [...failed, ...ok];
    },

    // The per-store table shows the newest run of the selected tier.
    get storeRows() {
      const run = this.runs
        .filter((r) => (r.key || '').startsWith(`${this.storeTier}:`))
        .sort((a, b) => (b.key || '').localeCompare(a.key || ''))[0];
      return run ? (run.stores || []) : [];
    },

    tierByName(name) {
      return ((this.summary || {}).tiers || []).find((t) => t.tier === name)
        || { tier: name, status: 'stale', age_seconds: null };
    },

    overallLabel() {
      const overall = (this.summary || {}).overall || 'unknown';
      return { ok: 'PROTECTED', stale: 'STALE', failing: 'FAILING' }[overall]
        || overall.toUpperCase();
    },

    statusClass(status) {
      return {
        ok: 'bk-ok',
        complete: 'bk-ok',
        pass: 'bk-ok',
        stale: 'bk-warn',
        skipped: 'bk-warn',
        unknown: 'bk-warn',
        running: 'bk-warn',
        failing: 'bk-bad',
        failed: 'bk-bad',
        fail: 'bk-bad',
        timeout: 'bk-bad',
        missing: 'bk-bad',
        error: 'bk-bad',
      }[status] || 'bk-warn';
    },
  };
}
