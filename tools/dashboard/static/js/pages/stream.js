// Stream page Alpine component.
// Shows a chronological feed of graph notes matching a tag.
// Registered via alpine:init so it's available when the fragment is injected.

(function () {
  document.addEventListener('alpine:init', () => {
    Alpine.data('streamPage', () => ({
      tag: '',
      items: [],
      loaded: false,
      zoom: 'normal',

      init() {
        this.tag = decodeURIComponent(window.location.pathname.split('/stream/')[1] || '');
        fetch('/api/graph/stream/' + encodeURIComponent(this.tag))
          .then(r => r.json())
          .then(d => { this.items = d.items || []; this.loaded = true; });
      },

      recencyText(ts) {
        if (!ts) return '';
        var diff = (Date.now() - new Date(ts).getTime()) / 1000;
        if (diff < 3600) return Math.floor(diff / 60) + 'm';
        if (diff < 86400) return Math.floor(diff / 3600) + 'h';
        return Math.floor(diff / 86400) + 'd';
      },

      recencyColor(ts) {
        if (!ts) return 'text-gray-600';
        var hours = (Date.now() - new Date(ts).getTime()) / 3600000;
        if (hours < 1) return 'text-green-400';
        if (hours < 6) return 'text-amber-400';
        if (hours < 48) return 'text-red-400';
        return 'text-gray-600';
      }
    }));
  });
})();
