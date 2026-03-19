// Search page Alpine component.
// Registered via alpine:init so it's available when the fragment is injected and
// Alpine.initTree() is called by the SPA router.
//
// Reads query and project from URL params on init.
// Fetches results from /api/search?q=X&or=1&limit=20.
//
// state machine: 'idle' → 'loading' → 'ready' | 'empty' | 'error'
//

(function () {
  document.addEventListener('alpine:init', () => {
    Alpine.data('searchPage', () => ({
      state: 'idle',   // 'idle' | 'loading' | 'ready' | 'empty' | 'error'
      errorMsg: '',
      results: [],
      query: '',
      project: '',

      init() {
        const params = new URLSearchParams(window.location.search);
        this.query = params.get('q') || '';
        this.project = params.get('project') || '';
        if (this.query) {
          this._fetch();
        }
      },

      async _fetch() {
        this.state = 'loading';
        try {
          let url = `/api/search?q=${encodeURIComponent(this.query)}&or=1&limit=20`;
          if (this.project) url += `&project=${encodeURIComponent(this.project)}`;
          const res = await fetch(url);
          const data = await res.json();
          if (data && data.error) {
            this.errorMsg = data.error;
            this.state = 'error';
            return;
          }
          const raw = Array.isArray(data) ? data : [];
          this.results = raw.map(r => this._mapResult(r));
          this.state = raw.length === 0 ? 'empty' : 'ready';
        } catch (e) {
          this.errorMsg = e.message || 'Search failed';
          this.state = 'error';
        }
      },

      _mapResult(r) {
        const lines = (r.content || '').split('\n').filter(l => l.trim());
        const headline = (lines[0] || '').slice(0, 120);
        const rest = lines.slice(1).join(' ').slice(0, 200);
        const srcTitle = (r.source_title || '').slice(0, 40);
        return {
          ...r,
          _key: `${r.source_id}-${r.turn_number}`,
          _headline: headline,
          _preview: rest,
          _srcLabel: srcTitle.startsWith('Read all the markdown') ? 'main session' : srcTitle,
          _srcId: (r.source_id || '').slice(0, 12),
          _isUser: r.result_type === 'thought',
          _turn: r.turn_number || '?',
          _href: `/source/${r.source_id}?turn=${r.turn_number}`,
        };
      },

      destroy() {},
    }));
  });
})();
