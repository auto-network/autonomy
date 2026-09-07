'use strict';
// The Design Studio library: the filter strip narrows the gallery client-side
// (form factor, live, dynamic) on top of the server-filtered catalog, blank
// tiles explain themselves from the renderer's status, and the render action
// posts to the headless renderer.

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const PAGE_JS = path.resolve(__dirname, '../plugins/design_studio/page.js');

function makeLibrary(options = {}) {
  const requests = [];
  const sessions = options.sessions || { 'auto-live': { isLive: true, label: 'Live designer' } };
  const responses = options.responses || {};
  const sandbox = {
    console,
    setTimeout,
    clearTimeout,
    URLSearchParams,
    encodeURIComponent,
    JSON,
    Date,
    Object,
    Array,
    String,
    Number,
    isNaN,
    document: { addEventListener() {}, getElementById() { return null; }, querySelector() { return null; } },
    Alpine: { data() {}, store(name) { return name === 'sessions' ? sessions : {}; } },
    sessionStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    location: { pathname: '/design', search: '' },
    navigateTo(pathname) { sandbox.navigatedTo = pathname; },
    registerHandler() {},
    unregisterHandler() {},
    fetch: async (url, init) => {
      requests.push({ url, init });
      const hit = Object.keys(responses).find((prefix) => url.startsWith(prefix));
      const body = hit ? responses[hit] : { designs: [], summary: {} };
      return { ok: body.ok !== false, status: body.status || 200, json: async () => body };
    },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(PAGE_JS, 'utf8'), sandbox, { filename: PAGE_JS });
  const page = vm.runInContext('designStudioPage()', sandbox);
  return { page, requests, sandbox };
}

const DESIGNS = [
  { design_id: 'd-both', latest_revision_id: 'r-both', title: 'Responsive', status: 'pending',
    form_factor: 'both', has_fixture: true, creator_session_id: 'auto-live', thumbnail_url: '/t/both' },
  { design_id: 'd-phone', latest_revision_id: 'r-phone', title: 'Phone', status: 'pending',
    form_factor: 'mobile', has_fixture: false, creator_session_id: 'auto-dead', thumbnail_url: '/t/phone' },
  { design_id: 'd-desk', latest_revision_id: 'r-desk', title: 'Desktop', status: 'pending',
    form_factor: 'desktop', has_fixture: false, creator_session_id: '', thumbnail_url: '' },
];

describe('Design Studio gallery strip', () => {
  it('narrows the catalog by form factor, live session, and dynamic fixture', () => {
    const { page } = makeLibrary();
    page.designs = DESIGNS.slice();
    assert.deepEqual(page.visibleDesigns.map((d) => d.design_id), ['d-both', 'd-phone', 'd-desk']);
    assert.equal(page.hasFilters, false);

    page.setFormFactor('mobile');
    assert.deepEqual(page.visibleDesigns.map((d) => d.design_id), ['d-phone']);
    assert.equal(page.hasFilters, true);

    page.setFormFactor('all');
    page.liveOnly = true;
    assert.deepEqual(page.visibleDesigns.map((d) => d.design_id), ['d-both']);

    page.liveOnly = false;
    page.dynamicOnly = true;
    assert.deepEqual(page.visibleDesigns.map((d) => d.design_id), ['d-both']);
  });

  it('archived is a status round-trip that matches dismissed and completed rows', async () => {
    const { page, requests } = makeLibrary();
    assert.equal(page.status, 'pending');
    page.toggleArchived();
    assert.equal(page.status, 'dismissed,completed');
    assert.equal(page._designMatchesCurrentFilter({ status: 'completed' }), true);
    assert.equal(page._designMatchesCurrentFilter({ status: 'dismissed' }), true);
    assert.equal(page._designMatchesCurrentFilter({ status: 'pending' }), false);
    await new Promise((resolve) => setTimeout(resolve, 0));
    const catalog = requests.find((r) => r.url.startsWith('/api/design-studio/designs?'));
    assert.ok(catalog, 'toggling archived reloads the catalog');
    assert.match(catalog.url, /status=dismissed%2Ccompleted/);
    page.toggleArchived();
    assert.equal(page.status, 'pending');
  });

  it('explains a blank tile from the renderer status', () => {
    const { page } = makeLibrary();
    const design = DESIGNS[2];
    page.renderStatus = {};
    assert.equal(page.blankNote(design), 'No preview yet');
    page.renderStatus = { available: true, running: true, pending: 3 };
    assert.equal(page.blankNote(design), 'Queued');
    assert.equal(page.renderChip, 'Rendering 3');
    page.renderStatus = { available: false };
    assert.equal(page.blankNote(design), 'No renderer');
    assert.equal(page.renderChip, 'Renderer unavailable');
    page.actionStates[page._designActionKey(design, 'render')] = 'working';
    assert.equal(page.blankNote(design), 'Rendering');
  });

  it('posts a render request for the latest revision and surfaces a refusal', async () => {
    const ok = makeLibrary({ responses: {
      '/api/design-studio/revisions/r-desk/render': { ok: true, queued: true, status: { available: true, running: true, pending: 1 } },
      '/api/design-studio/render/status': { available: true, running: true, pending: 0, rendered: 0 },
    } });
    await ok.page.renderDesignThumbnail(DESIGNS[2]);
    const post = ok.requests.find((r) => r.url === '/api/design-studio/revisions/r-desk/render');
    assert.equal(post.init.method, 'POST');
    assert.equal(ok.page.designActionState(DESIGNS[2], 'render'), 'is-done');
    assert.equal(ok.page.renderActionTitle(DESIGNS[2]), 'Render queued');
    ok.page.destroy();

    const refused = makeLibrary({ responses: {
      '/api/design-studio/revisions/r-desk/render': { ok: false, status: 503, error: 'thumbnail renderer unavailable: agent-browser is not installed on this host', status: { available: false } },
    } });
    await refused.page.renderDesignThumbnail(DESIGNS[2]);
    assert.equal(refused.page.designActionState(DESIGNS[2], 'render'), 'is-error');
    assert.match(refused.page.actionError, /agent-browser/);
  });

  it('labels form factors with icons the strip and the tile badge share', () => {
    const { page } = makeLibrary();
    assert.equal(page.formFactorOptions.map((o) => o.value).join(','), 'all,both,desktop,mobile');
    assert.match(page.formFactorIcon('both'), /<svg[\s\S]*<svg/);
    assert.equal(page.formFactorIcon(''), '');
    assert.equal(page.formFactorTitle('mobile'), 'Phone mockup');
  });
});
