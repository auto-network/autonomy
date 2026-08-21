(function () {
  function testingPage() {
    return {
      orgs: [],
      repositories: [],
      selectedOrg: 'autonomy',
      selectedRepository: '',
      summary: null,
      loading: false,
      error: '',
      refreshedAt: null,
      refreshTimer: null,

      async init() {
        await this.loadOrgs();
        await this.loadSummary();
        this.refreshTimer = window.setInterval(() => {
          if (!document.hidden && !this.loading) this.loadSummary(true);
        }, 5000);
      },

      async loadOrgs() {
        try {
          const response = await window.Autonomy.fetch('/api/orgs');
          if (!response.ok) throw new Error('Organizations unavailable');
          const value = await response.json();
          this.orgs = (value.orgs || [])
            .map(row => (row && row.org && row.org.slug) || row.slug)
            .filter(Boolean);
        } catch (_error) {
          this.orgs = ['autonomy'];
        }
        if (!this.orgs.length) this.orgs = ['autonomy'];
        if (!this.orgs.includes(this.selectedOrg)) this.selectedOrg = this.orgs[0];
        this.orgs.sort((left, right) => {
          if (left === this.selectedOrg) return -1;
          if (right === this.selectedOrg) return 1;
          return left.localeCompare(right);
        });
      },

      async onOrgChange() {
        this.selectedRepository = '';
        this.repositories = [];
        await this.loadSummary();
      },

      async loadSummary(silent = false) {
        if (!silent) this.loading = true;
        this.error = '';
        const query = new URLSearchParams({ recent_limit: '20', ranked_limit: '10' });
        if (this.selectedRepository) query.set('repository', this.selectedRepository);
        try {
          const response = await fetch('/api/plugins/testing/summary?' + query.toString(), {
            headers: { 'X-Graph-Org': this.selectedOrg },
          });
          const value = await response.json();
          if (!response.ok) throw new Error(value.error || 'Testing activity unavailable');
          this.summary = value;
          this.repositories = value.repositories || [];
          this.refreshedAt = new Date();
        } catch (error) {
          this.error = error.message || String(error);
        } finally {
          this.loading = false;
        }
      },

      count(status) { return (this.summary.runs.status_counts || {})[status] || 0; },
      percent(value) { return value == null ? '—' : (value * 100).toFixed(value === 1 ? 0 : 1) + '%'; },
      duration(value) {
        const seconds = Number(value || 0);
        if (seconds < 60) return seconds.toFixed(1) + 's';
        if (seconds < 3600) return (seconds / 60).toFixed(1) + 'm';
        return (seconds / 3600).toFixed(1) + 'h';
      },
      age(value) {
        const seconds = Math.max(0, Date.now() / 1000 - Number(value || 0));
        if (seconds < 60) return Math.floor(seconds) + 's';
        if (seconds < 3600) return Math.floor(seconds / 60) + 'm';
        return (seconds / 3600).toFixed(1) + 'h';
      },
      when(value) {
        const date = new Date(value);
        return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
      },
      refreshed() {
        return this.refreshedAt
          ? this.refreshedAt.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' })
          : 'waiting';
      },
      shortRepository(value) { return value.length > 48 ? '…' + value.slice(-47) : value; },
      resourceText(resources) {
        return Object.entries(resources || {})
          .map(([name, amount]) => amount + ' ' + name + (name === 'tests' ? ' slots' : ''))
          .join(' · ') || 'no slots';
      },
      selectorText(run) {
        const selectors = run.selectors || run.selector_preview || [];
        const preview = selectors.map(value => value.split('/').pop()).join(', ');
        const omitted = Math.max(0, Number(run.selector_count || 0) - selectors.length);
        return (preview || 'selectors unavailable') + (omitted ? ' +' + omitted + ' more' : '');
      },
      etaText(run) {
        const estimate = Number(run.estimated_seconds || 0);
        if (!estimate) return 'ETA unknown · waiting for timing history';
        const unknown = Number(run.unknown_selector_count || 0);
        const uncertain = Number(run.uncertain_selector_count || unknown);
        const floor = Number(run.estimated_low_seconds || estimate);
        if (uncertain) return 'at least ~' + this.duration(floor) + ' observed known work · ' + uncertain + ' selectors incomplete';
        const elapsed = Math.max(0, Date.now() / 1000 - Number(run.acquired_at || 0));
        const remaining = estimate - elapsed;
        const low = Math.max(0, Number(run.estimated_low_seconds || estimate) - elapsed);
        const high = Math.max(0, Number(run.estimated_high_seconds || estimate) - elapsed);
        const range = low !== high ? ' · observed ' + this.duration(low) + '–' + this.duration(high) + ' left' : '';
        return remaining > 0
          ? '~' + this.duration(remaining) + ' remaining' + range + ' · ' + this.duration(elapsed) + ' elapsed'
          : 'estimate exceeded · ' + this.duration(elapsed) + ' elapsed';
      },
      etaProgress(run) {
        const estimate = Number(run.estimated_seconds || 0);
        if (!estimate || Number(run.uncertain_selector_count || run.unknown_selector_count || 0)) return 0;
        const elapsed = Math.max(0, Date.now() / 1000 - Number(run.acquired_at || 0));
        return Math.min(96, Math.max(3, Math.round(elapsed / estimate * 100)));
      },
      featureLabel(value) {
        return String(value || 'unknown').replace(/^command_/, '').replaceAll('_', ' ');
      },
      featureEntries() {
        return Object.entries((this.summary.telemetry || {}).feature_counts || {})
          .map(([name, count]) => ({ name, count }))
          .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name))
          .slice(0, 10);
      },
      versionEntries() {
        return Object.entries((this.summary.telemetry || {}).versions || {})
          .map(([name, count]) => ({ name, count }))
          .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
      },
      behaviorTotal(name) { return (((this.summary.telemetry || {}).behavior || {}).totals || {})[name] || 0; },
      behaviorSessions() { return ((((this.summary.telemetry || {}).behavior || {}).sessions) || []).slice(0, 8); },
      sequenceText(sequence) {
        return (sequence || []).map(item => this.featureLabel(item.event)).join('  →  ');
      },
      recentUsage() { return ((this.summary.telemetry || {}).recent_events || []).slice(0, 6); },
      errorCounts() {
        return Object.entries((this.summary.operational_errors || {}).counts || {})
          .map(([name, count]) => ({ name, count }))
          .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name))
          .slice(0, 6);
      },
      recentErrors() { return ((this.summary.operational_errors || {}).recent || []).slice(0, 6); },
      chronologicalRuns() { return [...(this.summary.recent_runs || [])].reverse(); },
      runHeight(run) {
        const maximum = Math.max(1, ...(this.summary.recent_runs || []).map(item => Number(item.duration_seconds || 0)));
        return Math.max(12, Math.round((Number(run.duration_seconds || 0) / maximum) * 100));
      },
    };
  }

  window.testingPage = testingPage;
  document.addEventListener('alpine:init', () => window.Alpine.data('testingPage', testingPage));
})();
