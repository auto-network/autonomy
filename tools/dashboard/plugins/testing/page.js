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

      async init() {
        await this.loadOrgs();
        await this.loadSummary();
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
      },

      async onOrgChange() {
        this.selectedRepository = '';
        this.repositories = [];
        await this.loadSummary();
      },

      async loadSummary() {
        this.loading = true;
        this.error = '';
        const query = new URLSearchParams({ recent_limit: '20', ranked_limit: '10' });
        if (this.selectedRepository) query.set('repository', this.selectedRepository);
        try {
          const response = await fetch('/api/plugins/testing/summary?' + query.toString(), {
            headers: { 'X-Graph-Org': this.selectedOrg },
          });
          const value = await response.json();
          if (!response.ok) throw new Error(value.error || 'Testing statistics unavailable');
          this.summary = value;
          this.repositories = value.repositories || [];
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
      when(value) {
        const date = new Date(value);
        return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
      },
      shortRepository(value) { return value.length > 48 ? '…' + value.slice(-47) : value; },
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
