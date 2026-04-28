// Collab hub Alpine component.
// Tabs: Recent (all notes), Curated (collab-tagged), Thoughts, Threads, Topics.
// Registered via alpine:init so it's available when the fragment is injected.

(function () {
  // Migration: legacy localStorage value 'recent' is preserved as Recent
  // (which now means "all recent notes"). Users who had selected the
  // collab-tagged surface keep the same tab key — its meaning changed
  // from "collab-tagged only" to "everything recent". Anyone who actually
  // wants the curated view can click Curated; we don't auto-rewrite.
  function initialTab() {
    var fromUrl = new URLSearchParams(window.location.search).get('tab');
    if (fromUrl) return fromUrl;
    var stored = localStorage.getItem('collabTab');
    return stored || 'recent';
  }

  document.addEventListener('alpine:init', () => {
    Alpine.data('collabPage', () => ({
      tab: initialTab(),
      recent: [],
      curated: [],
      thoughts: [],
      threads: [],
      topics: [],
      loading: true,
      thoughtInput: '',

      async init() {
        const [recentRes, curatedRes, thoughtsRes, threadsRes, topicsRes] = await Promise.all([
          fetch('/api/graph/notes?since=7d&limit=50').then(r => r.json()),
          fetch('/api/graph/collab').then(r => r.json()),
          fetch('/api/graph/thoughts').then(r => r.json()),
          fetch('/api/graph/threads?all=1').then(r => r.json()),
          fetch('/api/graph/streams').then(r => r.json()),
        ]);
        this.recent = recentRes.notes || [];
        this.curated = curatedRes.notes || [];
        this.thoughts = thoughtsRes.thoughts || [];
        this.threads = threadsRes.threads || [];
        this.topics = topicsRes.streams || [];
        this.loading = false;
      },

      setTab(t) {
        this.tab = t;
        localStorage.setItem('collabTab', t);
      },

      async captureThought() {
        const text = this.thoughtInput.trim();
        if (!text) return;
        this.thoughtInput = '';
        await fetch('/api/graph/thought', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text }),
        });
        const res = await fetch('/api/graph/thoughts').then(r => r.json());
        this.thoughts = res.thoughts || [];
      },

      formatDate(iso) {
        if (!iso) return '';
        return iso.slice(0, 10);
      },
      formatAuthor(author) {
        if (!author) return '';
        return author.replace('terminal:', '');
      },
      isPitfall(item) {
        return (item.tags || []).indexOf('pitfall') !== -1;
      },
      // Resolve the visual "type bucket" for a card. Pitfall (a tag, not a
      // type) wins because it's the highest-signal classification. Otherwise
      // dispatch on source_type. Anything we don't recognise falls back to
      // 'note' styling.
      typeBucket(item) {
        if (this.isPitfall(item)) return 'pitfall';
        const t = (item.source_type || 'note').toLowerCase();
        const known = [
          'note', 'thought', 'session', 'agent-run',
          'conversation', 'docs', 'status', 'musing',
        ];
        return known.indexOf(t) !== -1 ? t : 'note';
      },
      borderClass(item) {
        const b = this.typeBucket(item);
        return b === 'note' ? '' : 'type-' + b;
      },
      typeLabel(item) {
        return this.typeBucket(item);
      },
      typeClass(item) {
        return 'note-type-' + this.typeBucket(item);
      },
      // Prefer the explicit short_description over the legacy preview slice
      // (a 140-char content prefix). Empty string means "render nothing".
      previewText(item) {
        return item.short_description || item.preview || '';
      },
    }));
  });
})();
