'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const { JSDOM } = require('jsdom');

function harness(persistedOrg) {
  let factory;
  const bars = [];
  const subscriptions = new Map();
  const dom = new JSDOM('<div id="topbar"></div>', {url:'https://example.test',runScripts:'outside-only'});
  dom.window.eval(fs.readFileSync(path.join(__dirname,'../../../static/js/org-picker.js'),'utf8'));
  dom.window.eval(fs.readFileSync(path.join(__dirname,'../../../static/js/asset-presence.js'),'utf8'));
  if (persistedOrg) dom.window.localStorage.setItem('autonomy.plugin.presentations.organization',persistedOrg);
  const sandbox = {
    console, setTimeout, clearTimeout,
    document: dom.window.document,
    OrgPicker: dom.window.OrgPicker,
    AssetPresence: dom.window.AssetPresence,
    localStorage: dom.window.localStorage,
    location: { pathname: '/presentations' },
    addEventListener() {}, removeEventListener() {},
    Alpine: { data(name, fn) { factory = fn; }, store() { return { live: { isLive: true, label: 'Editor' } }; } },
    registerHandler(topic, fn) { subscriptions.set(topic, fn); },
    unregisterHandler(topic) { subscriptions.delete(topic); },
    Autonomy: { setTopbar(bar) { bars.push(bar.html); dom.window.document.querySelector('#topbar').innerHTML=bar.html; }, fetch: async () => ({ ok: true, json: async () => ({ decks: [], org: 'autonomy' }) }) },
  };
  sandbox.window = sandbox;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../page.js'), 'utf8'), sandbox);
  return { page: factory(), sandbox, bars, subscriptions };
}

test('presence updates on the library cannot install deck chrome', () => {
  const { page, bars, sandbox } = harness();
  page.decks = [{ design_id: 'a', name: '<Deck>', creator_session_id: 'live' }];
  page.updateTopbar();
  assert.match(bars.at(-1), /Slides/);
  assert.match(sandbox.document.body.innerHTML, /1 deck/);
  assert.match(sandbox.document.body.innerHTML, /Designing now/);
  assert.match(sandbox.document.body.innerHTML, /&lt;Deck&gt;/);
  assert.doesNotMatch(bars.at(-1), /Untitled deck|present-topbar-back|present-topbar-count|present-topbar-presence-host/);
});

test('late callbacks cannot replace another route toolbar or revive a destroyed page', () => {
  const { page, sandbox, bars } = harness();
  sandbox.location.pathname = '/sessions';
  page.updateTopbar();
  assert.equal(bars.length, 0);
  sandbox.location.pathname = '/presentations/a';
  page.updateTopbar();
  assert.equal(bars.length, 0);
  page.destroy();
  sandbox.location.pathname = '/presentations';
  page.updateTopbar();
  assert.equal(bars.length, 0);
});

test('newer organization response wins even when the prior request finishes last', async () => {
  const { page, sandbox } = harness();
  const pending = [];
  sandbox.Autonomy.fetch = (url, options) => new Promise(resolve => pending.push({ resolve, options }));
  page.org = 'autonomy';
  const first = page.loadLibrary();
  page.org = 'other';
  const second = page.loadLibrary();
  assert.equal(pending[1].options.headers['X-Graph-Org'], 'other');
  pending[1].resolve({ ok: true, json: async () => ({ org: 'other', decks: [{ design_id: 'new' }] }) });
  await second;
  pending[0].resolve({ ok: true, json: async () => ({ org: 'autonomy', decks: [{ design_id: 'old' }] }) });
  await first;
  assert.equal(page.org, 'other');
  assert.equal(page.decks[0].design_id, 'new');
});

test('revision subscriptions survive refresh without repeated replay and are cleaned up', async () => {
  const { page, sandbox, subscriptions } = harness();
  let registrations = 0;
  sandbox.registerHandler = (topic, fn) => { registrations++; subscriptions.set(topic, fn); };
  page.decks = [{ design_id: 'a' }];
  page._subscribeLibrary();
  page._subscribeLibrary();
  assert.equal(registrations, 1);
  page.destroy();
  assert.equal(subscriptions.size, 0);
});

test('failed library load is an error, not an empty successful library', async () => {
  const { page, sandbox, bars } = harness();
  sandbox.Autonomy.fetch = async () => ({ ok: false });
  await page.loadLibrary();
  assert.match(page.error, /Could not load decks/);
  assert.match(sandbox.document.body.innerHTML, /Unavailable/);
});

test('library data refresh retains the org host rather than replacing the toolbar', () => {
  const {page, sandbox, bars}=harness();
  page.updateTopbar();
  const host=sandbox.document.querySelector('.present-library-org-host');
  page.loading=true; page.updateTopbar(); page.loading=false;
  page.decks=[{design_id:'new',name:'New'}]; page.updateTopbar();
  assert.equal(sandbox.document.querySelector('.present-library-org-host'),host);
  assert.equal(bars.length,1);
  assert.match(sandbox.document.body.textContent,/1 deck/);
});

test('library org picker keeps resolved icons and delegates changes once', async () => {
  const {page,sandbox}=harness();
  sandbox.Autonomy.fetch=async()=>({ok:true,json:async()=>({orgs:[{org:{slug:'alpha'},identity_resolved:{name:'Alpha',favicon:'/alpha.png'}},{org:{slug:'beta'},identity_resolved:{name:'Beta'}}]})});
  page.org='alpha'; await page.loadOrganizations();
  const trigger=sandbox.document.querySelector('[data-testid=present-org]');
  assert.equal(trigger.querySelector('img').getAttribute('src'),'/alpha.png');
  let loads=0;page.loadLibrary=()=>{loads++;};
  const menu=sandbox.document.getElementById(trigger.getAttribute('aria-controls'));
  menu.querySelector('[data-slug=beta]').click();
  assert.equal(page.org,'beta');assert.equal(loads,1);
  assert.equal(sandbox.localStorage.getItem('autonomy.plugin.presentations.organization'),'beta');
  page.updateTopbar();menu.querySelector('[data-slug=beta]').click();assert.equal(loads,1);
  page.destroy();assert.equal(menu.isConnected,false);
});

test('library restores the selected organization when the plugin remounts', () => {
  const first=harness();
  first.page.org='autonomy';first.page.organizations=[{slug:'autonomy',name:'Autonomy'},{slug:'beta',name:'Beta'}];first.page.updateTopbar();
  const trigger=first.sandbox.document.querySelector('[data-testid=present-org]');
  first.sandbox.document.getElementById(trigger.getAttribute('aria-controls')).querySelector('[data-slug=beta]').click();
  const second=harness(first.sandbox.localStorage.getItem('autonomy.plugin.presentations.organization'));
  assert.equal(second.page.org,'beta');
  first.page.destroy();second.page.destroy();
});

test('Slides preserves human participant kinds for the shared presence renderer', () => {
  const {page}=harness();
  page.deck={creator_session_id:'auto-editor'};
  page.participants=[{participant_id:'member-key',participant_label:'Operator',participant_kind:'operator'}];
  const rows=page._presenceOptions().sessions;
  assert.equal(rows.find(row=>row.id==='member-key').participant_kind,'operator');
  assert.equal(rows.find(row=>row.id==='auto-editor').id,'auto-editor');
});

// Build the real iframe document the viewer doc.write()s, run its runtime in
// jsdom with just enough layout stubbed (each slide 800px tall) for go() and
// activeIndex() to agree, and record what the runtime posts to the parent.
function slideRuntime(deckHtml) {
  const { sandbox } = harness();
  const html = sandbox.PresentationsTest.iframeDocument({ variants: [{ id: 'main', html: deckHtml }] }, 0);
  const posted = [];
  const dom = new JSDOM(html, {
    url: 'https://example.test/present/d1/1',
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    beforeParse(window) {
      const SLIDE = 800;
      const idx = (el) => { const v = el.getAttribute && el.getAttribute('data-present-index'); return v === null || v === undefined ? NaN : Number(v); };
      window.postMessage = (msg) => { posted.push(msg); };
      window.Element.prototype.scrollTo = function (opts) { this.scrollTop = Number(opts && opts.top) || 0; };
      Object.defineProperty(window.HTMLElement.prototype, 'offsetTop', {
        get() { const i = idx(this); return Number.isFinite(i) ? i * SLIDE : 0; },
      });
      Object.defineProperty(window.HTMLElement.prototype, 'offsetHeight', { get() { return SLIDE; } });
      Object.defineProperty(window.HTMLElement.prototype, 'clientHeight', { get() { return SLIDE; } });
      window.Element.prototype.getBoundingClientRect = function () {
        const i = idx(this);
        const root = this.ownerDocument.getElementById('present-scroll-root');
        const top = Number.isFinite(i) ? i * SLIDE - (root ? root.scrollTop : 0) : 0;
        return { top, bottom: top + SLIDE, left: 0, right: 0, width: 0, height: SLIDE };
      };
    },
  });
  return { dom, posted };
}

function click(el) {
  const ev = new el.ownerDocument.defaultView.MouseEvent('click', { bubbles: true, cancelable: true });
  return el.dispatchEvent(ev); // false when the runtime called preventDefault()
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
// Messages are minted in the jsdom realm; copy them so deepEqual compares values, not prototypes.
const last = (posted) => ({ ...posted.at(-1) });

test('an in-deck anchor to another slide routes through go() so the parent learns the new index', async () => {
  const { dom, posted } = slideRuntime(
    '<section id="a"><a id="jump" href="#b">next</a></section>' +
    '<section id="b"><p id="deep">two</p><a id="back" href="#a">back</a></section>' +
    '<section id="c"><a id="inner" href="#deep">into the middle of slide two</a></section>');
  const doc = dom.window.document;
  await wait(150);
  assert.deepEqual(last(posted), { type: 'present:active', index: 0, count: 3 });

  assert.equal(click(doc.getElementById('jump')), false, 'fragment navigation is intercepted');
  await wait(150);
  assert.equal(doc.getElementById('present-scroll-root').scrollTop, 800);
  assert.deepEqual(last(posted), { type: 'present:active', index: 1, count: 3 });
  assert.equal(dom.window.location.hash, '', 'the iframe location is not moved by the anchor');
  assert.ok(doc.getElementById('b').classList.contains('present-runtime-active'));

  // A target nested inside a slide resolves to that slide.
  assert.equal(click(doc.getElementById('inner')), false);
  await wait(150);
  assert.deepEqual(last(posted), { type: 'present:active', index: 1, count: 3 });

  assert.equal(click(doc.getElementById('back')), false);
  await wait(150);
  assert.equal(doc.getElementById('present-scroll-root').scrollTop, 0);
  assert.deepEqual(last(posted), { type: 'present:active', index: 0, count: 3 });
});

test('anchors that do not target a slide are left to the browser', async () => {
  const { dom, posted } = slideRuntime(
    '<section id="a"><a id="missing" href="#nowhere">gone</a><a id="ext" href="https://example.org/">out</a>' +
    '<a id="empty" href="#">top</a><a id="side" href="#aside">aside</a></section>' +
    '<div id="aside">not a slide</div><section id="b">two</section>');
  const doc = dom.window.document;
  await wait(150);
  const before = posted.length;
  for (const id of ['missing', 'empty', 'side']) {
    assert.equal(click(doc.getElementById(id)), true, id + ' is not intercepted');
  }
  const ext = doc.getElementById('ext');
  ext.addEventListener('click', (ev) => ev.preventDefault()); // keep jsdom from navigating
  assert.equal(click(ext), false);
  await wait(150);
  assert.equal(doc.getElementById('present-scroll-root').scrollTop, 0);
  assert.equal(posted.slice(before).filter((m) => m.type === 'present:active').length, 0);
});
