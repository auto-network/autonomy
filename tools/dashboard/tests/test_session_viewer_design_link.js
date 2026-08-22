const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const VIEWER_JS = path.resolve(
  __dirname,
  '../static/js/pages/session-viewer.js',
);

function makeViewer(designs) {
  let alpineInit;
  let factory;
  let fetchUrl = '';
  let navigatedTo = '';
  const handlers = {};

  const document = {
    addEventListener(name, callback) {
      if (name === 'alpine:init') alpineInit = callback;
    },
  };
  const windowObj = {
    SessionRenderer: {},
    Autonomy: {
      fetch: async (url) => {
        fetchUrl = url;
        return {
          ok: true,
          json: async () => ({ designs }),
        };
      },
    },
    registerHandler(topic, callback) { handlers[topic] = callback; },
    unregisterHandler(topic) { delete handlers[topic]; },
    location: { assign(pathname) { navigatedTo = pathname; } },
  };
  const Alpine = {
    data(name, callback) {
      if (name === 'sessionViewerPage') factory = callback;
    },
  };
  const sandbox = {
    window: windowObj,
    document,
    Alpine,
    URLSearchParams,
    encodeURIComponent,
    navigateTo(pathname) { navigatedTo = pathname; },
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(VIEWER_JS, 'utf8'), sandbox, {
    filename: 'session-viewer.js',
  });
  alpineInit();

  const viewer = factory({ mode: 'page' });
  viewer.sessionKey = 'auto-linked';
  return {
    viewer,
    handlers,
    get fetchUrl() { return fetchUrl; },
    get navigatedTo() { return navigatedTo; },
  };
}

describe('session viewer linked Design Studio navigation', () => {
  it('finds the exact creator session, follows live revisions, and opens the latest one', async () => {
    const h = makeViewer([
      {
        design_id: 'wrong-design',
        latest_revision_id: 'wrong-revision',
        title: 'Mentions auto-linked but belongs elsewhere',
        creator_session_id: 'auto-other',
      },
      {
        design_id: 'design-1',
        latest_revision_id: 'revision-1',
        title: 'Session header polish',
        creator_session_id: 'auto-linked',
      },
    ]);

    await h.viewer._syncLinkedDesign('auto-linked');

    assert.match(h.fetchUrl, /\/api\/design-studio\/designs\?/);
    assert.match(h.fetchUrl, /q=auto-linked/);
    assert.deepEqual(
      JSON.parse(JSON.stringify(h.viewer.linkedDesign)),
      {
        design_id: 'design-1',
        latest_revision_id: 'revision-1',
        title: 'Session header polish',
      },
    );

    h.handlers['session-design:auto-linked']({
      design_id: 'design-1',
      latest_revision_id: 'revision-2',
      title: 'Session header polish',
    });
    assert.equal(h.viewer.linkedDesign.latest_revision_id, 'revision-2');

    h.viewer.openLinkedDesign();
    assert.equal(h.navigatedTo, '/design/revision-2');
  });
});
