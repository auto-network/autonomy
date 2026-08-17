// Organization settings — the container, and the screens that go in it.
//
// One dialog per organization, holding an open-ended list of screens. The
// container knows nothing about any of them: a screen registers a label, a
// loader and a renderer, and the container places it. So adding the next one
// is a registration rather than an edit here.
//
// window.AutonomyOrgSettings.open(slug)         — show it
// window.AutonomyOrgSettings.register(screen)   — add a screen
(function () {
  'use strict';

  var root = window;
  var screens = [];
  var host = null;
  var state = { slug: null, screenId: null, view: 'list' };

  function el(tag, cls, text) {
    var node = root.document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function register(screen) {
    if (!screen || !screen.id || !screen.label) return;
    screens = screens.filter(function (s) { return s.id !== screen.id; });
    screens.push(screen);
    screens.sort(function (a, b) { return (a.order || 0) - (b.order || 0); });
  }

  // ── the container ──────────────────────────────────────────

  function close() {
    if (host && host.parentNode) host.parentNode.removeChild(host);
    host = null;
    state = { slug: null, screenId: null, view: 'list' };
    root.document.removeEventListener('keydown', onKey);
    root.document.body.style.overflow = '';
  }

  function onKey(event) {
    if (event.key !== 'Escape') return;
    // On a narrow screen the first Escape goes back to the list, because
    // that is the enclosing view — closing the whole dialog from a detail
    // screen loses the place the reader was in.
    if (state.view === 'detail' && root.matchMedia('(max-width: 767px)').matches) {
      show('list');
      return;
    }
    close();
  }

  function show(view) {
    state.view = view;
    var dialog = host && host.querySelector('.orgset-dialog');
    if (dialog) dialog.setAttribute('data-view', view);
  }

  function selectScreen(id) {
    state.screenId = id;
    show('detail');
    renderRail();
    renderPane();
  }

  function renderRail() {
    var rail = host && host.querySelector('.orgset-rail');
    if (!rail) return;
    rail.textContent = '';
    screens.forEach(function (screen) {
      var item = el('button', 'orgset-rail-item');
      item.type = 'button';
      item.setAttribute('data-testid', 'orgset-rail-' + screen.id);
      item.setAttribute('aria-current', screen.id === state.screenId ? 'true' : 'false');
      item.appendChild(el('span', '', screen.label));
      var badge = el('span', 'orgset-count');
      badge.setAttribute('data-testid', 'orgset-count-' + screen.id);
      item.appendChild(badge);
      item.addEventListener('click', function () { selectScreen(screen.id); });
      rail.appendChild(item);
    });
  }

  function setCount(screenId, text, tone) {
    var badge = host && host.querySelector('[data-testid="orgset-count-' + screenId + '"]');
    if (!badge) return;
    badge.textContent = text == null ? '' : String(text);
    badge.className = 'orgset-count' + (tone ? ' orgset-count-' + tone : '');
  }

  function renderPane() {
    var pane = host && host.querySelector('.orgset-pane');
    if (!pane) return;
    var screen = screens.filter(function (s) { return s.id === state.screenId; })[0];
    pane.textContent = '';
    if (!screen) {
      pane.appendChild(el('div', 'orgset-empty', 'Select a screen.'));
      return;
    }
    pane.appendChild(el('div', 'orgset-loading', 'Loading…'));
    var forSlug = state.slug;
    var forScreen = screen.id;
    Promise.resolve()
      .then(function () { return screen.render(state.slug); })
      .then(function (node) {
        // The reader may have moved on while this was in flight. Rendering
        // into a pane that now shows something else is how a stale answer
        // appears under a fresh heading.
        if (state.slug !== forSlug || state.screenId !== forScreen) return;
        pane.textContent = '';
        pane.appendChild(node);
        if (screen.count) {
          var c = screen.count();
          setCount(forScreen, c && c.text, c && c.tone);
        }
      })
      .catch(function (err) {
        if (state.slug !== forSlug || state.screenId !== forScreen) return;
        pane.textContent = '';
        pane.appendChild(el('div', 'orgset-error', String((err && err.message) || err)));
      });
  }

  function open(slug) {
    if (!slug) return;
    close();
    state.slug = slug;
    state.screenId = screens.length ? screens[0].id : null;

    host = el('div', 'orgset-overlay');
    host.setAttribute('data-testid', 'org-settings');
    host.addEventListener('click', function (event) {
      if (event.target === host) close();
    });

    var dialog = el('div', 'orgset-dialog');
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-label', 'Settings for ' + slug);
    // Desktop shows both panes and ignores this; narrow reads it to decide
    // which single pane is up.
    dialog.setAttribute('data-view', 'list');

    var header = el('div', 'orgset-header');
    var back = el('button', 'orgset-back', '‹ Screens');
    back.type = 'button';
    back.setAttribute('data-testid', 'orgset-back');
    back.addEventListener('click', function () { show('list'); });
    header.appendChild(back);
    header.appendChild(el('h2', 'orgset-title', slug));
    var closer = el('button', 'orgset-close', '×');
    closer.type = 'button';
    closer.setAttribute('aria-label', 'Close settings');
    closer.setAttribute('data-testid', 'orgset-close');
    closer.addEventListener('click', close);
    header.appendChild(closer);
    dialog.appendChild(header);

    var body = el('div', 'orgset-body');
    body.appendChild(el('nav', 'orgset-rail'));
    body.appendChild(el('div', 'orgset-pane'));
    dialog.appendChild(body);
    host.appendChild(dialog);
    root.document.body.appendChild(host);
    root.document.body.style.overflow = 'hidden';
    root.document.addEventListener('keydown', onKey);

    renderRail();
    // Desktop wants a screen already showing; narrow wants the list, which
    // IS its first screen. Both start with the same selection so that
    // widening the window never lands on an empty pane.
    if (!root.matchMedia('(max-width: 767px)').matches) show('detail');
    renderPane();
  }

  root.AutonomyOrgSettings = {
    open: open,
    close: close,
    register: register,
    screens: function () { return screens.slice(); },
  };

  // ── first screen: workspaces, and what they still need here ──

  var lastHealth = null;

  function healthNode(report) {
    var wrap = el('div', 'orgset-workspaces');

    var frame = el('div', 'orgset-frame');
    frame.textContent = 'Checked from ' + (report.asked_in || 'this process') + '.';
    wrap.appendChild(frame);

    var list = report.workspaces || [];
    if (!list.length) {
      wrap.appendChild(el('div', 'orgset-empty',
        'This organization declares no workspaces.'));
      return wrap;
    }

    list.forEach(function (ws) {
      var card = el('section', 'rounded-lg border border-gray-700 bg-gray-900/50 p-3 mb-2');
      card.setAttribute('data-testid', 'orgset-workspace-' + ws.id);

      var head = el('div', 'flex items-center justify-between gap-2 mb-1');
      head.appendChild(el('span', 'text-sm text-gray-200 font-medium', ws.name || ws.id));
      var tag = el('span', 'orgset-count ' + (ws.ready ? 'orgset-count-ready' : 'orgset-count-blocking'),
                   ws.ready ? 'Ready' : String((ws.blocking || []).length || '?'));
      tag.setAttribute('data-testid', 'orgset-workspace-state-' + ws.id);
      head.appendChild(tag);
      card.appendChild(head);

      // Three groups, never one list. "Everything declared is present" and
      // "this can run" are different questions, and printed together the
      // reader learns to treat all of it as noise.
      [['blocking', 'Needed here', 'text-red-300'],
       ['unanswerable', 'Cannot be answered from here', 'text-amber-300'],
       ['advisory', 'Missing, but it still runs', 'text-gray-400']
      ].forEach(function (group) {
        var items = ws[group[0]] || [];
        if (!items.length) return;
        card.appendChild(el('div', 'text-xs mt-2 mb-1 ' + group[2], group[1]));
        items.forEach(function (f) {
          var row = el('div', 'text-xs text-gray-400 pl-2 mb-1');
          row.appendChild(el('div', '', f.what));
          row.appendChild(el('div', 'text-gray-500', f.at));
          card.appendChild(row);
        });
      });

      if (ws.ready && !(ws.advisory || []).length) {
        card.appendChild(el('div', 'text-xs text-gray-500',
          'Everything it declares is present here.'));
      }
      wrap.appendChild(card);
    });
    return wrap;
  }

  register({
    id: 'workspaces',
    label: 'Workspaces',
    order: 10,
    render: function (slug) {
      return fetch('/api/orgs/' + encodeURIComponent(slug) + '/workspaces/health', {
        headers: { 'Accept': 'application/json' },
      }).then(function (res) {
        return res.json().then(function (body) {
          // Surface what the server said rather than a status code. A
          // reader who is told "502" has to go and find out what happened;
          // the server already knows and said so.
          if (!res.ok) throw new Error((body && body.error) || ('HTTP ' + res.status));
          return body;
        });
      }).then(function (body) {
        lastHealth = body;
        return healthNode(body);
      });
    },
    count: function () {
      if (!lastHealth) return null;
      var blocked = (lastHealth.workspaces || []).filter(function (w) {
        return !w.ready;
      }).length;
      return blocked
        ? { text: blocked, tone: 'blocking' }
        : { text: '', tone: null };
    },
  });
})();
