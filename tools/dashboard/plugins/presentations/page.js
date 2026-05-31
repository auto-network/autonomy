(function () {
  'use strict';

  function parsePresentPath(pathname) {
    var parts = String(pathname || '').split('/').filter(Boolean);
    if (parts[0] !== 'present') {
      return { mode: 'library', designId: '', slideIndex: 0 };
    }
    if (!parts[1]) {
      return { mode: 'library', designId: '', slideIndex: 0 };
    }
    return {
      mode: 'deck',
      designId: decodeURIComponent(parts[1]),
      slideIndex: parseSlideIndex(parts[2]),
    };
  }

  function parseSlideIndex(raw) {
    if (!raw) return 0;
    var value = String(raw).replace(/^slide-?/i, '');
    var num = Number.parseInt(value, 10);
    return Number.isFinite(num) && num > 0 ? num - 1 : 0;
  }

  function selectedVariant(design) {
    var variants = (design && design.variants) || [];
    if (!variants.length) return null;
    var selected = variants.filter(function (variant) { return !!variant.selected; });
    return selected.length ? selected[selected.length - 1] : variants[variants.length - 1];
  }

  function fixtureScript(design) {
    var raw = (design && design.fixture) || '{}';
    var parsed = null;
    try {
      parsed = JSON.parse(raw);
    } catch (_) {
      parsed = null;
    }
    if (parsed && parsed.states && typeof parsed.states === 'object') {
      var keys = Object.keys(parsed.states);
      if (keys.length) {
        return '<script>window.FIXTURE=' + JSON.stringify(parsed.states[keys[0]]) +
          ';window.FIXTURE_STATES=' + JSON.stringify(parsed.states) + ';<\/script>';
      }
    }
    return '<script>window.FIXTURE=' + raw + ';<\/script>';
  }

  function runtimeScript(initialIndex) {
    return '<script>(function(){' +
      'var initial=' + Number(initialIndex || 0) + ';' +
      'var slides=[];' +
      'var root=null;' +
      'function meaningful(el){return el&&["SCRIPT","STYLE","TEMPLATE"].indexOf(el.tagName)<0&&((el.textContent||"").trim()||el.children.length);}' +
      'function collect(){' +
      'root=document.getElementById("present-scroll-root")||document.body;' +
      'var found=[].slice.call(root.querySelectorAll("[data-slide],[data-present-slide],.slide,.present-slide,section,article"));' +
      'if(!found.length){found=[].slice.call(root.children).filter(meaningful);}' +
      'slides=found.length?found:[document.body];' +
      'slides.forEach(function(el,i){el.classList.add("present-runtime-slide");el.setAttribute("data-present-index",String(i));});' +
      '}' +
      'function post(type,detail){try{parent.postMessage(Object.assign({type:type},detail||{}),"*");}catch(_){}}' +
      'function activeIndex(){var best=0,bestDist=Infinity,base=root?root.getBoundingClientRect().top:0;slides.forEach(function(el,i){var r=el.getBoundingClientRect();var d=Math.abs(r.top-base);if(d<bestDist){bestDist=d;best=i;}});return best;}' +
      'var scheduled=false;' +
      'function report(){scheduled=false;post("present:active",{index:activeIndex(),count:slides.length});}' +
      'function schedule(){if(scheduled)return;scheduled=true;requestAnimationFrame(report);}' +
      'function go(index,behavior){index=Math.max(0,Math.min(slides.length-1,Number(index)||0));var el=slides[index];if(el&&root){root.scrollTo({top:el.offsetTop||0,behavior:behavior||"smooth"});}setTimeout(report,80);}' +
      'window.__presentGoToSlide=go;' +
      'window.addEventListener("message",function(event){var data=event.data||{};if(data.type==="present:goto")go(data.index);});' +
      'collect();root.addEventListener("scroll",schedule,{passive:true});window.addEventListener("resize",schedule);' +
      'requestAnimationFrame(function(){post("present:ready",{count:slides.length});go(initial,"auto");});' +
      '})();<\/script>';
  }

  function iframeDocument(design, initialIndex) {
    var variant = selectedVariant(design);
    var html = (variant && variant.html) || '<main></main>';
    return '<!DOCTYPE html><html><head><meta charset="utf-8">' +
      '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">' +
      '<script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"><\/script>' +
      '<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3/dist/cdn.min.js"><\/script>' +
      '<style>' +
      'html,body{height:100%;margin:0;overflow:hidden;background:#020617;color:#e5e7eb;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;}' +
      '#present-scroll-root{height:100%;overflow-y:auto;overflow-x:hidden;scroll-snap-type:y mandatory;scroll-behavior:smooth;}' +
      '.present-runtime-slide{min-height:100svh;scroll-snap-align:start;scroll-snap-stop:always;box-sizing:border-box;}' +
      '@supports not (min-height:100svh){.present-runtime-slide{min-height:100vh;}}' +
      '*{box-sizing:border-box;}' +
      '</style>' +
      fixtureScript(design) +
      '</head><body><main id="present-scroll-root">' + html + '</main>' + runtimeScript(initialIndex) + '</body></html>';
  }

  function setPath(designId, index) {
    if (!designId) return;
    var path = '/present/' + encodeURIComponent(designId) + '/' + (Number(index || 0) + 1);
    if (window.location.pathname !== path) {
      history.replaceState(history.state || {}, '', path);
    }
  }

  function maybeRegisterAlpine() {
    if (!window.Alpine || !window.Alpine.data) return false;
    window.Alpine.data('presentationsPage', function () {
      return {
        mode: 'library',
        loading: false,
        error: '',
        decks: [],
        deck: {},
        design: null,
        activeSlide: 0,
        slideCount: 1,
        _messageHandler: null,
        _keydownHandler: null,

        get slideIndexes() {
          return Array.from({ length: Math.max(1, this.slideCount) }, function (_, idx) { return idx; });
        },

        init: function () {
          var route = parsePresentPath(window.location.pathname);
          this.mode = route.mode;
          if (route.mode === 'deck') {
            this.loadDeck(route.designId, route.slideIndex);
          } else {
            this.loadLibrary();
          }
        },

        destroy: function () {
          if (this._messageHandler) window.removeEventListener('message', this._messageHandler);
          if (this._keydownHandler) window.removeEventListener('keydown', this._keydownHandler);
          this._messageHandler = null;
          this._keydownHandler = null;
        },

        loadLibrary: async function () {
          this.mode = 'library';
          this.loading = true;
          this.error = '';
          try {
            var response = await window.Autonomy.fetch('/api/presentations/decks');
            var data = await response.json();
            this.decks = Array.isArray(data.decks) ? data.decks : [];
          } catch (err) {
            this.error = 'Could not load decks';
          } finally {
            this.loading = false;
          }
        },

        loadDeck: async function (designId, initialIndex) {
          this.mode = 'deck';
          this.loading = true;
          this.error = '';
          this.activeSlide = Math.max(0, Number(initialIndex) || 0);
          try {
            var response = await window.Autonomy.fetch('/api/presentations/deck/' + encodeURIComponent(designId));
            var data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Deck not found');
            this.deck = data.deck || {};
            this.design = data.design || null;
            this.slideCount = Math.max(1, Number(this.deck.slide_count) || 1);
            await this.$nextTick();
            this.injectIframe(this.activeSlide);
            window.Autonomy.fetch('/api/presentations/deck/' + encodeURIComponent(designId) + '/shown', { method: 'POST' });
          } catch (err) {
            this.error = err && err.message ? err.message : 'Could not load deck';
          } finally {
            this.loading = false;
          }
        },

        injectIframe: function (initialIndex) {
          var iframe = document.getElementById('present-iframe');
          if (!iframe || !this.design) return;
          if (this._messageHandler) window.removeEventListener('message', this._messageHandler);
          if (this._keydownHandler) window.removeEventListener('keydown', this._keydownHandler);
          var self = this;
          this._messageHandler = function (event) {
            if (event.source !== iframe.contentWindow) return;
            var data = event.data || {};
            if (data.type === 'present:ready') {
              self.slideCount = Math.max(1, Number(data.count) || self.slideCount);
            } else if (data.type === 'present:active') {
              self.slideCount = Math.max(1, Number(data.count) || self.slideCount);
              self.activeSlide = Math.max(0, Math.min(self.slideCount - 1, Number(data.index) || 0));
              setPath(self.deck.design_id, self.activeSlide);
            }
          };
          this._keydownHandler = function (event) {
            if (event.altKey || event.ctrlKey || event.metaKey) return;
            if (event.key === 'ArrowDown' || event.key === 'ArrowRight' || event.key === 'PageDown' || event.key === ' ') {
              event.preventDefault();
              self.nextSlide();
            } else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft' || event.key === 'PageUp') {
              event.preventDefault();
              self.prevSlide();
            }
          };
          window.addEventListener('message', this._messageHandler);
          window.addEventListener('keydown', this._keydownHandler);
          var doc = iframe.contentDocument || iframe.contentWindow.document;
          doc.open();
          doc.write(iframeDocument(this.design, initialIndex));
          doc.close();
        },

        goToSlide: function (index, opts) {
          var next = Math.max(0, Math.min(this.slideCount - 1, Number(index) || 0));
          this.activeSlide = next;
          var iframe = document.getElementById('present-iframe');
          if (iframe && iframe.contentWindow) {
            if (typeof iframe.contentWindow.__presentGoToSlide === 'function') {
              iframe.contentWindow.__presentGoToSlide(next);
            } else {
              iframe.contentWindow.postMessage({ type: 'present:goto', index: next }, '*');
            }
          }
          if (!opts || !opts.skipUrl) setPath(this.deck.design_id, next);
        },

        nextSlide: function () {
          this.goToSlide(this.activeSlide + 1);
        },

        prevSlide: function () {
          this.goToSlide(this.activeSlide - 1);
        },

        openDeck: function (deck) {
          var id = deck && (deck.design_id || deck.key);
          if (id) navigateTo('/present/' + encodeURIComponent(id));
        },

        openDesign: function (deck) {
          var id = deck && (deck.latest_revision_id || deck.design_id || deck.key);
          if (id) navigateTo('/design/' + encodeURIComponent(id));
        },

        openSession: function (deck) {
          if (deck && deck.creator_session_id) navigateTo('/session/autonomy/' + encodeURIComponent(deck.creator_session_id));
        },

        creatorLabel: function (deck) {
          return (deck && (deck.creator_session_label || deck.creator_session_id)) || 'unknown session';
        },

        formatDate: function (value) {
          if (!value) return 'not shown';
          var date = new Date(value);
          if (Number.isNaN(date.getTime())) return value;
          return date.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
        },
      };
    });
    return true;
  }

  if (!maybeRegisterAlpine()) {
    document.addEventListener('alpine:init', maybeRegisterAlpine, { once: true });
  }

  window.PresentationsTest = {
    parsePresentPath: parsePresentPath,
    parseSlideIndex: parseSlideIndex,
    iframeDocument: iframeDocument,
  };
})();
