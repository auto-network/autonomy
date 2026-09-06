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
    window, document, Alpine, localStorage, console, fetch() { return Promise.resolve({ ok: false }); },
    setTimeout, clearTimeout, requestAnimationFrame(cb) { return setTimeout(cb, 0); }, cancelAnimationFrame: clearTimeout,
    Date, Math, Object, Array, JSON, Promise,
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(BOARD_JS, 'utf8'), sandbox, { filename: BOARD_JS });
  (listeners['alpine:init'] || []).forEach((cb) => cb());
  const board = components.sessionsBoard();
  board.$refs = {};
  board.$watch = () => {};
  board.rows = (opts.rows || []).map((id) => (typeof id === 'string' ? { id, session_id: id, tmux_session: id, label: 'Label ' + id, is_live: true, topics: [], org: null, session_type: 'interactive', project: 'p' } : id));
  return { board, logic: window.SessionBoardLogic, localStorage, localValues };
}

function ids(cols) { return cols.map((c) => c.id + ':' + c.members.join(',')); }
// Values built inside the vm realm have a different Object prototype; compare structure only.
function plain(v) { return JSON.parse(JSON.stringify(v)); }

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
  assert.equal(board.columns[1].title, 'Label s3');
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

test('persist round-trips columns, widths, heights and presentation through localStorage', () => {
  const { board, localStorage } = makeBoard({ rows: ['s1', 's2'] });
  board.columns = board.normalise([{ id: 'g', title: 'G', members: ['s2'], width: 600, _sized: true }]);
  board.cardHeights = { s1: 500 };
  board.presentation = 'stats';
  board.persist();
  const saved = JSON.parse(localStorage.getItem('sessions.board.layout'));
  assert.equal(saved.presentation, 'stats');
  assert.equal(saved.widths.g, 600);
  assert.equal(saved.heights.s1, 500);
  assert.deepEqual(saved.columns.map((c) => c.id + ':' + c.members.join(',')), ['solo:s1', 'g:s2']);
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
  assert.ok(html.includes("sessionViewerPage({mode:'panel'})"));
  assert.ok(html.includes('x-data="sessionsBoard()"'));
  assert.ok(html.includes('@click="bindDictation($event, id)"'));
  assert.ok(!/href="\/session\//.test(html), 'card click must bind dictation, not navigate');
  const js = fs.readFileSync(BOARD_JS, 'utf8');
  assert.ok(js.includes('ui.onClick(ev, ui.voiceBindKey(row)'), 'dictation binds through the standard voice path');
  assert.ok(!/store\('voice'\)\.(bindSession|requestBind)/.test(js), 'the board never binds the voice store directly');
});
