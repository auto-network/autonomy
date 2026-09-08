'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const { JSDOM } = require('jsdom');

function harness() {
  let factory;
  const bars = [];
  const subscriptions = new Map();
  const dom = new JSDOM('<div id="topbar"></div>', {url:'https://example.test',runScripts:'outside-only'});
  dom.window.eval(fs.readFileSync(path.join(__dirname,'../../../static/js/org-picker.js'),'utf8'));
  const sandbox = {
    console, setTimeout, clearTimeout,
    document: dom.window.document,
    OrgPicker: dom.window.OrgPicker,
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
  page.updateTopbar();menu.querySelector('[data-slug=beta]').click();assert.equal(loads,1);
  page.destroy();assert.equal(menu.isConnected,false);
});

test('Slides preserves human participant kinds for the shared presence renderer', () => {
  const {page}=harness();
  page.deck={creator_session_id:'auto-editor'};
  page.participants=[{participant_id:'member-key',participant_label:'Operator',participant_kind:'operator'}];
  const rows=page._presenceOptions().sessions;
  assert.equal(rows.find(row=>row.id==='member-key').participant_kind,'operator');
  assert.equal(rows.find(row=>row.id==='auto-editor').id,'auto-editor');
});
