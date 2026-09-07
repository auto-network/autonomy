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
  var state = { slug: null, screenId: null, view: 'list', identity: null };

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
    state = { slug: null, screenId: null, view: 'list', identity: null };
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
    renderHeader();
    renderPane();
  }

  function renderHeader() {
    var title = host && host.querySelector('.orgset-title');
    if (!title) return;
    var screen = screens.filter(function (s) { return s.id === state.screenId; })[0];
    var identity = state.identity || {};
    var name = identity.name || state.slug || '';
    title.textContent = '';
    var badge = el('span', 'orgset-org-badge');
    if (identity.favicon) {
      var image = el('img');
      image.src = identity.favicon;
      image.alt = '';
      badge.appendChild(image);
    } else {
      badge.style.backgroundColor = identity.color || '#475569';
      badge.appendChild(el('span', '', identity.initial || name.charAt(0).toUpperCase()));
    }
    title.appendChild(badge);
    title.appendChild(el('span', '', name));
    if (screen && screen.windowTitle) {
      title.appendChild(el('span', 'orgset-title-separator', '–'));
      title.appendChild(el('span', 'orgset-window-name', screen.windowTitle));
    }
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
      .then(function () { return screen.render(state.slug, { focus: state.focus }); })
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
        if (state.focus) {
          var target = node.querySelector && node.querySelector('[data-share="' + state.focus + '"]');
          state.focus = '';
          if (target) {
            target.classList.add('orgset-focus');
            if (target.scrollIntoView) target.scrollIntoView({ block: 'center' });
          }
        }
      })
      .catch(function (err) {
        if (state.slug !== forSlug || state.screenId !== forScreen) return;
        pane.textContent = '';
        pane.appendChild(el('div', 'orgset-error', String((err && err.message) || err)));
      });
  }

  // open(slug, {screen, focus}) — `screen` selects a registered screen id
  // (default: the first), `focus` is an opaque hint the screen may honour
  // once rendered (Published Links scrolls to `[data-share="<focus>"]`).
  function open(slug, opts) {
    if (!slug) return;
    opts = opts || {};
    close();
    state.slug = slug;
    state.focus = opts.focus || '';
    var wanted = screens.filter(function (s) { return s.id === opts.screen; })[0];
    state.screenId = wanted ? wanted.id : (screens.length ? screens[0].id : null);

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
    if (typeof root.fetch === 'function') {
      root.fetch('/api/orgs/' + encodeURIComponent(slug), {headers: {'Accept': 'application/json'}})
        .then(function (res) { return res.ok ? res.json() : null; })
        .then(function (body) {
          if (!host || state.slug !== slug || !body) return;
          state.identity = body.identity_resolved || null;
          renderHeader();
        })
        .catch(function () {});
    }
    renderHeader();
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

  // What a missing thing is CALLED, and what sort of thing it is. Both come
  // off the declaration; neither is composed here.
  //
  // Composing them here is not a style preference. Two earlier passes at this
  // screen wrote the subtitle at render time and produced a git credential
  // described as "An approved secret, sealed per workspace", and a missing
  // GH_TOKEN titled "Alpha" — the workspace's own name, because a field
  // existed and meant something else. Neither looks broken from outside. So
  // when the data says nothing, this renders nothing.
  var STATE = {
    missing_reference: 'Never provisioned here',
    missing_env: 'Not set',
    missing_path: 'Not on this machine',
    unpopulated_path: 'Present, but empty',
    invalid_mount: 'Mount declaration is unusable',
    missing_capability_install: 'No organization installation',
    missing_capability_contract_version: 'Contract version is unavailable',
    capability_contract_version_mismatch: 'Contract version does not match',
    capability_install_contract_version_mismatch: 'Install version does not match',
    missing_capability_implementation_version: 'Implementation version is unavailable',
    capability_implementation_contract_mismatch: 'Implementation contract does not match',
    unanswerable_here: 'Cannot be answered from here',
    unreadable: 'Could not be read',
    unreadable_reference: 'Exists, but not readable from here',
    unknown_target: 'No schema for this',
  };

  function thingTitle(t) {
    // `name` is the authored short label. `description` is a sentence and is
    // only a fallback — its lead clause, since some run past 90 characters.
    if (t.name) return t.name;
    if (t.description) return String(t.description).split('—')[0].trim();
    return t.subject || t.at || '';
  }

  var thingSeq = 0;

  function thingNode(t) {
    var card = el('section', 'orgset-thing');
    card.setAttribute('data-testid', 'orgset-thing-' + (t.kind || '') + '-' + (t.subject || ''));
    var blocking = t.severity !== 'advisory';
    var id = 'orgset-thing-detail-' + (++thingSeq);

    var head = el('button', 'orgset-thing-head');
    head.type = 'button';
    head.setAttribute('aria-expanded', 'false');
    head.setAttribute('aria-controls', id);

    var main = el('span', 'orgset-thing-main');
    main.appendChild(el('span', 'orgset-thing-title', thingTitle(t)));
    // The schema's description of the declaring field: what this KIND of
    // thing is. Absent for some kinds, and absent is rendered as absent.
    if (t.field_description) {
      main.appendChild(el('span', 'orgset-thing-cat', t.field_description));
    }
    var state = el('span', 'orgset-thing-state');
    state.appendChild(el('i', 'orgset-dot ' + (blocking ? 'orgset-dot-blocking' : 'orgset-dot-advisory')));
    state.appendChild(el('span', '', (STATE[t.kind] || t.kind || '') + ' · '
      + (blocking ? 'Blocks launch' : 'Still runs without it')));
    main.appendChild(state);
    head.appendChild(main);

    var right = el('span', 'orgset-thing-right');
    var needed = (t.needed_by || []).length;
    var count = el('span', 'orgset-thing-count', String(needed));
    count.setAttribute('title', needed === 1 ? '1 workspace needs this'
                                             : needed + ' workspaces need this');
    right.appendChild(count);
    right.appendChild(el('span', 'orgset-thing-chev', '⌄'));
    head.appendChild(right);
    card.appendChild(head);

    // Everything explanatory lives here and only here. It is the whole
    // reason the collapsed row is readable: the same 14-word sentence about
    // launchers forwarding what is set was previously printed on every row.
    var detail = el('dl', 'orgset-thing-detail');
    detail.id = id;
    detail.hidden = true;
    function pair(term, value) {
      if (!value) return;
      detail.appendChild(el('dt', '', term));
      detail.appendChild(el('dd', '', String(value)));
    }
    // First, because it is the only part that says what to DO about it.
    pair('How to get it', t.help);
    pair('Description', t.description);
    pair(t.kind === 'missing_reference' ? 'Key' : 'Expected at', t.subject);
    pair('Needed by', (t.needed_by || []).join(', '));
    pair('Declared by', t.field ? t.field + ' on ' + t.set_id : t.set_id);
    // The frame the answer came from. This is the field that admits a host
    // variable was looked for in the checking process's own environment.
    pair('Checked in', t.looked_in);
    card.appendChild(detail);

    head.addEventListener('click', function () {
      var open = head.getAttribute('aria-expanded') === 'true';
      head.setAttribute('aria-expanded', open ? 'false' : 'true');
      detail.hidden = open;
    });
    return card;
  }

  function healthNode(report) {
    var wrap = el('div', 'orgset-workspaces');
    var things = report.things || [];
    var spaces = report.workspaces || [];

    if (!spaces.length) {
      wrap.appendChild(el('div', 'orgset-empty',
        'This organization declares no workspaces.'));
      return wrap;
    }

    // One heading over the things, not one card per workspace repeating them.
    // The old shape drew a fact once per workspace that wanted it — on real
    // data, 23 rows for 6 facts — which hides the only thing worth knowing:
    // that fixing one clears several workspaces at once.
    if (things.length) {
      wrap.appendChild(el('h3', 'orgset-things-head',
        things.length === 1 ? '1 thing to install on this machine'
                            : things.length + ' things to install on this machine'));
      wrap.appendChild(el('p', 'orgset-frame',
        'Checked on ' + (report.asked_in || 'this process')
        + '. Fixing one clears it everywhere it is needed.'));
      things.forEach(function (t) { wrap.appendChild(thingNode(t)); });
    } else {
      wrap.appendChild(el('h3', 'orgset-things-head', 'Nothing to install'));
      wrap.appendChild(el('p', 'orgset-frame',
        'Checked on ' + (report.asked_in || 'this process')
        + '. Everything these workspaces declare is present here.'));
    }

    var ready = spaces.filter(function (w) { return w.ready; }).length;
    wrap.appendChild(el('h3', 'orgset-ws-head',
      'Workspaces — ' + ready + ' of ' + spaces.length + ' ready'));
    var chips = el('div', 'orgset-ws-chips');
    spaces.forEach(function (ws) {
      var chip = el('span', 'orgset-ws-chip' + (ws.ready ? ' orgset-ws-chip-ready' : ''));
      chip.setAttribute('data-testid', 'orgset-workspace-' + ws.id);
      chip.appendChild(el('span', '', ws.name || ws.id));
      var unresolved = (ws.blocking || []).length + (ws.unanswerable || []).length;
      var tag = el('b', '', ws.ready ? '✓' : String(unresolved));
      tag.setAttribute('data-testid', 'orgset-workspace-state-' + ws.id);
      chip.appendChild(tag);
      chips.appendChild(chip);
    });
    wrap.appendChild(chips);
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
