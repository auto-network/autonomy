(function () {
  document.addEventListener('alpine:init', () => {
    Alpine.data('searchPage', () => ({
      query: '',
      results: [],
      loaded: false,
      zoom: localStorage.getItem('searchZoom') || 'normal',

      init() {
        var params = new URLSearchParams(window.location.search);
        this.query = params.get('q') || '';
        if (!this.query) { this.loaded = true; return; }
        fetch('/api/search?q=' + encodeURIComponent(this.query) + '&group=1&limit=50')
          .then(r => r.json())
          .then(d => { this.results = Array.isArray(d) ? d : (d.results || []); this.loaded = true; });
      },

      typeColor(t) {
        if (t === 'note' || t === 'thought') return '#eab308';
        if (t === 'session') return '#22c55e';
        if (t === 'docs') return '#3b82f6';
        return '#64748b';
      },

      typeName(t) {
        return ({ thought: 'Thought', session: 'Session', docs: 'Docs' })[t] || 'Note';
      },

      parseTags(tagsStr) {
        if (!tagsStr) return [];
        if (Array.isArray(tagsStr)) return tagsStr;
        try { return JSON.parse(tagsStr); } catch { return []; }
      },

      highlightTerms(text, q) {
        if (!q || !text) return '';
        var terms = q.split(/\s+/).filter(t => t.length > 2);
        var escaped = terms.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
        var snip = text.slice(0, 200);
        var safe = this._esc(snip);
        if (!escaped.length) return safe;
        var re = new RegExp('(' + escaped.join('|') + ')', 'gi');
        return safe.replace(re, '<mark style="background:#78350f;color:#fde68a;padding:0 2px;border-radius:2px">$1</mark>');
      },

      _esc(s) {
        var d = document.createElement('div');
        d.textContent = s;
        return d.innerHTML;
      }
    }));
  });
})();
