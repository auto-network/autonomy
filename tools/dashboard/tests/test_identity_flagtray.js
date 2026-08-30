// Render proof for the identity-panel status flag tray (bead auto-wugnk).
// Drives the REAL module through jsdom — not a copy of its logic — so a dead
// handler can't pass (the design note's own pitfall). Verifies: the tray
// renders six tiles, the unlock-state payload lights the right tiles amber,
// a tile click opens/closes its detail balloon, and a loaded org favicon
// drops its tile background (stands alone).
const { describe, it, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');

const MODULE = '../static/js/identity-indicator.js';
const tick = () => new Promise((r) => setTimeout(r, 0));

function boot(unlockState) {
  const dom = new JSDOM(
    '<!DOCTYPE html><body><div id="identity-indicator"></div></body>',
    { url: 'http://localhost/' });
  const w = dom.window;
  const routes = {
    '/api/identity/status': {
      personal_identity: { display_name: 'Alex' }, passkeys: [{ credential_id: 'a' }],
      onboarding_needed: false, signed_in: true, method: 'passkey',
      enforced: true, gate_disabled: false,
    },
    '/api/orgs': { orgs: [] },
    '/api/identity/unlock-state': unlockState || {},
  };
  w.fetch = function (url) {
    var key = String(url).split('?')[0];
    var payload = Object.prototype.hasOwnProperty.call(routes, key) ? routes[key] : {};
    var ok = Object.prototype.hasOwnProperty.call(routes, key);
    return Promise.resolve({ ok: ok, status: ok ? 200 : 404, json: function () { return Promise.resolve(payload); } });
  };
  global.window = w;
  global.document = w.document;
  global.fetch = w.fetch;   // module calls bare fetch() for orgs/unlock-state (== window.fetch in a browser)
  delete require.cache[require.resolve(MODULE)];
  const ind = require(MODULE);
  return { dom, w, ind };
}

async function openPanel(dom, ind) {
  ind.init();
  await tick();                 // refresh() status fetch -> trigger renders
  var trigger = dom.window.document.querySelector('#identity-indicator button');
  assert.ok(trigger, 'a trigger button should render for a signed-in identity');
  trigger.click();              // open the panel
  await tick();                 // orgs + unlock-state fetch -> panel re-renders
  await tick();
}

describe('identity flag tray', () => {
  afterEach(() => { delete global.window; delete global.document; delete global.fetch; });

  it('renders the seven-tile tray in the panel', async () => {
    const { dom, ind } = boot();
    await openPanel(dom, ind);
    const d = dom.window.document;
    const tray = d.querySelector('[data-testid="identity-flagtray"]');
    assert.ok(tray, 'the flag tray renders');
    assert.equal(tray.querySelectorAll('.identity-fl').length, 7);
    assert.ok(d.querySelector('[data-testid="identity-fl-sync"]'),
      'the seventh flag is Sync');
  });

  it('the sync flag lights and its balloon says what is broken + the count', async () => {
    const { dom, ind } = boot({ sync: { needs: true, value: 'Locked',
      detail: "Your other machines can't sync with this one. 764 requests have been refused since 8pm.",
      scopes: ['anchore', 'autonomy', 'dynbench'] } });
    await openPanel(dom, ind);
    const d = dom.window.document;
    assert.ok(d.querySelector('[data-testid="identity-fl-sync"]').classList.contains('needs'),
      'sync goes amber when unarmed/stale');
    d.querySelector('[data-testid="identity-fl-sync"]').click();
    await tick();
    const pop = d.querySelector('[data-testid="identity-flagpop"]');
    assert.match(pop.textContent, /can't sync/, 'says what stopped working');
    assert.match(pop.textContent, /764 requests/, 'the count makes it legible');
  });

  it('a lit flag shows the broken detail; a dim one shows the plain description', async () => {
    const { dom, ind } = boot({ tunnel: { needs: true,
      detail: "Your other devices can't reach this dashboard from outside." } });
    await openPanel(dom, ind);
    const d = dom.window.document;
    d.querySelector('[data-testid="identity-fl-tunnel"]').click();
    await tick();
    assert.match(d.querySelector('[data-testid="identity-flagpop"]').textContent,
      /can't reach this dashboard/, 'lit: the server broken detail');
    // a dim flag with no server detail falls back to its static description
    d.querySelector('[data-testid="identity-fl-cert"]').click();
    await tick();
    assert.match(d.querySelector('[data-testid="identity-flagpop"]').textContent,
      /certificate/i, 'dim: the plain what-it-is description');
  });

  it("renders a quiet note without lighting the tile", async () => {
    const { dom, ind } = boot({ certificates: { needs: false,
      note: "blindhash isn't set up to serve — that's expected, not a problem." } });
    await openPanel(dom, ind);
    const d = dom.window.document;
    assert.ok(!d.querySelector('[data-testid="identity-fl-cert"]').classList.contains('needs'),
      'a never-set-up scope does not light the flag');
    d.querySelector('[data-testid="identity-fl-cert"]').click();
    await tick();
    assert.match(d.querySelector('[data-testid="identity-flagpop"]').textContent,
      /blindhash isn't set up/, 'the quiet note shows in the balloon');
  });

  it('lights only the flags the unlock-state marks, dim otherwise', async () => {
    const { dom, ind } = boot({ certificates: { needs: true }, tunnel: { needs: false } });
    await openPanel(dom, ind);
    const d = dom.window.document;
    assert.ok(d.querySelector('[data-testid="identity-fl-cert"]').classList.contains('needs'),
      'certificate flag is amber when unlock-state says it needs attention');
    assert.ok(!d.querySelector('[data-testid="identity-fl-tunnel"]').classList.contains('needs'),
      'tunnel flag stays dim when it does not');
  });

  it('unknown (no unlock-state payload) renders every tile dim, never lit', async () => {
    const { dom, ind } = boot();   // routes return {} -> no flag needs
    await openPanel(dom, ind);
    const d = dom.window.document;
    const lit = d.querySelectorAll('.identity-fl.needs').length;
    assert.equal(lit, 0, 'nothing lights up when the state is unknown');
  });

  it('clicking a tile opens its detail balloon, clicking again closes it', async () => {
    const { dom, ind } = boot({ certificates: { needs: true } });
    await openPanel(dom, ind);
    const d = dom.window.document;
    d.querySelector('[data-testid="identity-fl-cert"]').click();
    await tick();
    const pop = d.querySelector('[data-testid="identity-flagpop"]');
    assert.ok(pop, 'a balloon appears');
    assert.match(pop.textContent, /Certificate/);
    assert.match(pop.textContent, /certificate/i);   // crisp title + body
    d.querySelector('[data-testid="identity-fl-cert"]').click();
    await tick();
    assert.equal(d.querySelector('[data-testid="identity-flagpop"]'), null, 'clicking again closes it');
  });

  it('shows the flag liveliness value in the balloon corner (green / amber)', async () => {
    const { dom, ind } = boot({ tunnel: { needs: false, value: 'Up' }, certificates: { needs: true, value: '6 days left' } });
    await openPanel(dom, ind);
    const d = dom.window.document;
    d.querySelector('[data-testid="identity-fl-tunnel"]').click();
    await tick();
    const up = d.querySelector('[data-testid="identity-flagpop-value"]');
    assert.equal(up.textContent, 'Up', 'a boolean flag reports "Up"');
    assert.ok(!up.classList.contains('needs'), 'green when healthy');
    d.querySelector('[data-testid="identity-fl-cert"]').click();
    await tick();
    const left = d.querySelector('[data-testid="identity-flagpop-value"]');
    assert.equal(left.textContent, '6 days left', 'a timed flag reports its remaining range');
    assert.ok(left.classList.contains('needs'), 'amber when running low');
  });

  it('shows no value when the state is unknown', async () => {
    const { dom, ind } = boot();   // no unlock-state payload
    await openPanel(dom, ind);
    const d = dom.window.document;
    d.querySelector('[data-testid="identity-fl-tunnel"]').click();
    await tick();
    assert.equal(d.querySelector('[data-testid="identity-flagpop-value"]'), null,
      'unknown liveliness renders no value, not a fake "Up"');
  });

  it('the restart button appears only when a flag is lit', async () => {
    const dim = boot();   // no lit flags
    await openPanel(dim.dom, dim.ind);
    assert.equal(dim.dom.window.document.querySelector('[data-testid="identity-flag-restart"]'), null,
      'no restart button when nothing is broken');

    const lit = boot({ sync: { needs: true } });
    await openPanel(lit.dom, lit.ind);
    assert.ok(lit.dom.window.document.querySelector('[data-testid="identity-flag-restart"]'),
      'the restart button appears when a flag is lit');
  });

  it('a flag balloon closes when you tap elsewhere in the panel', async () => {
    const { dom, ind } = boot({ sync: { needs: true } });
    await openPanel(dom, ind);
    const d = dom.window.document;
    d.querySelector('[data-testid="identity-fl-sync"]').click();
    await tick();
    assert.ok(d.querySelector('[data-testid="identity-flagpop"]'), 'balloon opens');
    // tap the panel header (not the tile, not the balloon)
    d.querySelector('[data-testid="identity-panel"]').click();
    await tick();
    assert.equal(d.querySelector('[data-testid="identity-flagpop"]'), null,
      'tapping anywhere else closes the balloon');
  });

  it('a loaded org favicon drops its tile background and hides the initial', async () => {
    const { dom, w, ind } = boot();
    // give /api/orgs a real org that declares a favicon
    const orig = w.fetch;
    w.fetch = global.fetch = function (url) {
      if (String(url).split('?')[0] === '/api/orgs') {
        return Promise.resolve({ ok: true, status: 200, json: function () {
          return Promise.resolve({ orgs: [{ org: { slug: 'acme' },
            identity: { payload: { name: 'Acme', byline: 'Widgets', color: '#e11d48', initial: 'A', favicon: '/x.png' } } }] });
        } });
      }
      return orig(url);
    };
    await openPanel(dom, ind);
    const d = dom.window.document;
    const mark = d.querySelector('.identity-org-mark');
    assert.ok(mark, 'the org tile renders');
    const img = mark.querySelector('img.identity-org-favicon');
    assert.ok(img, 'the favicon image is present');
    assert.ok(!mark.classList.contains('has-favicon'), 'before load: still a drawn tile');
    img.dispatchEvent(new dom.window.Event('load'));
    assert.ok(mark.classList.contains('has-favicon'), 'after load: tile goes transparent');
    assert.equal(mark.style.background, 'transparent');
    const initial = mark.querySelector('span');
    assert.equal(initial.style.display, 'none', 'the initial hides so the logo stands alone');
  });
});
