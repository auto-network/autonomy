'use strict';
// The shared presence + sharing control (static/js/asset-presence.js):
// one pill and dropdown for Design Studio, Slides, and Notes.

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const path = require('path');

const AssetPresence = require(path.resolve(__dirname, '../static/js/asset-presence.js'));

class FakeEl {
  constructor() { this.innerHTML = ''; this.classList = { add() {}, remove() {} }; this.listeners = {}; }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  removeEventListener() {}
  contains() { return true; }
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
  it('resolves sessions live-first and renders initials, rows, and the share button', async () => {
    const { win } = makeWindow({ sessions: { 'auto-live': { isLive: true, label: 'Live designer' } } });
    const el = new FakeEl();
    const ctl = AssetPresence.mount(el, { org: 'autonomy', targetType: 'design', targetUuid: 'd-1', sessions: SESSIONS, noun: 'design' });
    await new Promise((r) => setTimeout(r, 0));
    const resolved = ctl.sessions();
    assert.equal(resolved.map((s) => s.id).join(','), 'auto-live,auto-old');
    assert.equal(resolved[0].live, true);
    assert.equal(resolved[0].label, 'Live designer');
    assert.match(el.innerHTML, /data-testid="asset-presence-session"/);
    assert.match(el.innerHTML, /Share by link/);
    assert.match(el.innerHTML, /2 sessions, 1 live · not shared/);
    assert.doesNotMatch(el.innerHTML, /asset-presence-chat/); // no chat on this surface
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
    assert.match(el.innerHTML, /Shared by link/);
    assert.match(el.innerHTML, /expires in (1 day|2 days)/);
    assert.match(el.innerHTML, /asset-share-manage/);
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
    assert.match(el.innerHTML, /Awaiting approval/);
    assert.match(el.innerHTML, /asset-share-cancel/);
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
    assert.match(el.innerHTML, /Chat with a session…/);
    ctl.update({ chat: { open: true, connected: true, onToggle: (s) => toggled.push(s ? s.id : null) } });
    assert.match(el.innerHTML, /Close chat/);
    // simulate clicks through the delegated handler
    const clickAction = (action, sessionId) => ctl._onClick({
      target: { closest: (sel) => (sel === '[data-action]'
        ? { getAttribute: () => action, closest: (s2) => (s2 === '[data-session]' && sessionId ? { getAttribute: () => sessionId } : null) }
        : null) },
      preventDefault() {},
    });
    clickAction('toggle-chat');
    clickAction('chat-with', 'auto-live');
    assert.equal(toggled.join(','), ',auto-live');
    clickAction('open-session', 'auto-old');
    assert.equal(win.navigated, '/session/autonomy/auto-old');
    ctl.destroy();
  });

  it('formats push times and expiry text', () => {
    assert.equal(AssetPresence.expiryText({ expires_at: null }), 'no expiry');
    assert.equal(AssetPresence.expiryText({ expires_at: 1 }), 'expired');
    assert.match(AssetPresence.formatAgo(new Date(Date.now() - 5 * 60000).toISOString()), /5m ago/);
  });
});
