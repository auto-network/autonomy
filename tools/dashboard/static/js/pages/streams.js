// Streams landing page Alpine component.
// Shows all tag streams ranked by note count with descriptions and last active time.
// Registered via alpine:init so it's available when the fragment is injected.

(function () {
  document.addEventListener('alpine:init', () => {
    Alpine.data('streamsPage', () => ({
      streams: [],
      loaded: false,

      init() {
        fetch('/api/graph/streams')
          .then(r => r.json())
          .then(d => { this.streams = d.streams || []; this.loaded = true; });
      },

      timeAgo(iso) {
        if (!iso) return '';
        const diff = Date.now() - new Date(iso).getTime();
        const mins = Math.floor(diff / 60000);
        if (mins < 60) return mins + 'm';
        const hrs = Math.floor(mins / 60);
        if (hrs < 24) return hrs + 'h';
        return Math.floor(hrs / 24) + 'd';
      }
    }));
  });
})();
