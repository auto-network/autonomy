/**
 * Sessions page: the machine chooser and remote Active cards (auto-mje3g,
 * design 64906530 revision f984e30b). Workspace first; a chooser only when
 * another machine can be launched on; remote rows become ordinary cards
 * whose machine is named and whose reachability is the session's own dot.
 */
const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const { SESSIONS_HTML, makeSessionsPage } = require('./sessions_page_harness');

const REFUSAL_JS = path.join(__dirname, '..', 'static', 'js', 'lib', 'refusal-text.js');
function loadRefusalText(win) {
  new Function('window', 'globalThis', fs.readFileSync(REFUSAL_JS, 'utf8'))(win, win);
  return win;
}

class CustomEvent {
  constructor(type, init) { this.type = type; this.detail = (init || {}).detail; }
}

const HOME = { machine_pub: 'a1', label: 'home', local: true, reachable: true, state: 'reachable',
  live_sessions: 3, ram_free_gb: 11.6, disk_free_gb: 184.4, load_1m: 3.2, cpus: 16 };
const SJC = { machine_pub: 'b1', label: 'sjc-2', local: false, reachable: true, state: 'reachable',
  live_sessions: 2, ram_free_gb: 47.3, disk_free_gb: 812, load_1m: 1.4, cpus: 32 };

describe('machine chooser', () => {
  it('launches here directly when no other machine can be launched on', () => {
    const p = makeSessionsPage({ CustomEvent });
    p.launchTargetsState = 'ready';
    p.launchTargets = [HOME];
    const closes = p.pickWorkspace({ id: 'autonomy-docs', name: 'Docs' });
    assert.equal(closes, true);
    assert.equal(p.launchPanel, 'workspaces');
  });

  it('opens the chooser after a workspace when another machine exists', () => {
    const p = makeSessionsPage({ CustomEvent });
    p.launchTargetsState = 'ready';
    p.launchTargets = [HOME, SJC];
    assert.equal(p.pickWorkspace({ id: 'autonomy-docs', name: 'Docs' }), false);
    assert.equal(p.launchPanel, 'machines');
    assert.equal(p.launchWorkspace.id, 'autonomy-docs');
  });

  it('refuses an unreachable machine and returns to the workspace list after a launch', () => {
    const p = makeSessionsPage({ CustomEvent });
    p.launchTargets = [HOME, Object.assign({}, SJC, { reachable: false, state: 'unreachable' })];
    p.pickWorkspace({ id: 'autonomy-docs', name: 'Docs' });
    assert.equal(p.launchOn(p.launchTargets[1]), false);
    assert.equal(p.launchPanel, 'machines');
    assert.equal(p.launchOn(p.launchTargets[0]), true);
    assert.equal(p.launchPanel, 'workspaces');
    assert.equal(p.launchWorkspace, null);
  });

  it('never launches on a machine that is reachable but not enabled', () => {
    const p = makeSessionsPage({ CustomEvent });
    const armed = Object.assign({}, SJC, { state: 'not_enabled', reason: 'session-control-not-granted', at: 'local' });
    p.launchTargets = [HOME, armed];
    p.pickWorkspace({ id: 'autonomy-docs', name: 'Docs' });
    assert.equal(p.launchOn(armed), false);
    assert.equal(p.launchOn(Object.assign({}, armed, { reachable: true })), false);
    assert.equal(p.launchPanel, 'machines');
  });

  it('states each machine\'s figures, and why one cannot be used', () => {
    const p = makeSessionsPage({ CustomEvent });
    loadRefusalText(p.__window);
    p.launchTargets = [HOME];
    assert.equal(p.launchTargetStats(SJC),
      '2 live · 47.3 GB RAM free · 812 GB disk free · load 0.04');
    // The refusal names the machine that decided it and keeps its code.
    assert.equal(
      p.launchTargetStats({ label: 'sjc', state: 'not_enabled', reason: 'session-control-not-granted', at: 'local' }),
      "Home's runtime delegation does not include session:control [session-control-not-granted]");
    assert.equal(
      p.launchTargetStats({ label: 'sjc', state: 'not_enabled', reason: 'peer-scope-missing', at: 'peer' }),
      "Home's runtime delegation does not include session:control (refused by Sjc) [peer-scope-missing]");
    assert.equal(
      p.launchTargetStats({ label: 'sjc', state: 'not_enabled', reason: 'session-control-not-negotiated', at: 'local' }),
      "Home's relay tunnel did not negotiate session control [session-control-not-negotiated]");
    assert.match(p.launchTargetStats({ reachable: false, state: 'unreachable', unreachable_since: 1800000000 }),
      /^unreachable since /);
  });
});

describe('refusal text', () => {
  const win = loadRefusalText({});
  const names = { here: 'home', there: 'sjc' };

  it('states handshake codes about the right machine, from either side', () => {
    // own-* is the checker's own material; the checker is whoever decided.
    assert.equal(win.describeRefusal({ reason: 'own-delegation-expired', at: 'local' }, names),
      "Home's runtime delegation has expired [own-delegation-expired]");
    assert.equal(win.describeRefusal({ reason: 'own-delegation-expired', at: 'peer' }, names),
      "Sjc's runtime delegation has expired (refused by Sjc) [own-delegation-expired]");
    assert.equal(win.describeRefusal({ reason: 'peer-scope-excess', at: 'local' }, names),
      "Sjc's runtime delegation carries a scope outside fleet:sync and session:control [peer-scope-excess]");
  });

  it('never invents a cause for a code it does not know', () => {
    assert.equal(win.describeRefusal({ reason: 'something-new' }, names),
      'Remote sessions refused [something-new]');
  });
});

describe('remote Active cards', () => {
  it('name the machine a remote session runs on, and say when it is unreachable', () => {
    const p = makeSessionsPage({ CustomEvent });
    assert.match(p.machineTitle({ machine: 'sjc-2', machine_reachable: false,
      machine_unreachable_since: 1800000000 }), /^sjc-2 unreachable since /);
    assert.equal(p.machineTitle({ machine: 'sjc-2', machine_reachable: true }), 'Runs on sjc-2');
    assert.equal(p.machineTitle({}), '');
  });
});

describe('remote card actions', () => {
  function sheet(card) {
    const calls = [];
    const p = makeSessionsPage({ CustomEvent, fetch: (url, opts) => { calls.push([url, opts && opts.method]); return Promise.resolve({ ok: true }); } });
    let shown = null;
    p.__window.actionSheet = { show(o) { shown = o; } };
    p.showSessionActions(card);
    return { labels: Array.from(shown.actions, (a) => a.label), shown, calls };
  }

  it('offer only Close for a live remote session, and Close stops it by its address', async () => {
    const card = { session_id: 'auto-9@sjc-2', is_live: true, machine: 'sjc-2', nag_enabled: false };
    const r = sheet(card);
    assert.deepEqual(r.labels, ['Close Session']);
    await r.shown.actions[0].handler();
    assert.deepEqual(r.calls, [['/api/terminal/auto-9%40sjc-2/kill', 'POST']]);
  });

  it('keep nag and restart for a local session', () => {
    const r = sheet({ session_id: 'auto-1', is_live: true, nag_enabled: false });
    assert.deepEqual(r.labels, ['Enable Nag (15m)', 'Restart Session', 'Close Session']);
  });
});

describe('templates', () => {
  it('the + menu has the chooser panel and the card reuses the session dot', () => {
    const html = fs.readFileSync(SESSIONS_HTML, 'utf8');
    assert.match(html, /data-testid="launch-machine-chooser"/);
    assert.match(html, /pickWorkspace\(p\)/);
    const card = fs.readFileSync(SESSIONS_HTML.replace('pages/sessions.html', 'partials/session-card.html'), 'utf8');
    assert.match(card, /isLive: !!s\.is_live && s\.machine_reachable !== false/);
    assert.doesNotMatch(card, /sc-machine-dot/);
    assert.doesNotMatch(card, /is-unreachable/);
    // Local-only card controls (worktree link, resume, retry) never render
    // for a session on another machine.
    assert.match(card, /x-show="!s\.machine && typeof hasWorkspaceChanges/);
    assert.equal((card.match(/x-if="!s\.machine && !s\.is_live && s\.resumable/g) || []).length, 2);
    assert.match(card, /x-if="!s\.machine && window\.Autonomy && window\.Autonomy\.lifecycle && window\.Autonomy\.lifecycle\.inlineAction/);
  });

  it('the viewer hides Escape, Ctrl-B and the terminal for a remote session', () => {
    const view = fs.readFileSync(SESSIONS_HTML.replace('sessions.html', 'session-view.html'), 'utf8');
    assert.equal((view.match(/x-if="!isRemote && _tmuxSession && isLive/g) || []).length, 3);
    const js = fs.readFileSync(SESSIONS_HTML.replace('templates/pages/sessions.html', 'static/js/pages/session-viewer.js'), 'utf8');
    assert.equal((js.match(/\|\| this\.isRemote\) return;/g) || []).length, 4);
  });
});

describe('machine list not ready', () => {
  it('opens the chooser in its loading state instead of launching here unasked', () => {
    const p = makeSessionsPage({ CustomEvent });
    const created = [];
    p.__window.addEventListener('create-terminal', (e) => created.push(e.detail));
    p.launchTargetsState = 'loading';
    assert.equal(p.pickWorkspace({ id: 'autonomy-docs', name: 'Docs' }), false);
    assert.equal(p.launchPanel, 'machines');
    assert.equal(created.length, 0);
  });

  it('opens the chooser with the failure, and launches here only when asked', () => {
    const p = makeSessionsPage({ CustomEvent });
    const created = [];
    p.__window.addEventListener('create-terminal', (e) => created.push(e.detail));
    p.launchTargetsState = 'failed';
    p.launchTargetsError = 'HTTP 502';
    assert.equal(p.pickWorkspace({ id: 'autonomy-docs', name: 'Docs' }), false);
    assert.equal(created.length, 0);
    assert.equal(p.launchHere(), true);
    assert.equal(created.length, 1);
    assert.equal(created[0].project, 'autonomy-docs');
  });

  it('launches here directly once the list is ready and no other machine exists', () => {
    const p = makeSessionsPage({ CustomEvent });
    p.launchTargetsState = 'ready';
    p.launchTargets = [HOME];
    assert.equal(p.pickWorkspace({ id: 'autonomy-docs', name: 'Docs' }), true);
  });
});

describe('a refused create is stated, not swallowed', () => {
  it('names the machine, the refusal in words, its code and the detail', () => {
    const p = makeSessionsPage({ CustomEvent });
    loadRefusalText(p.__window);
    p.launchTargets = [HOME];
    const text = p._createErrorText(
      { refusal: 'destination-slot-absent', at: 'local', error: 'the relay reports no serving slot' },
      { machineLabel: 'sjc' });
    assert.match(text, /^Could not launch on sjc: /);
    assert.match(text, /\[destination-slot-absent\]/);
    assert.match(text, /the relay reports no serving slot$/);
  });

  it('falls back to the server error when no refusal code came back', () => {
    const p = makeSessionsPage({ CustomEvent });
    assert.equal(p._createErrorText({ error: 'Unknown project' }, {}),
                 'Could not launch: Unknown project');
  });
});

describe('machine list never fetched', () => {
  it('starts the fetch and opens the chooser instead of launching here', () => {
    const fetched = [];
    const p = makeSessionsPage({ CustomEvent, fetch: (u) => { fetched.push(u); return new Promise(() => {}); } });
    const created = [];
    p.__window.addEventListener('create-terminal', (e) => created.push(e.detail));
    assert.equal(p.launchTargetsState, 'idle');
    assert.equal(p.pickWorkspace({ id: 'autonomy-docs', name: 'Docs' }), false);
    assert.deepEqual(Array.from(fetched), ['/api/fleet/launch-targets']);
    assert.equal(p.launchPanel, 'machines');
    assert.equal(created.length, 0);
  });
});
