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

function makeLinkedDesignHarness(search = '?from_session=auto-linked') {
  const components = {};
  const bodyClasses = new Set();
  const storage = new Map();
  const sessions = {
    'auto-linked': {isLive: true, label: 'Linked session'},
    'auto-other': {isLive: true, label: 'Other session'},
  };
  let navigatedTo = '';
  let historyPath = '';
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
    async initDisplayCapture() {},
    navigateTo(pathname) { navigatedTo = pathname; },
    history: {pushState(_state, _title, pathname) { historyPath = pathname; }},
    localStorage: {
      getItem(key) { return storage.has(key) ? storage.get(key) : null; },
      setItem(key, value) { storage.set(key, value); },
      removeItem(key) { storage.delete(key); },
    },
    location: {pathname: '/design/revision-1', search},
    addEventListener() {},
    removeEventListener() {},
    fetch: async () => ({ok: true, json: async () => fullDesign}),
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(DESIGN_PAGE_JS, 'utf8'), sandbox, {filename: DESIGN_PAGE_JS});
  const page = components.designPage();
  page.$watch = () => {};
  page.$nextTick = (callback) => callback();
  return {
    page,
    bodyClasses,
    storage,
    get navigatedTo() { return navigatedTo; },
    get historyPath() { return historyPath; },
  };
}

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

  it('renders only the linked rail and iframe while focused', () => {
    const html = fs.readFileSync(DESIGN_PAGE_HTML, 'utf8');
    const css = fs.readFileSync(DESIGN_PAGE_CSS, 'utf8');
    assert.match(html, /data-testid="design-linked-toolbar"/);
    assert.match(html, /data-testid="design-linked-return"/);
    assert.match(html, /data-testid="design-linked-capture"/);
    assert.equal((html.match(/x-if="!linkedSessionMode"/g) || []).length, 2);
    assert.match(css, /body\.route-design-linked #sidebar/);
    assert.match(css, /body\.route-design-linked \.voice-capsule/);
    assert.match(css, /body\.route-design-linked \.design-surface/);
    assert.match(css, /flex: 1 1 0%/);
  });
});
