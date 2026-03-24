// Stream page Alpine component.
// Shows a chronological feed of graph notes matching a tag.
// Registered via alpine:init so it's available when the fragment is injected.

(function () {
  document.addEventListener('alpine:init', () => {
    Alpine.data('streamPage', () => ({
      tag: '',
      items: [],
      loaded: false,

      init() {
        this.tag = decodeURIComponent(window.location.pathname.split('/stream/')[1] || '');
        fetch('/api/graph/stream/' + encodeURIComponent(this.tag))
          .then(r => r.json())
          .then(d => { this.items = d.items || []; this.loaded = true; });
      }
    }));
  });
})();
