(function () {
  'use strict';

  function resolvePresenceRuntime() {
    if (typeof window !== 'undefined' && window.Presence) return window.Presence;
    if (typeof require === 'function') {
      try { return require('../../static/js/surface-presence.js'); }
      catch (_) { return null; }
    }
    return null;
  }

  var _presenceRuntime = resolvePresenceRuntime();

  function parsePresentPath(pathname) {
    var parts = String(pathname || '').split('/').filter(Boolean);
    if (parts[0] !== 'present' && parts[0] !== 'presentations') {
      return { mode: 'library', designId: '', slideIndex: 0 };
    }
    if (parts[0] === 'presentations' && !parts[1]) {
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

  function presentSurfaceId(pathname) {
    var route = parsePresentPath(pathname);
    return route.mode === 'deck' && route.designId
      ? 'presentations:' + route.designId
      : 'presentations:library';
  }

  function selectedVariant(design) {
    var variants = (design && design.variants) || [];
    if (!variants.length) return null;
    var selected = variants.filter(function (variant) { return !!variant.selected; });
    return selected.length ? selected[selected.length - 1] : variants[variants.length - 1];
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch];
    });
  }

  function extractHtmlParts(html) {
    html = html || '';
    if (!/(<!doctype|<html|<body|<head)/i.test(html)) {
      return { head: '', body: html };
    }
    if (typeof DOMParser === 'undefined') {
      var headMatch = html.match(/<head[^>]*>([\s\S]*?)<\/head>/i);
      var bodyMatch = html.match(/<body[^>]*>([\s\S]*?)<\/body>/i);
      return {
        head: headMatch ? headMatch[1] : '',
        body: bodyMatch ? bodyMatch[1] : html,
      };
    }
    try {
      var doc = new DOMParser().parseFromString(html, 'text/html');
      return {
        head: doc.head ? doc.head.innerHTML : '',
        body: doc.body ? doc.body.innerHTML : html,
      };
    } catch (_) {
      return { head: '', body: html };
    }
  }

  function participantColor(participantId) {
    if (_presenceRuntime && typeof _presenceRuntime.participantColor === 'function') {
      return _presenceRuntime.participantColor(participantId);
    }
    return 'hsl(0 70% 60%)';
  }

  function participantInitial(participant) {
    if (participant && participant.display_initial) {
      return String(participant.display_initial).trim().charAt(0).toUpperCase() || '?';
    }
    var label = (participant && (participant.participant_label || participant.participant_id)) || '?';
    return String(label).trim().charAt(0).toUpperCase() || '?';
  }

  function topbarPresenceHtml(participants, ownerPresence) {
    var rows = Array.isArray(participants) ? participants.filter(Boolean) : [];
    if (ownerPresence && ownerPresence.participant_id) {
      rows = [ownerPresence].concat(rows.filter(function (p) {
        return p && p.participant_id !== ownerPresence.participant_id;
      }));
    }
    if (!rows.length) return '';
    var visible = rows.slice(0, 4);
    var html = '<div class="nx-avatar-stack present-topbar-presence" data-testid="present-topbar-presence">';
    for (var i = 0; i < visible.length; i += 1) {
      var p = visible[i] || {};
      var id = p.participant_id || '';
      var liveText = p.is_owner ? (p.is_live ? (p.is_active ? 'active owner' : 'live owner') : 'owner offline') : (p.state || 'present');
      var title = (p.participant_label || id || 'participant') + ' — ' + liveText;
      if (p.intent) title += ' — ' + p.intent;
      var classes = 'nx-avatar present-topbar-avatar';
      if (p.is_owner) classes += ' present-topbar-owner';
      if (p.is_owner && p.is_live) classes += ' is-live';
      if (p.is_owner && !p.is_live) classes += ' is-offline';
      html += '<span class="' + classes + '" style="background:' + escapeHtml(participantColor(id)) + '" title="' +
        escapeHtml(title) + '">' + escapeHtml(participantInitial(p)) + '</span>';
    }
    if (rows.length > visible.length) {
      html += '<span class="nx-avatar-overflow">+' + String(rows.length - visible.length) + '</span>';
    }
    html += '</div>';
    return html;
  }

  function topbarHtml(deck, activeSlide, slideCount, participants, ownerPresence) {
    deck = deck || {};
    var subtitle = deck.subtitle ? '<span>' + escapeHtml(deck.subtitle) + '</span>' : '';
    return '<div class="present-topbar">' +
      '<a href="/presentations" class="present-topbar-back" title="Deck library" aria-label="Deck library">' +
      '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg>' +
      '</a>' +
      '<div class="present-topbar-title"><strong>' + escapeHtml(deck.name || 'Untitled deck') + '</strong>' + subtitle + '</div>' +
      '<div class="present-topbar-side">' +
      topbarPresenceHtml(participants, ownerPresence) +
      '<div class="present-topbar-count">' + String(Number(activeSlide || 0) + 1) + ' / ' + String(slideCount || 1) + '</div>' +
      '</div>' +
      '</div>';
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function progressIndexFromPosition(clientX, rect, slideCount) {
    var count = Math.max(1, Number(slideCount) || 1);
    if (count <= 1) return 0;
    if (!rect || !Number.isFinite(rect.left) || !Number.isFinite(rect.width) || rect.width <= 0) {
      return 0;
    }
    var ratio = clamp((Number(clientX) - rect.left) / rect.width, 0, 1);
    return clamp(Math.round(ratio * (count - 1)), 0, count - 1);
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
      'function activeIndex(){var base=root?root.getBoundingClientRect().top:0;var best=0,bestDist=Infinity;for(var i=0;i<slides.length;i++){var r=slides[i].getBoundingClientRect();if(r.top<=base+1&&r.bottom>base+1){return i;}var d=Math.abs(r.top-base);if(d<bestDist){bestDist=d;best=i;}}return best;}' +
      // Slides taller than the viewport get no snap alignment (free scroll — the
      // reader can rest at the bottom), and their presence relaxes the container
      // from mandatory to proximity snapping: mobile engines otherwise yank the
      // scroller back to the slide top, making tall-slide bottoms unreadable.
      'function applySnapMode(){if(!root)return;var vh=root.clientHeight||window.innerHeight;var anyTall=false;slides.forEach(function(el){var tall=el.offsetHeight>vh+8;if(tall)anyTall=true;el.style.scrollSnapAlign=tall?"none":"";el.style.scrollSnapStop=tall?"normal":"";});root.style.scrollSnapType=anyTall?"y proximity":"";}' +
      'function reveal(index){index=Math.max(0,Math.min(slides.length-1,Number(index)||0));var el=slides[index];if(el){el.classList.add("in");el.classList.add("present-runtime-active");}}' +
      'var scheduled=false;' +
      'function report(){scheduled=false;var index=activeIndex();reveal(index);post("present:active",{index:index,count:slides.length});}' +
      'function schedule(){if(scheduled)return;scheduled=true;requestAnimationFrame(report);}' +
      'function go(index,behavior){index=Math.max(0,Math.min(slides.length-1,Number(index)||0));reveal(index);var el=slides[index];if(el&&root){root.scrollTo({top:el.offsetTop||0,behavior:behavior||"smooth"});}setTimeout(report,80);}' +
      'window.__presentGoToSlide=go;' +
      'window.addEventListener("message",function(event){var data=event.data||{};if(data.type==="present:goto")go(data.index);});' +
      'collect();applySnapMode();root.addEventListener("scroll",schedule,{passive:true});' +
      'window.addEventListener("resize",function(){applySnapMode();schedule();});' +
      'window.addEventListener("load",applySnapMode);setTimeout(applySnapMode,600);' +
      'requestAnimationFrame(function(){post("present:ready",{count:slides.length});go(initial,"auto");});' +
      '})();<\/script>';
  }

  function iframeDocument(design, initialIndex) {
    var variant = selectedVariant(design);
    var html = (variant && variant.html) || '<main></main>';
    var parts = extractHtmlParts(html);
    return '<!DOCTYPE html><html><head><meta charset="utf-8">' +
      '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">' +
      // Cosmetic defaults (dark chrome, light ink, sans) go BEFORE the deck's
      // own <head> so an unstyled deck still reads, but any deck stylesheet
      // wins the cascade — injecting these after deck styles silently
      // overrode deck body color/font and made light decks unreadable.
      '<style>' +
      'html,body{background:#020617;color:#e5e7eb;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;}' +
      '</style>' +
      parts.head +
      '<script src="/static/vendor/tailwind-browser.min.js"><\/script>' +
      '<script defer src="/static/vendor/alpine.min.js"><\/script>' +
      // Structural pager rules stay AFTER deck styles — the scroll root and
      // snap mechanics must hold regardless of what the deck ships.
      '<style>' +
      'html,body{height:100%;margin:0;overflow:hidden;}' +
      '#present-scroll-root{height:100%;overflow-y:auto;overflow-x:hidden;scroll-snap-type:y mandatory;scroll-behavior:smooth;}' +
      '.present-runtime-slide{min-height:100svh;scroll-snap-align:start;scroll-snap-stop:always;box-sizing:border-box;}' +
      '.present-runtime-slide+.present-runtime-slide{margin-top:var(--present-slide-gap,48px);}' +
      '@supports not (min-height:100svh){.present-runtime-slide{min-height:100vh;}}' +
      '*{box-sizing:border-box;}' +
      '</style>' +
      fixtureScript(design) +
      '</head><body><main id="present-scroll-root">' + parts.body + '</main>' + runtimeScript(initialIndex) + '</body></html>';
  }

  function setPath(designId, index) {
    if (!designId) return;
    var base = window.location.pathname.indexOf('/presentations/') === 0 ? '/presentations/' : '/present/';
    var path = base + encodeURIComponent(designId) + '/' + (Number(index || 0) + 1);
    if (window.location.pathname !== path) {
      history.replaceState(history.state || {}, '', path);
    }
  }

  function maybeRegisterAlpine() {
    if (!window.Alpine || !window.Alpine.data) return false;
    window.Alpine.data('presentationsPage', function () {
      var state = {
        mode: 'library',
        loading: false,
        error: '',
        decks: [],
        deck: {},
        ownerPresence: null,
        design: null,
        activeSlide: 0,
        slideCount: 1,
        _messageHandler: null,
        _keydownHandler: null,
        _progressScrubbing: false,
        _progressPointerId: null,
        _progressRect: null,
        _lastHapticSlide: null,

        participantColor: participantColor,
        participantInitial: participantInitial,

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
            this.ownerPresence = data.owner_presence || null;
            this.design = data.design || null;
            this.slideCount = Math.max(1, Number(this.deck.slide_count) || 1);
            await this.$nextTick();
            this.updateTopbar();
            this.injectIframe(this.activeSlide);
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
              self.updateTopbar();
            } else if (data.type === 'present:active') {
              self.slideCount = Math.max(1, Number(data.count) || self.slideCount);
              self.activeSlide = Math.max(0, Math.min(self.slideCount - 1, Number(data.index) || 0));
              self.updateTopbar();
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
          var changed = next !== this.activeSlide;
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
          this.updateTopbar();
          if (changed && (!opts || opts.haptic !== false)) this.hapticTick(next);
        },

        hapticTick: function (index) {
          if (this._lastHapticSlide === index) return;
          this._lastHapticSlide = index;
          try {
            if (window.navigator && typeof window.navigator.vibrate === 'function') {
              window.navigator.vibrate(8);
            }
          } catch (_) {}
        },

        startProgressScrub: function (event) {
          var target = event.currentTarget;
          this._progressScrubbing = true;
          this._progressPointerId = event.pointerId;
          this._progressRect = target && target.getBoundingClientRect
            ? target.getBoundingClientRect()
            : null;
          if (target && typeof target.setPointerCapture === 'function') {
            try { target.setPointerCapture(event.pointerId); } catch (_) {}
          }
          this.updateProgressScrub(event);
        },

        moveProgressScrub: function (event) {
          if (!this._progressScrubbing) return;
          if (this._progressPointerId != null && event.pointerId !== this._progressPointerId) return;
          this.updateProgressScrub(event);
        },

        endProgressScrub: function (event) {
          if (this._progressScrubbing
              && (this._progressPointerId == null || event.pointerId === this._progressPointerId)) {
            this.updateProgressScrub(event);
          }
          this._progressScrubbing = false;
          this._progressPointerId = null;
          this._progressRect = null;
        },

        updateProgressScrub: function (event) {
          var rect = this._progressRect;
          if (!rect) {
            var progress = document.querySelector('.present-progress');
            rect = progress && progress.getBoundingClientRect ? progress.getBoundingClientRect() : null;
          }
          var next = progressIndexFromPosition(event.clientX, rect, this.slideCount);
          if (next !== this.activeSlide) {
            this.goToSlide(next);
          }
        },

        updateTopbar: function () {
          if (!window.Autonomy || typeof window.Autonomy.setTopbar !== 'function') return;
          window.Autonomy.setTopbar({
            html: topbarHtml(
              this.deck,
              this.activeSlide,
              this.slideCount,
              this.participants,
              this.ownerPresence,
            ),
          });
        },

        nextSlide: function () {
          this.goToSlide(this.activeSlide + 1);
        },

        prevSlide: function () {
          this.goToSlide(this.activeSlide - 1);
        },

        openDeck: function (deck) {
          var id = deck && (deck.design_id || deck.key);
          if (id) navigateTo('/presentations/' + encodeURIComponent(id));
        },

        openDesign: function (deck) {
          var id = deck && (deck.latest_revision_id || deck.design_id || deck.key);
          if (id) navigateTo('/design/' + encodeURIComponent(id));
        },

        openSession: function (deck) {
          if (deck && deck.creator_session_id) navigateTo('/session/autonomy/' + encodeURIComponent(deck.creator_session_id));
        },

        creatorLabel: function (deck) {
          return (deck && (deck.creator_session_label || deck.creator_session_id)) || '';
        },

        formatDate: function (value) {
          if (!value) return 'not shown';
          var raw = String(value);
          var date = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(raw) ? raw : raw.replace(' ', 'T') + 'Z');
          if (Number.isNaN(date.getTime())) return value;
          return date.toLocaleString([], {
            month: 'short',
            day: 'numeric',
            hour: 'numeric',
            minute: '2-digit',
            timeZone: 'UTC',
            timeZoneName: 'short',
          });
        },
      };
      if (_presenceRuntime && typeof _presenceRuntime.alpine === 'function') {
        return _presenceRuntime.alpine({
          surfaceId: presentSurfaceId(window.location && window.location.pathname),
          onParticipantChange: function () {
            this.updateTopbar();
          },
        }, state);
      }
      return state;
    });
    return true;
  }

  if (!maybeRegisterAlpine()) {
    document.addEventListener('alpine:init', maybeRegisterAlpine, { once: true });
  }

  window.PresentationsTest = {
    parsePresentPath: parsePresentPath,
    parseSlideIndex: parseSlideIndex,
    presentSurfaceId: presentSurfaceId,
    progressIndexFromPosition: progressIndexFromPosition,
    iframeDocument: iframeDocument,
    extractHtmlParts: extractHtmlParts,
    topbarHtml: topbarHtml,
  };
})();
