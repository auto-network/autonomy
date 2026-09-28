// The software-update tile and the welcome preference (auto-n25fc; designs
// dc8b737b / c35fe726; operator decision D8). Drives the REAL modules through
// jsdom with a routed, logged fetch, so a dead handler or a stray GitHub
// fetch on panel open cannot pass.
const { describe, it, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const MODULE = '../static/js/identity-indicator.js';
const WELCOME_JS = path.join(__dirname, '../static/js/pages/welcome.js');
const tick = () => new Promise((r) => setTimeout(r, 0));
const settle = async () => { for (let i = 0; i < 6; i++) await tick(); };

function boot(updateStatus, extraRoutes) {
  const dom = new JSDOM(
    '<!DOCTYPE html><body><div id="identity-indicator"></div></body>',
    { url: 'http://localhost/' });
  const w = dom.window;
  const requests = [];
  const routes = Object.assign({
    'GET /api/identity/status': {
      personal_identity: { display_name: 'Alex' }, passkeys: [{ credential_id: 'a' }],
      onboarding_needed: false, signed_in: true, method: 'passkey',
      enforced: true, gate_disabled: false,
    },
    'GET /api/orgs': { orgs: [] },
    'GET /api/identity/unlock-state': {},
    'GET /api/software/update-status?fetch=0': updateStatus,
  }, extraRoutes || {});
  w.fetch = function (url, opts) {
    const method = (opts && opts.method) || 'GET';
    const key = method + ' ' + String(url);
    requests.push(key);
    let payload = routes[key];
    if (payload === undefined) payload = routes[method + ' ' + String(url).split('?')[0]];
    const ok = payload !== undefined;
    const body = typeof payload === 'function' ? payload() : payload;
    return Promise.resolve({ ok: ok, status: ok ? 200 : 404,
      json: function () { return Promise.resolve(body || {}); } });
  };
  global.window = w;
  global.document = w.document;
  global.fetch = w.fetch;
  delete require.cache[require.resolve(MODULE)];
  const ind = require(MODULE);
  return { dom, w, ind, requests };
}

async function openPanel(dom, ind) {
  ind.init();
  await settle();
  dom.window.document.querySelector('#identity-indicator button').click();
  await settle();
}

const tile = (dom) => dom.window.document.querySelector('[data-testid="identity-action-software"]');
const panel = (dom) => dom.window.document.querySelector('.identity-panel');

const BASE = { fetched: false, error: null, current: '70288a6e74', ahead: 0, mode: 'follower',
  checked_at: '2026-09-26T09:15:00Z', last_update: null, auto_install: false };

describe('software update tile', () => {
  afterEach(() => { delete global.window; delete global.document; delete global.fetch; });

  it('auto_check on and nothing to update: no tile, and opening never fetches GitHub', async () => {
    const { dom, ind, requests } = boot(Object.assign({}, BASE, { auto_check: true, behind: 0, can_update: false }));
    await openPanel(dom, ind);
    assert.equal(tile(dom), null);
    assert.ok(requests.includes('GET /api/software/update-status?fetch=0'));
    assert.ok(!requests.some((r) => r === 'GET /api/software/update-status'),
      'panel open must not issue the fetching update-status request');
  });

  it('auto_check on and behind: the update tile', async () => {
    const { dom, ind } = boot(Object.assign({}, BASE, { auto_check: true, behind: 3, can_update: true }));
    await openPanel(dom, ind);
    const t = tile(dom);
    assert.ok(t, 'the update tile renders');
    assert.match(t.textContent, /Software update available/);
    assert.match(t.textContent, /3 commits behind — click to update/);
  });

  it('auto_check off: Check for updates; a check spins, keeps the panel open, then rewrites the tile', async () => {
    let answer;
    const checked = new Promise((resolve) => { answer = resolve; });
    const found = Object.assign({}, BASE, { auto_check: false, fetched: true, behind: 2,
      can_update: true, checked_at: '2026-09-27T14:05:00Z' });
    const { dom, ind, requests } = boot(Object.assign({}, BASE, { auto_check: false, behind: 0, can_update: false }), {
      'GET /api/software/update-status': () => found,
    });
    // Hold the fetching request open so the in-flight state is observable.
    const realFetch = dom.window.fetch;
    dom.window.fetch = function (url, opts) {
      if (String(url) === '/api/software/update-status') {
        requests.push('GET /api/software/update-status');
        return checked.then(() => ({ ok: true, status: 200, json: () => Promise.resolve(found) }));
      }
      return realFetch(url, opts);
    };
    await openPanel(dom, ind);
    assert.match(tile(dom).textContent, /Check for updates/);
    assert.match(tile(dom).textContent, /Last checked/);

    tile(dom).click();
    await settle();
    assert.ok(panel(dom), 'the panel stays open while checking');
    assert.match(tile(dom).textContent, /Checking for updates…/);
    assert.ok(tile(dom).querySelector('.identity-update-spinner'), 'an inline spinner');
    assert.equal(tile(dom).disabled, true);

    answer();
    await settle();
    assert.ok(panel(dom), 'the panel is still open after the result');
    assert.match(tile(dom).textContent, /Software update available/);
    assert.match(tile(dom).textContent, /2 commits behind/);
  });

  it('auto_check off: a check that finds nothing says up to date', async () => {
    const current = Object.assign({}, BASE, { auto_check: false, fetched: true, behind: 0,
      can_update: false, checked_at: '2026-09-27T14:05:00Z' });
    const { dom, ind } = boot(Object.assign({}, BASE, { auto_check: false, behind: 0, can_update: false, checked_at: null }), {
      'GET /api/software/update-status': current,
    });
    await openPanel(dom, ind);
    assert.match(tile(dom).textContent, /Not checked yet/);
    tile(dom).click();
    await settle();
    assert.match(tile(dom).textContent, /Up to date as of/);
    assert.ok(tile(dom).querySelector('.identity-panel-action-detail--ok'));
  });

  it('auto_check off: a check that cannot reach GitHub offers a retry', async () => {
    const failed = Object.assign({}, BASE, { auto_check: false, fetched: false,
      error: 'fetch failed: Could not resolve host: github.com', behind: 0, can_update: false });
    const { dom, ind } = boot(Object.assign({}, BASE, { auto_check: false, behind: 0, can_update: false }), {
      'GET /api/software/update-status': failed,
    });
    await openPanel(dom, ind);
    tile(dom).click();
    await settle();
    assert.match(tile(dom).textContent, /Could not reach GitHub — click to try again/);
    assert.ok(tile(dom).querySelector('.identity-panel-action-detail--error'));
  });

  it('an automatic install is announced in the panel', async () => {
    const { dom, ind } = boot(Object.assign({}, BASE, { auto_check: true, auto_install: true, behind: 0,
      can_update: false, last_update: { automatic: true, at: '2026-09-27T14:02:05Z',
        from: '70288a6e74', to: 'a60361cd41', count: 3 } }));
    await openPanel(dom, ind);
    assert.equal(tile(dom), null);
    const notice = dom.window.document.querySelector('[data-testid="identity-software-auto-updated"]');
    assert.ok(notice);
    assert.match(notice.textContent, /Updated automatically to a60361c \(3 commits\)/);
  });

  it('clicking the update tile posts the update and reports the result', async () => {
    const { dom, ind, requests } = boot(Object.assign({}, BASE, { auto_check: true, behind: 3, can_update: true }), {
      'POST /api/software/update': { updated: true, from: '70288a6e74', to: 'a60361cd41', count: 3, deploy_changed: false },
    });
    await openPanel(dom, ind);
    tile(dom).click();
    await settle();
    assert.ok(requests.includes('POST /api/software/update'));
    const result = dom.window.document.querySelector('[data-testid="identity-software-result"]');
    assert.match(result.textContent, /Updated 3 commits \(70288a6→a60361c\)\. Reloading…/);
  });
});

describe('software update tile across openings', () => {
  afterEach(() => { delete global.window; delete global.document; delete global.fetch; });

  it('re-reads the cached status on every opening, so a changed preference shows', async () => {
    // S7 witness 2026-09-28: a tab open since an earlier check kept the old
    // status; after auto_check was switched off the reopened menu had no tile.
    let current = Object.assign({}, BASE, { auto_check: true, behind: 0, can_update: false });
    const { dom, ind, requests } = boot(null, {
      'GET /api/software/update-status?fetch=0': () => current,
    });
    await openPanel(dom, ind);
    assert.equal(tile(dom), null, 'auto_check on, nothing pending: no tile');

    const trigger = () => dom.window.document.querySelector('[data-testid="identity-trigger"]');
    trigger().click();                  // close
    await settle();
    current = Object.assign({}, current, { auto_check: false, fetched: true, checked_at: '2026-09-28T01:13:40Z' });
    trigger().click();                  // reopen
    await settle();
    assert.ok(tile(dom), 'the check tile appears for the switched-off preference');
    assert.match(tile(dom).textContent, /Check for updates/);
    assert.ok(!requests.some((r) => r === 'GET /api/software/update-status'),
      'reopening still never issues the fetching request');
  });
});

describe('welcome software-update preference', () => {
  // Objects made inside the jsdom realm have its prototypes; compare plain copies.
  const plain = (o) => JSON.parse(JSON.stringify(o));
  function welcome(routes) {
    const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/welcome', runScripts: 'outside-only' });
    const w = dom.window;
    const sent = [];
    w.fetch = function (url, opts) {
      const method = (opts && opts.method) || 'GET';
      sent.push({ method, url: String(url), body: opts && opts.body ? JSON.parse(opts.body) : null });
      const r = routes[method + ' ' + String(url)];
      const res = typeof r === 'function' ? r(opts) : r;
      return Promise.resolve(res || { ok: false, status: 404, json: () => Promise.resolve({}) });
    };
    w.eval(fs.readFileSync(WELCOME_JS, 'utf8'));
    return { app: w.welcomeApp(), sent };
  }
  const ok = (body) => ({ ok: true, status: 200, json: () => Promise.resolve(body) });

  it('defaults to check-and-notify on, install off, before and after loading', async () => {
    const { app } = welcome({ 'GET /api/software/preference': ok({ auto_check: true, auto_install: false, interval_minutes: 360 }) });
    assert.deepEqual(plain(app.updatePref), { auto_check: true, auto_install: false });
    await app.loadUpdatePref();
    assert.deepEqual(plain(app.updatePref), { auto_check: true, auto_install: false });
  });

  it('toggling a checkbox PUTs the preference', async () => {
    const { app, sent } = welcome({
      'PUT /api/software/preference': (opts) => ok(Object.assign({ auto_check: true, auto_install: false }, JSON.parse(opts.body))),
    });
    await app.saveUpdatePref({ auto_check: true, auto_install: true });
    const put = sent.find((s) => s.method === 'PUT');
    assert.deepEqual(plain(put.body), { auto_check: true, auto_install: true });
    assert.deepEqual(plain(app.updatePref), { auto_check: true, auto_install: true });
  });

  it('a failed save reverts and says so', async () => {
    const { app } = welcome({ 'PUT /api/software/preference': { ok: false, status: 503, json: () => Promise.resolve({}) } });
    await app.saveUpdatePref({ auto_check: false, auto_install: false });
    assert.deepEqual(plain(app.updatePref), { auto_check: true, auto_install: false });
    assert.match(app.updatePrefError, /HTTP 503/);
  });
});
