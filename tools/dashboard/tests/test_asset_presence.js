'use strict';
// The shared presence + sharing control (static/js/asset-presence.js):
// one pill and dropdown for Design Studio, Slides, and Notes.

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const path = require('path');

const AssetPresence = require(path.resolve(__dirname, '../static/js/asset-presence.js'));

// Enough DOM for the control: it builds a shell once, then writes into the
// summary and menu. innerHTML is parsed only far enough to answer the
// queries the control makes of it.
class FakeNode {
  constructor(tag, cls) {
    this.tagName = String(tag || 'DIV').toUpperCase();
    this.innerHTML = '';
    this.open = false;
    this.attrs = {};
    this._cls = new Set(String(cls || '').split(' ').filter(Boolean));
    this.classList = {
      add: (c) => this._cls.add(c),
      remove: (c) => this._cls.delete(c),
      toggle: (c, on) => (on ? this._cls.add(c) : this._cls.delete(c)),
      contains: (c) => this._cls.has(c),
    };
    this.listeners = {};
  }
  get className() { return [...this._cls].join(' '); }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  removeEventListener(name, fn) { this.listeners[name] = (this.listeners[name] || []).filter((f) => f !== fn); }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k]; }
  focus() { this.focused = true; }
  contains() { return true; }
  fire(name, event) { (this.listeners[name] || []).forEach((fn) => fn(event || {})); }
}

class FakeEl extends FakeNode {
  constructor() {
    super('DIV');
    this.details = new FakeNode('DETAILS');
    this.summary = new FakeNode('SUMMARY');
    this.menu = new FakeNode('DIV', 'design-presence-menu');
  }
  querySelector(sel) {
    if (sel === 'details') return this.details;
    if (sel === 'summary') return this.summary;
    if (sel === '.design-presence-menu') return this.menu;
    return null;
  }
  // What the control actually rendered, wherever it put it.
  get html() { return String(this.summary.innerHTML) + String(this.menu.innerHTML); }
}

function makeWindow({ responses = {}, sessions = {} } = {}) {
  const requests = [];
  const win = {
    Alpine: { store: (name) => (name === 'sessions' ? sessions : {}) },
    fetch: async (url, init) => {
      requests.push({ url, init });
      const hit = Object.keys(responses).find((prefix) => String(url).startsWith(prefix));
      const body = hit ? responses[hit] : { shared: false, grants: [] };
      return { ok: body.ok !== false, status: body.status || 200, json: async () => body };
    },
    navigateTo(p) { win.navigated = p; },
    open(url) { win.opened = url; },
  };
  global.window = win;
  const docHandlers = {};
  global.document = {
    addEventListener(name, fn) { (docHandlers[name] ||= []).push(fn); },
    removeEventListener(name, fn) { docHandlers[name] = (docHandlers[name] || []).filter((f) => f !== fn); },
    fire(name, event) { (docHandlers[name] || []).forEach((fn) => fn(event)); },
  };
  Object.defineProperty(global, 'navigator', { value: {}, configurable: true, writable: true });
  global.setTimeout = setTimeout;
  global.clearTimeout = clearTimeout;
  return { win, requests };
}

const SESSIONS = [
  { id: 'auto-old', label: 'Old', last_push: '2026-09-01 10:00:00', count: 1 },
  { id: 'auto-live', label: 'Live', last_push: '2026-09-07 01:00:00', count: 3 },
];

describe('AssetPresence', () => {
  it('activity mode shares chrome, counts sessions once and separates artifact and session links', async () => {
    const {requests}=makeWindow();const el=new FakeEl();
    const entry={session_id:'auto-edit',session_label:'Review layout',org:'example',artifact_id:'deck1',artifact_title:'Title bar <review>',artifact_href:'/presentations/deck1',artifact_kind:'Slides'};
    const ctl=AssetPresence.mount(el,{mode:'activity',entries:[entry,entry,{...entry,artifact_id:'deck2',artifact_href:'/presentations/deck2'}]});
    await new Promise(r=>setTimeout(r,0));
    assert.match(el.html,/Designing now/);assert.match(el.html,/Slides/);assert.match(el.html,/Session/);
    assert.match(el.html,/href="\/presentations\/deck1"/);assert.match(el.html,/href="\/session\/example\/auto-edit"/);
    assert.equal((el.html.match(/data-testid="activity-artifact"/g)||[]).length,2);
    assert.match(el.summary.getAttribute('title'),/1 live session/);
    assert.doesNotMatch(el.html,/asset-share|Share by link/);assert.equal(requests.length,0);
    assert.match(el.html,/Title bar &lt;review&gt;/);ctl.destroy();
  });
  it('read-only mission presence honors explicit human liveness and offers no sharing',async()=>{
    const {requests}=makeWindow({sessions:{agent:{isLive:true}}});const el=new FakeEl();
    const ctl=AssetPresence.mount(el,{sharing:false,targetUuid:'mission',people:true,noun:'mission',sessions:[{id:'person',label:'Operator',participant_kind:'operator',live:true},{id:'agent',participant_kind:'agent',live:false}]});
    await new Promise(r=>setTimeout(r,0));
    assert.equal(ctl.sessions().find(s=>s.id==='person').live,true);
    assert.equal(ctl.sessions().find(s=>s.id==='agent').live,false);
    assert.match(el.html,/People on this mission/);assert.doesNotMatch(el.html,/Sharing|Share by link/);
    assert.equal(requests.length,0);ctl.destroy();
  });
  it('renders people as text while retaining actual session links', () => {
    makeWindow();
    const el = new FakeEl();
    const ctl = AssetPresence.mount(el, {sessions: [
      {id: 'person-key', label: 'Operator', participant_kind: 'operator'},
      {id: 'auto-editor', label: 'Editor', participant_kind: 'agent'},
    ]});
    assert.match(el.html, /<div class="design-presence-row"[^>]*data-session="person-key">/);
    assert.doesNotMatch(el.html, /href="[^"]*person-key/);
    assert.match(el.html, /href="\/session\/autonomy\/auto-editor"/);
    ctl.destroy();
  });
  it('resolves sessions live-first and renders initials, rows, and the share button', async () => {
    const { win } = makeWindow({ sessions: { 'auto-live': { isLive: true, label: 'Live designer' } } });
    const el = new FakeEl();
    const ctl = AssetPresence.mount(el, { org: 'autonomy', targetType: 'design', targetUuid: 'd-1', sessions: SESSIONS, noun: 'design' });
    await new Promise((r) => setTimeout(r, 0));
    const resolved = ctl.sessions();
    assert.equal(resolved.map((s) => s.id).join(','), 'auto-live,auto-old');
    assert.equal(resolved[0].live, true);
    assert.equal(resolved[0].label, 'Live designer');
    assert.match(el.html, /data-testid="asset-presence-session"/);
    assert.match(el.html, /Share by link/);
    assert.match(el.summary.getAttribute('title'), /2 sessions, 1 live · not shared/);
    assert.doesNotMatch(el.html, /asset-presence-chat/); // no chat on this surface
    ctl.destroy();
    void win;
  });

  it('reads share state from the share-state route and shows the grant actions', async () => {
    const grant = { token: 'tok', url: 'https://relay/l/tok', expires_at: Math.floor(Date.now() / 1000) + 2 * 86400 };
    const { win, requests } = makeWindow({ responses: { '/api/share-state/present/deck-1': { shared: true, grants: [grant] } } });
    const el = new FakeEl();
    const ctl = AssetPresence.mount(el, { org: 'autonomy', targetType: 'present', targetUuid: 'deck-1', extraIds: ['rev-9'], sessions: [] });
    await new Promise((r) => setTimeout(r, 0));
    assert.ok(requests.some((r) => r.url === '/api/share-state/present/deck-1?org=autonomy&ids=rev-9'));
    assert.equal(ctl.share.shared, true);
    assert.match(el.html, /Shared by link/);
    assert.match(el.html, /expires in (1 day|2 days)/);
    assert.match(el.html, /asset-share-manage/);
    ctl.openLink();
    assert.equal(win.opened, grant.url);
    let opened = null;
    win.AutonomyOrgSettings = { open(slug, opts) { opened = { slug, opts }; } };
    ctl.manage();
    assert.equal(JSON.stringify(opened), JSON.stringify({ slug: 'autonomy', opts: { screen: 'published-links', focus: 'tok' } }));
    ctl.destroy();
  });

  it('requests a link_publish approval, waits, and resets on cancel or decline', async () => {
    const { win, requests } = makeWindow({ responses: { '/api/approvals': { id: 'central-1' } } });
    let overlay = '';
    win.openApprovalOverlay = (id) => { overlay = id; };
    const el = new FakeEl();
    const ctl = AssetPresence.mount(el, { org: 'autonomy', targetType: 'note', targetUuid: 'note-1', sessions: [] });
    await ctl.requestShare();
    const post = requests.find((r) => r.url === '/api/approvals');
    const body = JSON.parse(post.init.body);
    assert.equal(body.kind, 'link_publish');
    assert.equal(JSON.stringify(body.request), JSON.stringify({ org: 'autonomy', target_type: 'note', target_uuid: 'note-1', meta: {} }));
    assert.equal(overlay, 'central-1');
    assert.equal(ctl.shareState, 'awaiting');
    assert.match(el.html, /Awaiting approval/);
    assert.match(el.html, /asset-share-cancel/);
    await ctl.requestShare();  // no double request while awaiting
    assert.equal(requests.filter((r) => r.url === '/api/approvals').length, 1);
    ctl.cancelShareWait();
    assert.equal(ctl.shareState, 'idle');

    await ctl.requestShare();
    win.fetch = async (url) => (String(url).startsWith('/api/approvals/')
      ? { ok: true, json: async () => ({ result: { approved: false } }) }
      : { ok: true, json: async () => ({ shared: false, grants: [] }) });
    assert.equal(await ctl.checkApproval(), 'declined');
    ctl.destroy();
  });

  it('offers the chat toggle only when the surface has chat, and routes it back', async () => {
    const { win } = makeWindow({ sessions: { 'auto-live': { isLive: true, label: 'Live' } } });
    let toggled = [];
    const el = new FakeEl();
    const ctl = AssetPresence.mount(el, {
      org: 'autonomy', targetType: 'design', targetUuid: 'd-1', sessions: SESSIONS,
      chat: { open: false, connected: false, onToggle: (s) => toggled.push(s ? s.id : null) },
    });
    await new Promise((r) => setTimeout(r, 0));
    assert.match(el.html, /Chat with a session…/);
    ctl.update({ chat: { open: true, connected: true, onToggle: (s) => toggled.push(s ? s.id : null) } });
    assert.match(el.html, /Close chat/);
    // simulate clicks through the delegated handler
    const clickAction = (action, sessionId) => ctl._onClick({
      target: { closest: (sel) => (sel === '[data-action]'
        ? { getAttribute: () => action, closest: (s2) => (s2 === '[data-session]' && sessionId ? { getAttribute: () => sessionId } : null) }
        : null) },
      preventDefault() {},
    });
    clickAction('toggle-chat');
    assert.equal(toggled.join(','), '');
    clickAction('open-session', 'auto-old');
    assert.equal(win.navigated, '/session/autonomy/auto-old');
    ctl.destroy();
  });

  it('renders into a stable shell: a refresh cannot retrigger itself', async () => {
    // The control used to replace its own <details> on every render, which
    // fired a fresh toggle, which refreshed, which rendered: an unbounded
    // loop that hammered the API and destroyed buttons mid-click.
    const { requests } = makeWindow({ responses: { '/api/share-state/design/d-1': { shared: false, grants: [] } } });
    const el = new FakeEl();
    const ctl = AssetPresence.mount(el, { org: 'autonomy', targetType: 'design', targetUuid: 'd-1', sessions: SESSIONS });
    await new Promise((r) => setTimeout(r, 0));
    const detailsEl = ctl.details;
    // Opening asks for share state once, and rendering the answer must not
    // replace the element that would fire another toggle.
    el.details.open = true;
    el.details.fire('toggle');
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    assert.equal(ctl.details, detailsEl, 'the <details> element is never replaced');
    const shareStateCalls = requests.filter((r) => String(r.url).startsWith('/api/share-state/'));
    assert.ok(shareStateCalls.length <= 3, `expected a handful of share-state reads, got ${shareStateCalls.length}`);
    ctl.destroy();
  });

  it('closes on an outside click and on Escape, and leaves inside clicks alone', async () => {
    makeWindow();
    const el = new FakeEl();
    const ctl = AssetPresence.mount(el, { org: 'autonomy', targetType: 'note', targetUuid: 'n-1', sessions: [] });
    await new Promise((r) => setTimeout(r, 0));

    ctl.open = true;
    el.contains = () => true;                       // the click landed inside
    global.document.fire('click', { target: {} });
    assert.equal(ctl.open, true, 'an inside click keeps the menu open');

    el.contains = () => false;                      // the click landed outside
    global.document.fire('click', { target: {} });
    assert.equal(ctl.open, false, 'an outside click dismisses the menu');

    ctl.open = true;
    global.document.fire('keydown', { key: 'a' });
    assert.equal(ctl.open, true);
    global.document.fire('keydown', { key: 'Escape' });
    assert.equal(ctl.open, false, 'Escape dismisses the menu');
    ctl.destroy();
  });

  it('offers exactly one chat action, never a per-session duplicate', async () => {
    makeWindow({ sessions: { 'auto-live': { isLive: true, label: 'Live' } } });
    const el = new FakeEl();
    const ctl = AssetPresence.mount(el, {
      org: 'autonomy', targetType: 'design', targetUuid: 'd-1', sessions: SESSIONS,
      chat: { open: false, connected: false, onToggle: () => {} },
    });
    await new Promise((r) => setTimeout(r, 0));
    assert.equal((el.html.match(/data-action="toggle-chat"/g) || []).length, 1);
    assert.equal(el.html.includes('data-action="chat-with"'), false);
    ctl.destroy();
  });

  it('formats push times and expiry text', () => {
    assert.equal(AssetPresence.expiryText({ expires_at: null }), 'no expiry');
    assert.equal(AssetPresence.expiryText({ expires_at: 1 }), 'expired');
    assert.match(AssetPresence.formatAgo(new Date(Date.now() - 5 * 60000).toISOString()), /5m ago/);
  });
});
