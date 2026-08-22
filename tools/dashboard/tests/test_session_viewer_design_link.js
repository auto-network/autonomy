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
              href: `/design/revision-${revision}`,
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
    assert.equal(h.viewer.sessionContributionActions[0].href, '/design/revision-1');

    h.setRevision(2);
    h.handlers['session-contributions']({session_id: 'auto-linked'});
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(h.viewer.sessionContributionActions[0].href, '/design/revision-2');

    h.viewer.openSessionContribution(h.viewer.sessionContributionActions[0]);
    assert.equal(h.navigatedTo, '/design/revision-2');
  });
});
