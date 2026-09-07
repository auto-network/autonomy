/**
 * Portable plugin contributions used by the session viewer and cards.
 *
 * Run: node --test tools/dashboard/tests/test_session_viewer_design_link.js
 */
const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SERVICE_JS = path.resolve(
  __dirname,
  '../static/js/lib/session-contributions.js',
);
const VIEWER_JS = path.resolve(
  __dirname,
  '../static/js/pages/session-viewer.js',
);
const DESIGN_PAGE_JS = path.resolve(
  __dirname,
  '../plugins/design_studio/page.js',
);
const DESIGN_PAGE_HTML = path.resolve(
  __dirname,
  '../plugins/design_studio/page.html',
);
const DESIGN_PAGE_CSS = path.resolve(
  __dirname,
  '../plugins/design_studio/page.css',
);
const TOOLBAR_STATE = require('../static/js/lib/toolbar-state.js');

function makeHarness() {
  const listeners = {};
  const handlers = {};
  const stores = {};
  const components = {};
  const requests = [];
  let navigatedTo = '';
  let revision = 1;
  const document = {
    addEventListener(name, callback) { (listeners[name] ||= []).push(callback); },
  };
  const Alpine = {
    data(name, factory) { components[name] = factory; },
    store(name, value) {
      if (arguments.length === 2) stores[name] = value;
      return stores[name];
    },
  };
  const windowObj = {
    Alpine,
    CSS: { supports() { return true; } },
    SessionRenderer: {},
    Autonomy: {
      fetch: async (url, options) => {
        requests.push({url, options});
        const ids = JSON.parse(options.body).session_ids;
        return {
          ok: true,
          json: async () => ({
            sessions: Object.fromEntries(ids.map((id) => [id, [{
              id: `design_studio:design:${revision}`,
              plugin_id: 'design_studio',
              session_id: id,
              kind: 'action',
              label: 'Design Studio',
              title: 'Open linked design',
              href: `/design/revision-${revision}?from_session=${encodeURIComponent(id)}`,
              icon_svg: '<svg></svg>',
              accent: '#818cf8',
            }]])),
          }),
        };
      },
    },
    registerHandler(topic, callback) { handlers[topic] = callback; },
    location: { assign(pathname) { navigatedTo = pathname; } },
    navigateTo(pathname) { navigatedTo = pathname; },
  };
  const sandbox = {
    window: windowObj,
    document,
    Alpine,
    CSS: windowObj.CSS,
    URLSearchParams,
    encodeURIComponent,
    console,
    setTimeout,
    fetch: windowObj.Autonomy.fetch,
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(SERVICE_JS, 'utf8'), sandbox, {filename: SERVICE_JS});
  vm.runInContext(fs.readFileSync(VIEWER_JS, 'utf8'), sandbox, {filename: VIEWER_JS});
  (listeners['alpine:init'] || []).forEach((callback) => callback());

  return {
    service: windowObj.Autonomy.sessionContributions,
    viewer: components.sessionViewerPage({mode: 'page'}),
    handlers,
    requests,
    setRevision(value) { revision = value; },
    get navigatedTo() { return navigatedTo; },
  };
}

function makeLinkedDesignHarness(search = '?from_session=auto-linked', viewport = {}) {
  const components = {};
  const bodyClasses = new Set();
  const storage = new Map();
  const rootStyles = new Map();
  const viewportListeners = {};
  const sessions = {
    'auto-linked': {isLive: true, label: 'Linked session'},
    'auto-other': {isLive: true, label: 'Other session'},
  };
  let navigatedTo = '';
  let historyPath = '';
  let captureInits = 0;
  let opened = '';
  const fetches = [];
  const series = viewport.series || {
    design_id: 'design-1',
    revisions: [
      {id: 'revision-1', creator_session_id: 'auto-other', creator_session_label: 'Other', created_at: '2026-09-01 10:00:00'},
      {id: 'revision-2', creator_session_id: 'auto-linked', creator_session_label: 'Linked', created_at: '2026-09-06 10:00:00'},
      {id: 'revision-3', creator_session_id: 'auto-linked', creator_session_label: 'Linked', created_at: '2026-09-07 01:00:00'},
    ],
    share: viewport.share || {shared: false, grants: []},
  };
  const visualViewport = {
    width: viewport.width || 390,
    height: viewport.height || 844,
    offsetLeft: viewport.offsetLeft || 0,
    offsetTop: viewport.offsetTop || 0,
    ...(viewport.pageLeft === undefined ? {} : {pageLeft: viewport.pageLeft}),
    ...(viewport.pageTop === undefined ? {} : {pageTop: viewport.pageTop}),
    addEventListener(name, callback) { (viewportListeners[name] ||= new Set()).add(callback); },
    removeEventListener(name, callback) { viewportListeners[name]?.delete(callback); },
  };
  const document = {
    body: {
      classList: {
        add(name) { bodyClasses.add(name); },
        remove(name) { bodyClasses.delete(name); },
        toggle(name, enabled) {
          if (enabled) bodyClasses.add(name);
          else bodyClasses.delete(name);
        },
      },
    },
    addEventListener() {},
    getElementById() { return null; },
    querySelector() { return null; },
  };
  const Alpine = {
    data(name, factory) { components[name] = factory; },
    store(name) { return name === 'sessions' ? sessions : {}; },
  };
  const fullDesign = {
    id: 'revision-2',
    design_id: 'design-1',
    org: 'autonomy',
    title: 'Linked design',
    revisions: ['revision-1', 'revision-2'],
    variants: [],
  };
  const sandbox = {
    window: null,
    document,
    Alpine,
    URLSearchParams,
    encodeURIComponent,
    console,
    setTimeout,
    clearTimeout,
    deriveToolbarState: TOOLBAR_STATE.deriveToolbarState,
    toolbarElements: TOOLBAR_STATE.toolbarElements,
    registerHandler() {},
    unregisterHandler() {},
    captureTabScreenshot() {},
    async manualCaptureScreenshot() {},
    async initDisplayCapture() { captureInits += 1; },
    navigateTo(pathname) { navigatedTo = pathname; },
    history: {pushState(_state, _title, pathname) { historyPath = pathname; }},
    localStorage: {
      getItem(key) { return storage.has(key) ? storage.get(key) : null; },
      setItem(key, value) { storage.set(key, value); },
      removeItem(key) { storage.delete(key); },
    },
    location: {pathname: '/design/revision-1', search},
    visualViewport,
    innerWidth: 390,
    innerHeight: 844,
    scrollX: viewport.scrollX || 0,
    scrollY: viewport.scrollY || 0,
    requestAnimationFrame(callback) { callback(); return 0; },
    cancelAnimationFrame() {},
    addEventListener() {},
    removeEventListener() {},
    fetch: async (url, init) => {
      fetches.push({url, init});
      if (String(url).startsWith('/api/design-studio/designs/')) {
        return {ok: true, json: async () => series};
      }
      if (String(url) === '/api/approvals') {
        return {ok: true, json: async () => ({id: 'central-approval-1'})};
      }
      return {ok: true, json: async () => fullDesign};
    },
    navigator: {},
    open(url) { opened = url; },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(DESIGN_PAGE_JS, 'utf8'), sandbox, {filename: DESIGN_PAGE_JS});
  const page = components.designPage();
  page.$watch = () => {};
  page.$nextTick = (callback) => callback();
  page.$root = {
    style: {setProperty(name, value) { rootStyles.set(name, value); }},
  };
  return {
    page,
    bodyClasses,
    storage,
    rootStyles,
    visualViewport,
    viewportListeners,
    get navigatedTo() { return navigatedTo; },
    get historyPath() { return historyPath; },
    get captureInits() { return captureInits; },
    get opened() { return opened; },
    fetches,
    sandbox,
  };
}

describe('design viewer presence and sharing', () => {
  it('returns to the gallery and opens the session picker from the library entry', () => {
    const h = makeLinkedDesignHarness('');
    h.page.init();
    h.page.returnToGallery();
    assert.equal(h.navigatedTo, '/design');
    let loaded = 0;
    h.page._loadChatSessions = () => { loaded += 1; };
    h.page.openSessionPicker();
    assert.equal(h.page.chatOpen, true);
    assert.equal(loaded, 1);
    h.page.destroy();
  });

  it('lists every session that pushed a revision, live first, newest first', async () => {
    const h = makeLinkedDesignHarness('');
    h.page.designId = 'design-1';
    h.page.design = {org: 'autonomy', title: 'Linked design'};
    await h.page._loadSeries();
    const sessions = h.page.presenceSessions;
    assert.equal(sessions.map((s) => s.id).join(','), 'auto-linked,auto-other');
    assert.equal(sessions[0].count, 2);
    assert.equal(sessions[0].last_push, '2026-09-07 01:00:00');
    assert.equal(sessions[0].live, true);
    assert.equal(sessions[0].label, 'Linked session');
    assert.equal(sessions[0].href, '/session/autonomy/auto-linked');
    assert.equal(sessions[1].live, true);   // the harness registry lists both as live
    assert.equal(sessions[1].label, 'Other session');
    assert.equal(h.page.presenceLive, true);
    assert.match(h.page.presenceSummaryTitle, /2 sessions, 2 live · not shared/);
    h.page.openPresenceSession(sessions[1]);
    assert.equal(h.navigatedTo, '/session/autonomy/auto-other');
    h.page.destroy();
  });

  it('toggles the chat panel and closes it from the toolbar', () => {
    const h = makeLinkedDesignHarness('');
    h.page.init();
    h.page._loadChatSessions = () => {};
    assert.equal(h.page.chatOpen, false);
    h.page.toggleChat();
    assert.equal(h.page.chatOpen, true);
    h.page.toggleChat();
    assert.equal(h.page.chatOpen, false);
    assert.match(h.page.formatPushedAt('2026-09-07 01:00:00'), /Sep 7/);
    h.page.destroy();
  });

  it('a cancelled or declined publish request returns the share button to idle', async () => {
    const h = makeLinkedDesignHarness('');
    h.page.designId = 'design-1';
    h.page.design = {org: 'autonomy', title: 'Linked design'};
    await h.page.shareDesign();
    assert.equal(h.page.shareState, 'awaiting');
    h.page.cancelShareWait();
    assert.equal(h.page.shareState, 'idle');

    await h.page.shareDesign();
    h.sandbox.fetch = async (url) => {
      if (String(url).startsWith('/api/approvals/')) return {ok: true, json: async () => ({result: {approved: false}})};
      return {ok: true, json: async () => ({})};
    };
    assert.equal(await h.page._checkShareApproval(), 'declined');
    h.page.destroy();
  });

  it('requests a link_publish approval for the design and waits for the grant', async () => {
    const h = makeLinkedDesignHarness('');
    h.page.designId = 'design-1';
    h.page.design = {org: 'autonomy', title: 'Linked design'};
    let overlay = '';
    h.sandbox.openApprovalOverlay = (id) => { overlay = id; };
    await h.page.shareDesign();
    const post = h.fetches.find((f) => f.url === '/api/approvals');
    const body = JSON.parse(post.init.body);
    assert.equal(body.kind, 'link_publish');
    assert.equal(JSON.stringify(body.request), JSON.stringify({org: 'autonomy', target_type: 'design', target_uuid: 'design-1', meta: {}}));
    assert.equal(overlay, 'central-approval-1');
    assert.equal(h.page.shareState, 'awaiting');
    assert.equal(await h.page.shareDesign(), undefined); // no double request while awaiting
    assert.equal(h.fetches.filter((f) => f.url === '/api/approvals').length, 1);
    h.page.destroy();
  });

  it('exposes the newest grant: expiry text, open in a new tab, manage in Published Links', async () => {
    const grant = {token: 'tok-1', url: 'https://relay.auto.network/l/tok-1', expires_at: Math.floor(Date.now() / 1000) + 3 * 86400};
    const h = makeLinkedDesignHarness('', {share: {shared: true, grants: [grant]}});
    h.page.designId = 'design-1';
    h.page.design = {org: 'autonomy', title: 'Linked design'};
    await h.page._loadSeries();
    assert.equal(h.page.share.shared, true);
    assert.equal(h.page.primaryGrant.token, 'tok-1');
    assert.match(h.page.shareExpiryText, /expires in (2|3) days/);
    assert.match(h.page.presenceSummaryTitle, /shared by link/);
    h.page.openShareLink();
    assert.equal(h.opened, grant.url);
    let openedSettings = null;
    h.sandbox.AutonomyOrgSettings = {open(slug, opts) { openedSettings = {slug, opts}; }};
    h.page.manageShare();
    assert.equal(JSON.stringify(openedSettings), JSON.stringify({slug: 'autonomy', opts: {screen: 'published-links', focus: 'tok-1'}}));
    h.page.destroy();
  });
});

describe('portable session contributions', () => {
  it('loads a linked Design Studio action, refreshes it live, and opens it', async () => {
    const h = makeHarness();
    h.viewer.sessionKey = 'auto-linked';

    await h.service.load(['auto-linked']);
    assert.equal(h.requests[0].url, '/api/session-contributions');
    assert.deepEqual(
      JSON.parse(h.requests[0].options.body),
      {session_ids: ['auto-linked']},
    );
    assert.equal(h.viewer.sessionContributionActions[0].href, '/design/revision-1?from_session=auto-linked');

    h.setRevision(2);
    h.handlers['session-contributions']({session_id: 'auto-linked'});
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(h.viewer.sessionContributionActions[0].href, '/design/revision-2?from_session=auto-linked');

    h.viewer.openSessionContribution(h.viewer.sessionContributionActions[0]);
    assert.equal(h.navigatedTo, '/design/revision-2?from_session=auto-linked');
  });
});

describe('linked Design Studio viewer mode', () => {
  it('enters a focused shell and returns to the originating session', () => {
    const h = makeLinkedDesignHarness();
    h.page.init();
    assert.equal(h.page.linkedSessionMode, true);
    assert.equal(h.page.linkedSessionLive, true);
    assert.equal(h.page.linkedSessionLabel, 'Linked session');
    assert.equal(h.bodyClasses.has('route-design-linked'), true);

    h.page.design = {org: 'autonomy'};
    h.page.returnToSession();
    assert.equal(h.navigatedTo, '/session/autonomy/auto-linked');

    h.page.destroy();
    assert.equal(h.bodyClasses.has('route-design-linked'), false);
  });

  it('never requests a screen-share stream when a chat session connects', () => {
    const h = makeLinkedDesignHarness('');
    h.page.init();
    h.page.designId = 'design-1';
    h.page.revisionId = 'revision-2';
    h.page.chatSessions = [{id: 'auto-other', label: 'Other session', project: 'default'}];
    h.page._connectSession('auto-other');
    assert.equal(h.page.chatConnected, true);
    assert.equal(h.storage.get('design-chat-design-1'), 'auto-other');
    assert.equal(h.captureInits, 0);
    h.page.destroy();
  });

  it('does not enter focused mode for ordinary library/direct navigation', () => {
    const h = makeLinkedDesignHarness('');
    h.page.init();
    assert.equal(h.page.linkedSessionMode, false);
    assert.equal(h.bodyClasses.has('route-design-linked'), false);
    h.page.destroy();
  });

  it('preserves linked context when a live revision replaces the iframe', async () => {
    const h = makeLinkedDesignHarness();
    h.page.linkedSessionId = 'auto-linked';
    h.page.designId = 'design-1';
    h.page.revisionId = 'revision-1';
    h.page._destroyed = false;
    h.page._loadGen = 0;

    await h.page._swapRevision('revision-2');
    assert.equal(h.historyPath, '/design/revision-2?from_session=auto-linked');
  });

  it('tracks an iOS visual viewport shift without leaving a gutter', () => {
    const h = makeLinkedDesignHarness('?from_session=auto-linked', {
      width: 390,
      height: 804,
      pageLeft: 0,
      pageTop: 40,
    });
    h.page.init();

    assert.equal(h.rootStyles.get('--design-viewport-top'), '40px');
    assert.equal(h.rootStyles.get('--design-viewport-height'), '804px');
    assert.equal(h.rootStyles.get('--design-viewport-width'), '390px');
    assert.equal(40 + Number.parseInt(h.rootStyles.get('--design-viewport-height'), 10), 844);

    h.visualViewport.pageTop = 0;
    h.visualViewport.height = 844;
    for (const callback of h.viewportListeners.resize) callback();
    assert.equal(h.rootStyles.get('--design-viewport-top'), '0px');
    assert.equal(h.rootStyles.get('--design-viewport-height'), '844px');

    h.page.destroy();
    assert.equal(h.viewportListeners.resize.size, 0);
    assert.equal(h.viewportListeners.scroll.size, 0);
  });

  it('renders only the linked rail and iframe while focused', () => {
    const html = fs.readFileSync(DESIGN_PAGE_HTML, 'utf8');
    const css = fs.readFileSync(DESIGN_PAGE_CSS, 'utf8');
    assert.match(html, /data-testid="design-toolbar"/);
    assert.match(html, /data-testid="design-presence"/);
    assert.match(html, /data-testid="design-linked-return"/);
    assert.match(html, /data-testid="design-linked-capture"/);
    // hamburger (phone), back-to-gallery control, and the chat overlay
    assert.equal((html.match(/x-if="!linkedSessionMode"/g) || []).length, 3);
    assert.match(html, /data-testid="design-gallery-return"/);
    assert.match(html, /data-testid="design-presence-pick-session"/);
    assert.match(css, /body\.route-design-linked #sidebar/);
    // 50a05f29 deliberately stopped hiding the voice capsule on linked pages.
    assert.doesNotMatch(css, /body\.route-design-linked \.voice-capsule/);
    assert.match(css, /body\.route-design-linked \.design-surface/);
    assert.match(css, /--design-viewport-top/);
    assert.match(css, /env\(safe-area-inset-top/);
    assert.match(css, /flex: 1 1 0%/);
  });
});
