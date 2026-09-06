// Backup plugin — frontend Alpine factory.
//
// Reworked 2026-09-06 from the operator's live design review: plain
// language everywhere, a real status table with headers, a "where do
// backups go" panel with credential state and provider size, one
// contents table (hourly/daily capture the same stores — a toggle
// implied a difference that doesn't exist), and an editable
// configuration form with the schema's own descriptions.
//
// Reads: GET /api/backup/summary (now incl. destinations),
//        /api/backup/runs, /api/backup/drills, /api/backup/config
// Writes (operator authority): POST /api/backup/reconcile,
//        POST /api/backup/drill, PUT /api/backup/config

function backupRelativeAge(seconds) {
  if (seconds === null || seconds === undefined) return 'never';
  const s = Math.max(0, Math.floor(seconds));
  if (s < 90) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 172800) return `${(s / 3600).toFixed(1)} hours ago`;
  return `${(s / 86400).toFixed(1)} days ago`;
}

function backupBytes(n) {
  let v = Number(n || 0);
  if (!v) return '—';
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
    configMeta: { config: {}, fields: {}, editable: false },
    configDraft: {},
    configMessage: '',
    drillMessage: '',
    _timer: null,

    async init() {
      document.title = 'Backup — Autonomy';
      const header = document.querySelector('header');
      if (header) header.classList.add('app-topbar-active');
      await this.refresh();
      this._timer = setInterval(() => this.refresh(), 60_000);
    },
    destroy() {
      if (this._timer) clearInterval(this._timer);
    },

    async refresh() {
      try {
        const [summary, runs, drills, configMeta] = await Promise.all([
          fetch('/api/backup/summary').then((r) => r.json()),
          fetch('/api/backup/runs?limit=30').then((r) => r.json()),
          fetch('/api/backup/drills?limit=10').then((r) => r.json()),
          fetch('/api/backup/config').then((r) => r.json()),
        ]);
        this.summary = summary;
        this.runs = runs.runs || [];
        this.drills = drills.drills || [];
        this.configMeta = configMeta;
        if (!Object.keys(this.configDraft).length) {
          this.configDraft = { ...(configMeta.config || {}) };
        }
        this.error = '';
      } catch (e) {
        this.error = 'Could not load backup state.';
      } finally {
        this.loading = false;
      }
    },

    // Ingest the newest on-disk reports first, then re-read.
    async refreshFromDisk() {
      try {
        await fetch('/api/backup/reconcile', { method: 'POST' });
      } catch (e) { /* stored state still renders */ }
      await this.refresh();
    },

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

    async saveConfig() {
      this.configMessage = '';
      const changed = {};
      for (const [key, value] of Object.entries(this.configDraft)) {
        if (value !== (this.configMeta.config || {})[key]) {
          const meta = (this.configMeta.fields || {})[key] || {};
          changed[key] = (meta.type === 'integer') ? parseInt(value, 10)
            : (meta.type === 'number') ? parseFloat(value)
            : (meta.type === 'boolean') ? (value === true || value === 'true')
            : value;
        }
      }
      if (!Object.keys(changed).length) {
        this.configMessage = 'Nothing changed.';
        return;
      }
      try {
        const res = await fetch('/api/backup/config', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(changed),
        });
        const body = await res.json();
        this.configMessage = res.ok ? 'Saved.'
          : (body.error || 'Save failed.');
      } catch (e) {
        this.configMessage = 'Save failed.';
      }
      await this.refresh();
    },

    age: backupRelativeAge,
    fmtBytes: backupBytes,

    tierRows() {
      return ((this.summary || {}).tiers || []).map((tier) => {
        const last = tier.last_run || {};
        return {
          tier: tier.tier,
          status: tier.status,
          mark: { ok: '✓', stale: '⚠', failing: '✗' }[tier.status] || '?',
          lastGood: this.age(tier.age_seconds),
          size: this.fmtBytes(tier.total_bytes),
          offsite: this.offsiteLabel(tier.offsite, tier.age_seconds),
          reason: this.tierReason(tier, last),
        };
      });
    },

    offsiteLabel(verdict, ageSeconds) {
      if (verdict === 'complete') return '✓ pushed';
      if (verdict === 'failed') return '✗ failed';
      if (verdict === 'skipped') return 'off';
      if (ageSeconds !== null && ageSeconds !== undefined
          && ageSeconds < 900) return 'pushing…';
      return 'unverified';
    },

    tierReason(tier, lastRun) {
      if (tier.status === 'failing') {
        return ((lastRun.failures || [])[0]) || 'last backup failed';
      }
      if (tier.status === 'stale') {
        return tier.age_seconds === null || tier.age_seconds === undefined
          ? 'no backup has completed yet'
          : `nothing since ${this.age(tier.age_seconds)} (limit `
            + `${(tier.stale_after_seconds / 3600).toFixed(0)}h)`;
      }
      return '';
    },

    overallLabel() {
      const overall = (this.summary || {}).overall || 'unknown';
      return { ok: 'PROTECTED', stale: 'ATTENTION', failing: 'FAILING' }[overall]
        || overall.toUpperCase();
    },

    overallExplanation() {
      const problems = this.tierRows()
        .filter((row) => row.reason)
        .map((row) => `${row.tier}: ${row.reason}`);
      if (!problems.length) {
        return 'Both tiers are backing up on schedule.';
      }
      return problems.join(' · ');
    },

    credentialsLabel() {
      const status = ((this.summary || {}).destinations || {}).credentials;
      return {
        ok: 'sealed in the vault · releasable now (vault is unlocked)',
        'vault-cold': 'sealed in the vault · will release when the vault is unlocked',
        unsealed: 'NOT sealed yet — offsite cannot authenticate',
        disabled: 'offsite pushes are switched off',
        unconfigured: 'no offsite provider configured',
      }[status] || 'unknown';
    },

    get runStream() {
      const key = (r) => (r.key || '').split(':').pop();
      const failed = this.runs.filter((r) => r.verdict === 'failed');
      const ok = this.runs.filter((r) => r.verdict !== 'failed');
      failed.sort((a, b) => key(b).localeCompare(key(a)));
      ok.sort((a, b) => key(b).localeCompare(key(a)));
      return [...failed, ...ok];
    },

    // Hourly and daily capture the SAME stores; one table, labeled
    // with the run it came from.
    latestContents() {
      const complete = this.runs
        .filter((r) => r.verdict === 'complete' && (r.stores || []).length)
        .sort((a, b) => (b.key || '').split(':').pop()
          .localeCompare((a.key || '').split(':').pop()));
      return complete[0] || null;
    },

    statusClass(status) {
      return {
        ok: 'bk-ok', complete: 'bk-ok', pass: 'bk-ok',
        '✓ pushed': 'bk-ok',
        stale: 'bk-warn', skipped: 'bk-warn', unknown: 'bk-warn',
        running: 'bk-warn', 'pushing…': 'bk-warn', unverified: 'bk-warn',
        off: 'bk-muted',
        failing: 'bk-bad', failed: 'bk-bad', fail: 'bk-bad',
        timeout: 'bk-bad', missing: 'bk-bad', error: 'bk-bad',
        '✗ failed': 'bk-bad',
      }[status] || 'bk-warn';
    },
  };
}
