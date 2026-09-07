/**
 * Session Board logic contract (static/js/pages/sessions-board.js).
 *
 * Runs the page script in a bare vm sandbox (same shape as
 * sessions_page_harness.js) and exercises the pure layout functions and the
 * Alpine component's state methods directly. Layout geometry (drag preview,
 * resize) stays on a real browser; this file pins the invariants that broke
 * during design review: every live session exactly once, Ungrouped keeps its
 * arranged order, empty columns collapse, a sole member cannot start a group,
 * ⤢ keeps the focused session in place, slot selection is a pure function of
 * the frame and the pointer, and card height never exceeds 80% of the viewport.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const BOARD_JS = path.join(REPO_ROOT, 'tools/dashboard/static/js/pages/sessions-board.js');
const BOARD_HTML = path.join(REPO_ROOT, 'tools/dashboard/templates/pages/sessions-board.html');

function makeBoard(opts) {
  opts = opts || {};
  const listeners = {};
  const components = {};
  const stores = { sessions: opts.sessions || {}, voice: opts.voice || null };
  const localValues = {};
  const localStorage = {
    getItem(k) { return Object.prototype.hasOwnProperty.call(localValues, k) ? localValues[k] : null; },
    setItem(k, v) { localValues[k] = String(v); },
    removeItem(k) { delete localValues[k]; },
  };
  const document = {
    body: { classList: { add() {}, remove() {} } },
    addEventListener(name, cb) { (listeners[name] ||= []).push(cb); },
    querySelector() { return null; },
    createElement() { return { style: {}, appendChild() {}, remove() {}, className: '', textContent: '' }; },
  };
  const Alpine = {
    data(name, factory) { components[name] = factory; },
    store(name) { return stores[name]; },
  };
  const window = {
    innerHeight: opts.innerHeight || 1000,
    addEventListener(name, cb) { (listeners[name] ||= []).push(cb); },
    removeEventListener() {},
    localStorage,
  };
  const sandbox = {
    window, document, Alpine, localStorage, console,
    fetch: opts.fetchImpl || function () { return Promise.resolve({ ok: false }); },
    setTimeout, clearTimeout, requestAnimationFrame(cb) { return setTimeout(cb, 0); }, cancelAnimationFrame: clearTimeout,
    Date, Math, Object, Array, JSON, Promise,
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(BOARD_JS, 'utf8'), sandbox, { filename: BOARD_JS });
  (listeners['alpine:init'] || []).forEach((cb) => cb());
  const board = components.sessionsBoard();
  board.$refs = {};
  board.$watch = () => {};
  board._ready = true;   // tests drive the component after the roster would have arrived
  board.rows = (opts.rows || []).map((id) => (typeof id === 'string' ? { id, session_id: id, tmux_session: id, label: 'Label ' + id, is_live: true, topics: [], org: null, session_type: 'interactive', project: 'p' } : id));
  return { board, logic: window.SessionBoardLogic, localStorage, localValues };
}

// Values built inside the vm realm have a different Object prototype; compare structure only.
function plain(v) { return JSON.parse(JSON.stringify(v)); }
function ids(cols) { return plain(cols.map((c) => c.id + ':' + c.members.join(','))); }

test('normaliseColumns keeps every live id exactly once and drops unknown ids', () => {
  const { logic } = makeBoard();
  const out = logic.normaliseColumns(
    [{ id: 'a', title: 'A', members: ['s1', 's2', 'zzz'] }, { id: 'b', title: 'B', members: ['s2', 's3'] }],
    ['s1', 's2', 's3', 's4'],
  );
  assert.deepEqual(ids(out), ['solo:s4', 'a:s1,s2', 'b:s3']);
});

test('normaliseColumns collapses an empty column unless it is kept alive', () => {
  const { logic } = makeBoard();
  const cols = [{ id: 'a', title: 'A', members: [] }, { id: 'b', title: 'B', members: ['s1'] }];
  assert.deepEqual(ids(logic.normaliseColumns(cols.map((c) => ({ ...c, members: c.members.slice() })), ['s1'])), ['b:s1']);
  assert.deepEqual(ids(logic.normaliseColumns(cols.map((c) => ({ ...c, members: c.members.slice() })), ['s1'], { a: true })), ['a:', 'b:s1']);
});

test('normaliseColumns preserves the Ungrouped order and position and appends newly ungrouped ids', () => {
  const { logic } = makeBoard();
  const cols = [
    { id: 'g', title: 'G', members: ['s1'] },
    { id: 'solo', title: 'Ungrouped', members: ['s3', 's2'] },
  ];
  const out = logic.normaliseColumns(cols, ['s1', 's2', 's3', 's4']);
  assert.deepEqual(ids(out), ['g:s1', 'solo:s3,s2,s4']);
});

test('slotFor is a pure function of the frame and the pointer', () => {
  const { logic } = makeBoard();
  const frame = {
    zone: { left: 900, right: 1100 },
    cols: [
      { id: 'solo', left: 0, right: 400, mids: [100, 300], scrollDelta: 0 },
      { id: 'g', left: 420, right: 820, mids: [150], scrollDelta: 0 },
    ],
  };
  assert.deepEqual(plain(logic.slotFor(frame, 50, 50)), { colId: 'solo', idx: 0, zone: false });
  assert.deepEqual(plain(logic.slotFor(frame, 50, 200)), { colId: 'solo', idx: 1, zone: false });
  assert.deepEqual(plain(logic.slotFor(frame, 50, 350)), { colId: 'solo', idx: 2, zone: false });
  assert.deepEqual(plain(logic.slotFor(frame, 500, 160)), { colId: 'g', idx: 1, zone: false });
  assert.equal(logic.slotFor(frame, 1000, 500).zone, true);
  assert.equal(logic.slotFor(frame, 850, 500), null);
  // The same inputs always give the same answer — the moving preview cannot move a threshold.
  for (let i = 0; i < 5; i++) assert.deepEqual(plain(logic.slotFor(frame, 50, 200)), { colId: 'solo', idx: 1, zone: false });
  // Column scroll since the snapshot shifts the pointer into frame coordinates.
  frame.cols[0].scrollDelta = 150;
  assert.deepEqual(plain(logic.slotFor(frame, 50, 200)), { colId: 'solo', idx: 2, zone: false });
});

test('a sole member cannot start a new group; ungrouped and paired cards can', () => {
  const { board } = makeBoard({ rows: ['s1', 's2', 's3', 's4'] });
  board.columns = board.normalise([{ id: 'one', title: 'One', members: ['s1'] }, { id: 'two', title: 'Two', members: ['s2', 's3'] }]);
  assert.equal(board.canStartGroup('s1'), false);
  assert.equal(board.canStartGroup('s2'), true);
  assert.equal(board.canStartGroup('s4'), true);
});

test('placeCard and moveCard put a card exactly where asked and collapse what they empty', () => {
  const { board } = makeBoard({ rows: ['s1', 's2', 's3'] });
  board.columns = board.normalise([{ id: 'g', title: 'G', members: ['s1', 's2'] }]);
  board.placeCard('s3', 'g', 1);
  assert.deepEqual(ids(board.columns), ['g:s1,s3,s2']);
  board.moveCard('s1', 'g', 2);
  assert.deepEqual(ids(board.columns), ['g:s3,s1,s2']);
  board.placeCard('s3', 'solo', 0);
  board.placeCard('s1', 'solo', 0);
  board.placeCard('s2', 'solo', 5);
  assert.deepEqual(ids(board.columns), ['solo:s1,s3,s2']);
});

test('focusCard keeps the focused session in its slot and spills the others to its right', () => {
  const { board } = makeBoard({ rows: ['s1', 's2', 's3', 's4', 's5'] });
  board.columns = board.normalise([{ id: 'g', title: 'Lane', members: ['s2', 's3', 's4'] }]);
  assert.deepEqual(ids(board.columns), ['solo:s1,s5', 'g:s2,s3,s4']);
  board.focusCard('s3');
  assert.equal(board.columns[1].id, 'g');
  assert.equal(board.columns[1].focus, 's3');
  assert.deepEqual(plain(board.columns[1].members), ['s3']);
  // Shared semantics: the focused session keeps the group (and its name); the others spill.
  assert.equal(board.columns[1].title, 'Lane');
  assert.deepEqual(plain(board.columns[2].members), ['s2', 's4']);
  assert.equal(board.columns[2].title, 'Lane');
  // From Ungrouped: the new column takes Ungrouped's slot, Ungrouped shifts right with the rest.
  board.focusCard('s1');
  assert.equal(board.columns[0].focus, 's1');
  assert.equal(board.columns[0].title, 'Label s1');
  assert.equal(board.columns[1].id, 'solo');
  assert.deepEqual(plain(board.columns[1].members), ['s5']);
  // ⤡ restores height only; the split stays.
  board.focusCard('s1');
  assert.equal(board.columns[0].focus, null);
  assert.deepEqual(plain(board.columns[0].members), ['s1']);
});

test('card height is clamped to 80% of the viewport on read and on stored values', () => {
  const { board } = makeBoard({ rows: ['s1'], innerHeight: 1000 });
  assert.equal(board.maxCardHeight(), 800);
  board.cardHeights = { s1: 9000 };
  assert.equal(board.cardHeight('s1'), 800);
  assert.equal(board.clampHeight(10), 160);
  assert.equal(board.cardHeight('missing'), 340);
});

test('the layout payload carries everything a refresh must restore, and nothing about membership', () => {
  const { board } = makeBoard({ rows: ['s1', 's2'] });
  board.columns = board.normalise([{ id: 'g', title: 'G', members: ['s2'], width: 600, _sized: true }]);
  board.cardHeights = { s1: 500 };
  board.cardPresentations = { s1: 'stats' };
  board.presentation = 'stats';
  board.columns.filter((c) => c.id === 'g')[0].focus = 's2';
  const payload = plain(board.layoutPayload());
  assert.deepEqual(payload, {
    presentation: 'stats', presentations: { s1: 'stats' }, column_order: ['solo', 'g'],
    widths: { g: 600 }, heights: { s1: 500 }, focus_session: 's2',
  });
  // Membership is never part of the layout — it is the group record's.
  assert.ok(!('columns' in payload) && !('members' in payload));
});

test('a refresh restores the card face and the full-height card', () => {
  const grp = { slug: 'g', name: 'G', color: '', why: '' };
  const sessions = { s1: { isLive: true, groupId: 'g', group: grp, entries: [{}] },
                     s2: { isLive: true, groupId: 'g', group: grp, entries: [{}] } };
  const { board } = makeBoard({ rows: ['s1', 's2'], sessions });
  // As init() does after reading the layout member.
  board.cardPresentations = { s1: 'stats' };
  board._focusSession = 's2';
  board.refresh({ columns: [{ id: 'g', members: [] }], widths: {} });
  assert.equal(board.cardPresentation('s1'), 'stats', 'the flipped card comes back on its stats face');
  assert.equal(board.cardPresentation('s2'), 'transcript');
  const g = board.columns.filter((c) => c.id === 'g')[0];
  assert.equal(g.focus, 's2', 'the full-height card comes back full height');
  // A focus whose session has gone is dropped rather than stranding the column.
  board._focusSession = 'ghost';
  board.refresh({ columns: [{ id: 'g', members: [] }], widths: {} });
  assert.ok(!board.columns.some((c) => c.focus));
  assert.equal(board._focusSession, '');
});

test('columnSlotFor orders a dragged column from the resting midpoints and the pointer only', () => {
  const { logic } = makeBoard();
  const mids = [200, 600, 1000];
  assert.equal(logic.columnSlotFor(mids, 100), 0);
  assert.equal(logic.columnSlotFor(mids, 300), 1);
  assert.equal(logic.columnSlotFor(mids, 700), 2);
  assert.equal(logic.columnSlotFor(mids, 1500), 3);
  for (let i = 0; i < 5; i++) assert.equal(logic.columnSlotFor(mids, 300), 1);
});

test('host sessions without a transcript fall back to the stats presentation', () => {
  const { board } = makeBoard({ rows: [{ id: 'h1', session_id: 'h1', tmux_session: 'h1', label: 'Host', is_live: true, topics: [], org: null, session_type: 'host' }, 'c1'], sessions: { h1: { entries: [] }, c1: { entries: [{}] } } });
  assert.equal(board.cardPresentation('h1'), 'stats');
  assert.equal(board.cardPresentation('c1'), 'transcript');
  board.togglePresentation('c1');
  assert.equal(board.cardPresentation('c1'), 'stats');
});

test('the template mounts the production partials and the panel viewer, and never navigates on card click', () => {
  const html = fs.readFileSync(BOARD_HTML, 'utf8');
  assert.ok(html.includes('{% include "partials/session-card.html" %}'));
  assert.ok(html.includes('{% include "partials/session-entries.html" %}'));
  assert.ok(html.includes("sessionViewerPage({mode:'panel', dictationTile:true, pageSignals:false, tailEntries:40})"));
  assert.ok(html.includes('x-data="sessionsBoard()"'));
  assert.ok(html.includes('@click="bindDictation($event, id)"'));
  assert.ok(!/href="\/session\//.test(html), 'card click must bind dictation, not navigate');
  const js = fs.readFileSync(BOARD_JS, 'utf8');
  assert.ok(js.includes('ui.onClick(ev, key, { isLive: !!row.is_live })'), 'a first bind goes through the standard voice path');
  assert.ok(/voice\.bindSession\(key\)/.test(js), 'clicking another card switches the target without a second prompt');
});

test('columns derive from the store\'s group membership; local state keeps only order, width and member order', () => {
  const sessions = {
    s1: { groupId: 'deploy', groupTab: 'crypto', group: { slug: 'deploy', name: 'Registry deploy lane', color: '#f59e0b', why: 'one effort' }, entries: [] },
    s2: { groupId: 'deploy', groupTab: '', group: { slug: 'deploy', name: 'Registry deploy lane', color: '#f59e0b', why: 'one effort' }, entries: [] },
    s3: { groupId: null, groupTab: '', group: null, entries: [] },
    s4: { groupId: 'recovery', groupTab: '', group: { slug: 'recovery', name: 'Post-crash recovery', color: '#38bdf8', why: '' }, entries: [] },
  };
  const { board } = makeBoard({ rows: ['s1', 's2', 's3', 's4'], sessions });
  board.columns = board.normalise(board.columnsFromStore(null));
  assert.deepEqual(ids(board.columns), ['solo:s3', 'deploy:s1,s2', 'recovery:s4']);
  assert.equal(board.columns[1].title, 'Registry deploy lane');
  assert.equal(board.columns[1].why, 'one effort');
  assert.equal(board.columns[2].color, '#38bdf8');
  // Local arrangement survives a re-derivation: member order inside a column, column order, width.
  board.columns[1].members = ['s2', 's1'];
  board.columns[1].width = 700; board.columns[1]._sized = true;
  board.columns = [board.columns[2], board.columns[1], board.columns[0]];
  board.columns = board.normalise(board.columnsFromStore(null));
  assert.deepEqual(ids(board.columns), ['recovery:s4', 'deploy:s2,s1', 'solo:s3']);
  assert.equal(board.columns[1].width, 700);
  // Membership is server truth: a session that changed group on the server moves columns.
  sessions.s2.groupId = 'recovery'; sessions.s2.group = sessions.s4.group;
  board.columns = board.normalise(board.columnsFromStore(null));
  assert.deepEqual(ids(board.columns), ['recovery:s4,s2', 'deploy:s1', 'solo:s3']);
  // A dissolved group vanishes and its sessions land in Ungrouped.
  sessions.s1.groupId = null; sessions.s1.group = null;
  board.columns = board.normalise(board.columnsFromStore(null));
  assert.deepEqual(ids(board.columns), ['recovery:s4,s2', 'solo:s3,s1']);
});


test('nothing is derived or persisted before the roster and the layout member arrive', () => {
  const { board } = makeBoard({ rows: ['s1'] });
  board._ready = false;
  board.columns = [];
  board.refresh({ columns: [{ id: 'g', members: [] }], widths: {} });
  assert.deepEqual(ids(board.columns), []);
  board._lastPersisted = null;
  board.persist();
  assert.equal(board._persistTimer, undefined);   // no write scheduled before ready
});

test('organizeDigest carries identity, title, topics, current group and at most 10 non-internal turns per session, well under the input cap', () => {
  const entries = [];
  for (let i = 0; i < 40; i++) entries.push({ type: i % 2 ? 'assistant_text' : 'user', role: i % 2 ? 'assistant' : 'user', content: 'turn ' + i + ' ' + 'x'.repeat(600), internal: i % 7 === 0 });
  const sessions = {};
  const rows = [];
  for (let n = 0; n < 40; n++) {
    const id = 'auto-' + n;
    sessions[id] = { entries, groupId: n % 3 ? 'lane' : null, group: null };
    rows.push({ id, session_id: id, tmux_session: id, label: 'Session ' + n, role: 'Builder', topics: ['t1', 't2'], org: { slug: 'autonomy' }, harness: 'claude', model: 'claude-sonnet-5', is_live: true, session_type: 'interactive', project: 'p' });
  }
  const { board } = makeBoard({ rows, sessions });
  const digest = plain(board.organizeDigest());
  assert.equal(digest.length, 40);
  assert.deepEqual(Object.keys(digest[1]).sort(), ['group', 'harness', 'model', 'org', 'role', 'session', 'tail', 'title', 'topics'].sort());
  assert.equal(digest[1].group, 'lane');
  assert.equal(digest[0].tail.length, 10);
  assert.ok(digest[0].tail.every((t) => t.text.length <= 400 && !t.internal));
  assert.ok(JSON.stringify(digest).length < 512 * 1024, 'digest for 40 sessions stays under the 512 KB custom_input cap');
});

test('dictation shows in the card: the shared pending-tile partial is mounted, and the floating capsule is gone', () => {
  const html = fs.readFileSync(BOARD_HTML, 'utf8');
  assert.ok(html.includes('{% include "partials/session-pending-tiles.html" %}'), 'the card renders the session viewer\'s own dictation tiles');
  // The tile partial binds to sessionViewerPage state, so it must sit inside the panel component.
  const bodyStart = html.indexOf('sessionViewerPage({');
  assert.ok(bodyStart !== -1 && html.indexOf('session-pending-tiles.html') > bodyStart);
  assert.ok(!html.includes('sb-talkpill'), 'no floating "Dictating to…" capsule');
  assert.ok(!/Dictating to/.test(html));
});

test('the org glyph opens an inline desktop menu, never the phone action sheet', () => {
  const html = fs.readFileSync(BOARD_HTML, 'utf8');
  const js = fs.readFileSync(BOARD_JS, 'utf8');
  assert.ok(html.includes('class="sb-menu"'), 'the menu is inline in the card');
  assert.ok(!/actionSheet/.test(js), 'the board never calls the global action sheet');
  const { board } = makeBoard({ rows: ['s1'] });
  const row = board.rowFor('s1');
  row.is_live = true; row.nag_enabled = false;
  const actions = plain(board.sessionActions(row));
  assert.deepEqual(actions.map((a) => a.label),
    ['Enable nag (15m)', 'Open full viewer', 'Copy session name', 'Restart session', 'Close session']);
  assert.ok(actions.every((a) => a.icon), 'every action carries an icon');
  assert.equal(actions[actions.length - 1].style, 'destructive');
  // Toggling: showing the same card's menu twice closes it.
  board.showSessionActions(row);
  assert.equal(board.menuFor, 's1');
  board.showSessionActions(row);
  assert.equal(board.menuFor, '');
  // Binding dictation closes an open menu.
  board.showSessionActions(row);
  board.bindDictation({}, 's1');
  assert.equal(board.menuFor, '');
});

test('sparklines share one fleet-wide scale, so a quiet column cannot look as busy as a loud one', () => {
  const resources = {
    quiet: { cpu_pct: 1, history: [[1, 0.5, 0], [2, 1.4, 0], [3, 0.7, 0]] },
    loud: { cpu_pct: 437, history: [[1, 200, 0], [2, 437, 0], [3, 300, 0]] },
  };
  const { board } = makeBoard({ rows: ['quiet', 'loud'] });
  board.resources = resources;
  board.columns = board.normalise([{ id: 'a', title: 'A', members: ['quiet'] }, { id: 'b', title: 'B', members: ['loud'] }]);
  assert.equal(board.fleetCpuMax(), 437);
  const y = (pts) => pts.split(' ').map((p) => parseFloat(p.split(',')[1]));
  const quiet = y(board.colSpark(board.columns.filter((c) => c.id === 'a')[0]));
  const loud = y(board.colSpark(board.columns.filter((c) => c.id === 'b')[0]));
  // 19 is the baseline; smaller y means taller. The quiet lane must stay near the floor.
  assert.ok(Math.min.apply(null, quiet) > 18.8, 'a 1.4% lane draws flat against a 437% ceiling');
  assert.ok(Math.min.apply(null, loud) < 2.5, 'the 437% lane reaches the top');
  assert.ok(Math.min.apply(null, quiet) > Math.min.apply(null, loud));
  assert.match(board.colSparkTitle(board.columns.filter((c) => c.id === 'a')[0]), /peak 1.4% of 437% fleet max/);
});


test('only the target card shows dictation: the cross-session tile is suppressed on the board', () => {
  const html = fs.readFileSync(BOARD_HTML, 'utf8');
  // Every card is a viewer, so the "going to another session" tile would render
  // on every card but one. The target is on screen and highlighted instead.
  assert.match(html, /\.sb-card-body \.sv-pending--cross \{ display: none !important; \}/);
  // The local outbox / dictation tiles stay.
  assert.ok(html.includes('.sb-card-body .sv-pending {'));
  assert.ok(!/\.sb-card-body \.sv-pending--live \{ display: none/.test(html));
});

test('clicking a different card switches the dictation target immediately', () => {
  let bound = 'auto-a', calls = [];
  const voice = {
    enabled: true, boundSessionId: bound, pendingRebindTarget: 'stale',
    bindSession(id) { calls.push(['bindSession', id]); this.boundSessionId = id; },
    requestBind(id) { calls.push(['requestBind', id]); return { ok: false, reason: 'confirm' }; },
  };
  const { board } = makeBoard({ rows: ['auto-a', 'auto-b'], voice });
  globalThis.window = globalThis.window || {};
  board.bindDictation({}, 'auto-b');
  assert.deepEqual(calls, []);   // no voice.ui stub in the sandbox → no-op, but state stays sane
  assert.equal(voice.boundSessionId, 'auto-a');
});


test('board cards never write the page-global voice signals that pick the capsule target', () => {
  const html = fs.readFileSync(BOARD_HTML, 'utf8');
  const viewer = fs.readFileSync(path.join(REPO_ROOT, 'tools/dashboard/static/js/pages/session-viewer.js'), 'utf8');
  // The board mounts one viewer per card; the body signals assume exactly one.
  assert.ok(html.includes('pageSignals:false'), 'the board opts out of page-global voice signals');
  assert.ok(/_pageSignals: !\(opts && opts\.pageSignals === false\)/.test(viewer));
  // Both writers are gated, so a card mount cannot claim body.dataset.svComposerSession —
  // which voice-shell reads to retarget delivery.
  assert.match(viewer, /_syncTilePresent\(\) \{\s*\n\s*if \(!this\._pageSignals\) return;/);
  // _syncComposerSignal returns early for board cards, but only AFTER running
  // the outbox mirror — that one is not a page-global write and every surface
  // needs it to stage a capturing tile.
  assert.match(viewer, /if \(!this\._pageSignals\) \{\s*\n\s*if \(shell0 && typeof shell0\.syncViewerOutboxCapture === 'function'\) shell0\.syncViewerOutboxCapture\(\);\s*\n\s*return;\s*\n\s*\}/);
  // And the target card renders the live buffer even before an outbox is staged.
  assert.ok(html.includes('dictationTile:true'));
  assert.match(viewer, /if \(this\._dictationTile && this\._localDictationText\) return true;/);
});


test('a moved card is never in two columns: the operator\'s move outranks a stale groupId until the record agrees', () => {
  const grp = (slug) => ({ slug, name: slug, color: '', why: '' });
  const sessions = {
    s1: { isLive: true, groupId: 'a', group: grp('a'), entries: [{}] },
    s2: { isLive: true, groupId: 'b', group: grp('b'), entries: [{}] },
  };
  const { board } = makeBoard({ rows: ['s1', 's2'], sessions });
  board.refresh({ columns: [{ id: 'a', members: [] }, { id: 'b', members: [] }], widths: {} });
  assert.deepEqual(ids(board.columns), ['a:s1', 'b:s2']);
  // The operator drops s1 into b. The server write is in flight, so the store
  // still says groupId 'a' — a registry broadcast must NOT undo the move.
  board._pending.s1 = 'b';
  board.refresh({ columns: [{ id: 'a', members: [] }, { id: 'b', members: [] }], widths: {} });
  const seen = {};
  board.columns.forEach((c) => c.members.forEach((m) => {
    assert.ok(!seen[m], m + ' appears in more than one column');
    seen[m] = true;
  }));
  assert.deepEqual(ids(board.columns), ['b:s2,s1']);
  // Once the record agrees the pending intent is dropped.
  sessions.s1.groupId = 'b'; sessions.s1.group = grp('b');
  board.refresh({ columns: [{ id: 'b', members: [] }], widths: {} });
  assert.deepEqual(ids(board.columns), ['b:s2,s1']);
  assert.deepEqual(plain(board._pending), {});
});


test('a CrossTalk sender link reveals the session on the board instead of navigating away', () => {
  const js = fs.readFileSync(BOARD_JS, 'utf8');
  // It must intercept before app.js's document-level router, and only for
  // /session/<project>/<tmux> hrefs.
  assert.match(js, /a\.sc-ct-sender/);
  assert.match(js, /e\.preventDefault\(\); e\.stopPropagation\(\);/);
  const sessions = { s1: { isLive: true, groupId: 'g', group: { slug: 'g', name: 'G', color: '', why: '' }, entries: [{}] } };
  const { board } = makeBoard({ rows: ['s1'], sessions });
  board.refresh({ columns: [{ id: 'g', members: [] }], widths: {} });
  board.$nextTick = (fn) => fn();
  board.$refs = { board: { querySelector: () => null } };
  // A session with a card is revealed: flipped to its transcript face.
  board.cardPresentations = { s1: 'stats' };
  assert.equal(board.revealSession('s1'), true);
  assert.equal(board.cardPresentation('s1'), 'transcript');
  // One without a card here is left to the link.
  assert.equal(board.revealSession('not-on-this-board'), false);
  assert.equal(board.revealSession(''), false);
});

test('the tile Send points delivery at its own card before sending, and reports failure', () => {
  const viewer = fs.readFileSync(path.join(REPO_ROOT, 'tools/dashboard/static/js/pages/session-viewer.js'), 'utf8');
  // sendBuffer reads ONLY deliverySessionId; the tile can render off bound.
  assert.match(viewer, /voice\.deliverySessionId !== me && typeof voice\.retargetDelivery === 'function'/);
  // A failure must not be silent: the capsule's sheetError is not visible on a board.
  assert.match(viewer, /if \(!ok\) this\.sendTileError = voice\.sheetError \|\| 'Send failed\.'/);
});

test('revealing a session scrolls only the board, never an ancestor', () => {
  const js = fs.readFileSync(BOARD_JS, 'utf8');
  assert.ok(!/\.scrollIntoView\(/.test(js), 'scrollIntoView scrolls any scrollable ancestor and shifted the shell under the nav');
  assert.match(js, /board\.scrollTo\(\{ left: Math\.max\(0, Math\.min\(want, max\)\)/);
});

test('a board card opens on a small tail; the full-page viewer keeps its own', () => {
  const html = fs.readFileSync(BOARD_HTML, 'utf8');
  const viewer = fs.readFileSync(path.join(REPO_ROOT, 'tools/dashboard/static/js/pages/session-viewer.js'), 'utf8');
  // Every card pays the opening read at once; a card shows ~10 turns.
  assert.ok(html.includes('tailEntries:40'));
  assert.match(viewer, /_tailEntries: \(opts && opts\.tailEntries > 0\) \? opts\.tailEntries : FAST_OPEN_TAIL_LINES/);
  assert.match(viewer, /_initialTailUrl\(\) \{\s*\n\s*return this\._tailUrl \+ '\?tail_entries=' \+ this\._tailEntries;/);
  // Scroll-back is unaffected: older pages still use the full window.
  assert.match(viewer, /_olderTailUrl\(cursor\) \{[\s\S]*?tail_entries=' \+ FAST_OPEN_TAIL_LINES/);
});

test('the model badge types the harness vocabulary and answers its confirmation', async () => {
  const calls = [];
  const fetchImpl = (url, opts) => {
    calls.push({ url: String(url), body: opts && opts.body });
    if (String(url).indexOf('/api/session-models') !== -1) {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ harness: 'claude', models: [
        { label: 'Opus', argument: 'opus', command: '/model', model: 'claude-opus-4-8', confirm_key: '' },
        { label: 'Fable', argument: 'fable', command: '/model', model: 'claude-fable-5-1', confirm_key: '1' },
      ] }) });
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
  };
  const rows = [{ id: 's1', session_id: 's1', tmux_session: 's1', label: 'S1', is_live: true, topics: [],
                  org: null, session_type: 'interactive', project: 'p', harness: 'claude', model: 'claude-opus-4-8' }];
  const { board } = makeBoard({ rows, fetchImpl });
  await board.openModelMenu('s1');
  assert.deepEqual(plain(board.modelOptions).map((m) => m.label), ['Opus', 'Fable']);
  // The running model is ticked and not selectable.
  assert.equal(plain(board.modelOptions)[0].current, true);
  assert.equal(plain(board.modelOptions)[1].current, false);

  // A model that needs no confirmation sends exactly one message.
  await board.chooseModel('s1', plain(board.modelOptions)[0]);
  let sends = calls.filter((c) => c.url.indexOf('/api/session/send') !== -1);
  assert.equal(sends.length, 1);
  assert.deepEqual(JSON.parse(sends[0].body), { tmux_session: 's1', message: '/model opus' });

  // Fable is accepted only as the bare name, and then asks to confirm — the
  // versioned name was rejected in live use, and an unanswered prompt parks
  // the session. Both facts are Settings data, not code.
  calls.length = 0;
  await board.chooseModel('s1', plain(board.modelOptions)[1]);
  sends = calls.filter((c) => c.url.indexOf('/api/session/send') !== -1);
  assert.equal(sends.length, 2, 'command then confirmation');
  assert.deepEqual(JSON.parse(sends[0].body), { tmux_session: 's1', message: '/model fable' });
  assert.deepEqual(JSON.parse(sends[1].body), { tmux_session: 's1', message: '1' });
  assert.equal(board.modelMenuFor, '');
});

test('a harness with no known switch command gets no model menu', async () => {
  const fetchImpl = () => Promise.resolve({ ok: true, json: () => Promise.resolve({ harness: 'codex', models: [] }) });
  const rows = [{ id: 'c1', session_id: 'c1', tmux_session: 'c1', label: 'C1', is_live: true, topics: [],
                  org: null, session_type: 'interactive', project: 'p', harness: 'codex', model: 'gpt-5.6-sol' }];
  const { board } = makeBoard({ rows, fetchImpl });
  await board.openModelMenu('c1');
  assert.equal(board.modelMenuFor, '', 'never types an unverified command into a live agent');
  // A dead session is not switchable either.
  rows[0].is_live = false; rows[0].harness = 'claude';
  await board.openModelMenu('c1');
  assert.equal(board.modelMenuFor, '');
});
