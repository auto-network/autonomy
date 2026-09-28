/**
 * Sessions page: the machine chooser and remote Active cards (auto-mje3g,
 * design 64906530 revision f984e30b). Workspace first; a chooser only when
 * another machine can be launched on; remote rows become ordinary cards
 * whose machine is named and whose reachability is the session's own dot.
 */
const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const { SESSIONS_HTML, makeSessionsPage } = require('./sessions_page_harness');

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
    p.launchTargets = [HOME];
    const closes = p.pickWorkspace({ id: 'autonomy-docs', name: 'Docs' });
    assert.equal(closes, true);
    assert.equal(p.launchPanel, 'workspaces');
  });

  it('opens the chooser after a workspace when another machine exists', () => {
    const p = makeSessionsPage({ CustomEvent });
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

  it('states each machine\'s figures, and why one cannot be used', () => {
    const p = makeSessionsPage({ CustomEvent });
    assert.equal(p.launchTargetStats(SJC),
      '2 live · 47.3 GB RAM free · 812 GB disk free · load 0.04');
    assert.equal(p.launchTargetStats({ state: 'not_enabled', reason: 'session-cap-missing' }),
      'this machine is not armed for remote launch');
    assert.equal(p.launchTargetStats({ state: 'not_enabled', reason: 'session-control-not-negotiated' }),
      "this machine's relay does not offer remote launch yet");
    assert.match(p.launchTargetStats({ reachable: false, state: 'unreachable', unreachable_since: 1800000000 }),
      /^unreachable since /);
  });
});

describe('remote Active cards', () => {
  it('map a remote registry row to a card that names its machine', () => {
    const p = makeSessionsPage({ CustomEvent });
    const card = p._remoteCard({ session_id: 'auto-9@sjc-2', label: 'Sweep', role: 'reviewer',
      topics: ['t'], last_message: 'hi', machine: 'sjc-2', machine_pub: 'b1',
      machine_reachable: false, machine_state: 'unreachable', machine_unreachable_since: 1800000000 });
    assert.equal(card.session_id, 'auto-9@sjc-2');
    assert.equal(card.tmux_session, 'auto-9@sjc-2');
    assert.equal(card.machine, 'sjc-2');
    assert.equal(card.machine_reachable, false);
    assert.equal(card.latest, 'hi');
    assert.match(p.machineTitle(card), /^sjc-2 unreachable since /);
    assert.equal(p.machineTitle({ machine: 'sjc-2', machine_reachable: true }), 'Runs on sjc-2');
    assert.equal(p.machineTitle({}), '');
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
  });
});
