/**
 * Mission Control home page list behaviour (bead auto-acyib).
 *
 * The five things that shipped in one day with no harness, ranked by the
 * bead: two damage-class (the default view must exclude completed missions
 * or the lifecycle control is a label that changes nothing; removal must
 * take two clicks or one click destroys a mission and its history) and
 * three correctness-class (org outer / status inner, chip counts describe
 * the org-scoped rows, the menu never offers a mission its current state).
 *
 * Tests the real component factory (page.js exports via module.exports)
 * with stubbed globals — no browser, per the test pillar's browser-free
 * convention. Run: node --test <this file>
 */
const { describe, it, beforeEach } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../../../..');
const PLUGIN_DIR = path.join(REPO_ROOT, 'tools/dashboard/plugins/mission_control');

// ── stub the classic-script environment before requiring page.js ──
const storage = new Map();
global.localStorage = {
  getItem: (k) => (storage.has(k) ? storage.get(k) : null),
  setItem: (k, v) => storage.set(k, String(v)),
  removeItem: (k) => storage.delete(k),
};
global.window = global.window || {};
global.document = global.document || { addEventListener: () => {} };
const fetchCalls = [];
let fetchResponder = () => ({ ok: false, json: async () => ({}) });
global.fetch = async (url, opts) => {
  fetchCalls.push({ url, opts: opts || {} });
  return fetchResponder(url, opts || {});
};

const { missionControlPage } = require(path.join(PLUGIN_DIR, 'page.js'));

function mission(id, org, status) {
  return { mission_id: id, org, status, name: id, open_question_count: 0 };
}

function freshPage(missions) {
  const page = missionControlPage();
  page.missions = missions;
  return page;
}

const FLEET = [
  mission('a1', 'autonomy', 'active'),
  mission('a2', 'autonomy', 'paused'),
  mission('a3', 'autonomy', 'complete'),
  mission('b1', 'anchore', 'active'),
];

beforeEach(() => {
  storage.clear();
  fetchCalls.length = 0;
  fetchResponder = () => ({ ok: false, json: async () => ({}) });
});

describe('default view — the reason the lifecycle control exists', () => {
  it('defaults to "current", which excludes completed missions', () => {
    const page = freshPage(FLEET);
    assert.equal(page.statusFilter, 'current');
    const ids = page.filteredMissions.map((m) => m.mission_id);
    assert.ok(!ids.includes('a3'), 'completed mission must leave the default view');
    assert.deepEqual(ids.sort(), ['a1', 'a2', 'b1']);
  });

  it('keeps Complete and All one chip away, with their own counts', () => {
    const page = freshPage(FLEET);
    const byValue = Object.fromEntries(page.statusFilters.map((f) => [f.value, f.count]));
    assert.equal(byValue.complete, 1);
    assert.equal(byValue.all, 4);
    page.statusFilter = 'complete';
    assert.deepEqual(page.filteredMissions.map((m) => m.mission_id), ['a3']);
  });
});

describe('removal takes two clicks, the second one destructive', () => {
  it('first click only arms; no request is issued', async () => {
    const page = freshPage(FLEET);
    await page.removeMission('a1');
    assert.equal(page.deleteArmed, 'a1');
    assert.equal(fetchCalls.length, 0, 'arming must not touch the network');
    assert.ok(page.missions.some((m) => m.mission_id === 'a1'));
  });

  it('second click deletes exactly the armed mission', async () => {
    const page = freshPage(FLEET);
    fetchResponder = () => ({ ok: true, json: async () => ({}) });
    await page.removeMission('a1');
    await page.removeMission('a1');
    assert.equal(fetchCalls.length, 1);
    assert.equal(fetchCalls[0].opts.method, 'DELETE');
    assert.ok(fetchCalls[0].url.includes('/api/missions/a1'));
    assert.ok(!page.missions.some((m) => m.mission_id === 'a1'));
    assert.equal(page.deleteArmed, '');
  });

  it('arming one mission then clicking another arms the other instead of deleting', async () => {
    const page = freshPage(FLEET);
    await page.removeMission('a1');
    await page.removeMission('a2');
    assert.equal(fetchCalls.length, 0, 'switching targets must re-arm, never delete');
    assert.equal(page.deleteArmed, 'a2');
  });

  it('reopening the menu disarms', async () => {
    const page = freshPage(FLEET);
    await page.removeMission('a1');
    page.toggleMenu('a1');
    assert.equal(page.deleteArmed, '', 'menu toggle must reset the armed state');
  });
});

describe('org outer, status inner', () => {
  it('org filter decides visibility; status narrows within it', () => {
    const page = freshPage(FLEET);
    page.selectedOrg = 'autonomy';
    page.statusFilter = 'active';
    assert.deepEqual(page.filteredMissions.map((m) => m.mission_id), ['a1']);
    page.statusFilter = 'all';
    assert.deepEqual(page.filteredMissions.map((m) => m.mission_id).sort(),
                     ['a1', 'a2', 'a3']);
  });

  it('chip counts describe the org-scoped rows, never the whole fleet', () => {
    const page = freshPage(FLEET);
    page.selectedOrg = 'anchore';
    const byValue = Object.fromEntries(page.statusFilters.map((f) => [f.value, f.count]));
    assert.equal(byValue.active, 1);
    assert.equal(byValue.paused, 0);
    assert.equal(byValue.complete, 0);
    assert.equal(byValue.all, 1, 'All counts the scoped set, not the fleet');
  });

  it('org selection persists', () => {
    const page = freshPage(FLEET);
    page.pickOrgFilter('anchore');
    assert.equal(storage.get('missionControlOrgFilter'), 'anchore');
  });
});

describe('the menu offers only transitions that apply', () => {
  // The gating lives in the template (x-show="m.status !== s"), which no
  // node test can execute. This pins the guard's presence in the markup so
  // its silent removal fails a test instead of shipping a menu that offers
  // a paused mission "Pause".
  it('template gates each status option on m.status !== s', () => {
    const html = fs.readFileSync(path.join(PLUGIN_DIR, 'page.html'), 'utf8');
    assert.match(html, /x-show="m\.status !== s"/,
      'menu options must be hidden for the state the mission is already in');
  });

  it('setStatus posts the transition and swaps the updated row in place', async () => {
    const page = freshPage(FLEET);
    fetchResponder = () => ({
      ok: true,
      json: async () => ({ mission: mission('a2', 'autonomy', 'active') }),
    });
    await page.setStatus('a2', 'active');
    assert.equal(fetchCalls.length, 1);
    assert.ok(fetchCalls[0].url.includes('/api/missions/a2/status'));
    assert.equal(JSON.parse(fetchCalls[0].opts.body).status, 'active');
    assert.equal(page.missions.find((m) => m.mission_id === 'a2').status, 'active');
    assert.equal(page.menuFor, '', 'acting closes the menu');
  });
});
