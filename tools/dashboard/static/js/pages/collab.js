// Collab hub Alpine component.
// Tabs: Recent notes, Thoughts, Threads, Topics — all fetched from real APIs.
// Registered via alpine:init so it's available when the fragment is injected.

(function () {
  document.addEventListener('alpine:init', () => {
    Alpine.data('collabPage', () => ({
      tab: new URLSearchParams(window.location.search).get('tab') || localStorage.getItem('collabTab') || 'recent',
      recent: [],
      thoughts: [],
      threads: [],
      topics: [],
      loading: true,
      thoughtInput: '',

      async init() {
        const [recentRes, thoughtsRes, threadsRes, topicsRes] = await Promise.all([
          fetch('/api/graph/collab').then(r => r.json()),
          fetch('/api/graph/thoughts').then(r => r.json()),
          fetch('/api/graph/threads?all=1').then(r => r.json()),
          fetch('/api/graph/streams').then(r => r.json()),
        ]);
        this.recent = recentRes.notes || [];
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
      borderClass(item) {
        if (this.isPitfall(item)) return 'type-pitfall';
        if (item.source_type === 'thought') return 'type-thought';
        return '';
      },
      typeLabel(item) {
        if (this.isPitfall(item)) return 'pitfall';
        return item.source_type || 'note';
      },
      typeClass(item) {
        if (this.isPitfall(item)) return 'note-type-pitfall';
        if (item.source_type === 'thought') return 'note-type-thought';
        return 'note-type-note';
      },
    }));
  });
})();
