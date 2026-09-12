import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

function harness() {
  const factories = {};
  const context = {
    window: {location: {pathname: '/bead/epic', search: '?org=autonomy'}},
    document: {addEventListener: (_, fn) => fn()},
    Alpine: {data: (name, factory) => { factories[name] = factory; }},
    localStorage: {getItem: () => null}, URLSearchParams,
  };
  for (const path of ['bead-order.js', 'pages/beads.js', 'pages/bead-detail.js']) {
    vm.runInNewContext(fs.readFileSync(new URL('../static/js/' + path, import.meta.url), 'utf8'), context);
  }
  return {context, factories, sort: context.window.beadImplementationOrder};
}
const ids = rows => Array.from(rows, row => row.id);
const bead = (id, deps = [], extra = {}) => ({id, priority: 1, status: 'open',
  dependencies: deps.map(dep => ({type: 'blocks', depends_on_id: dep})), ...extra});

test('prerequisites precede dependents regardless of priority, status or update time', () => {
  const {sort} = harness();
  const rows = [bead('finish', ['left', 'right'], {priority: 0}),
    bead('right', ['start']), bead('left', ['start']),
    bead('start', [], {status: 'closed', priority: 4, updated_at: '2000-01-01'})];
  assert.deepEqual(ids(sort(rows)), ['start', 'left', 'right', 'finish']);
  assert.deepEqual(ids(rows), ['finish', 'right', 'left', 'start']);
});

test('independent siblings use priority then natural ID, not last touched time', () => {
  const {sort} = harness();
  assert.deepEqual(ids(sort([bead('task.10'), bead('task.2'), bead('urgent', [], {priority: 0})])),
    ['urgent', 'task.2', 'task.10']);
});

test('relationship and outside edges do not constrain siblings; duplicate edges do not lose rows', () => {
  const {sort} = harness();
  const a = bead('a', ['external']);
  a.dependencies.push({type: 'parent-child', depends_on_id: 'b'}, {type: 'relates-to', depends_on_id: 'b'});
  assert.deepEqual(ids(sort([bead('b', ['a', 'a']), a])), ['a', 'b']);
  assert.deepEqual(ids(sort([bead('b', [], {dependencies: [{dependency_type: 'blocks', id: 'a'}]}), a])), ['a', 'b']);
});

test('cycles and empty inputs terminate without dropping children', () => {
  const {sort} = harness();
  assert.deepEqual(ids(sort([])), []);
  assert.deepEqual(ids(sort([bead('b', ['a']), bead('a', ['b']), bead('c')])), ['c', 'a', 'b']);
});

test('tree orders the full sibling graph before filtering; progress still counts closed children', () => {
  const {factories} = harness();
  const ui = factories.beadsPage();
  const children = [bead('a-end', ['middle']), bead('middle', ['z-start']),
    bead('z-start', [], {status: 'closed'})];
  for (const child of children) child.dependencies.push({type: 'parent-child', depends_on_id: 'epic'});
  ui.allBeads = [{id: 'epic', issue_type: 'epic'}, ...children];
  Object.defineProperty(ui, 'filtered', {value: children.filter(b => b.id !== 'middle')});
  ui.query = 'filtered';
  const group = ui.treeGroups.groups[0];
  assert.deepEqual(ids(group.children), ['z-start', 'a-end']);
  assert.equal(group.total, 3);
  assert.equal(group.closed, 1);
  assert.equal(group.countLabel, '2/3');
});

test('detail page initial load exposes children in implementation order and keeps org routing', async () => {
  const {context, factories} = harness();
  const calls = [];
  context.fetch = async url => {
    calls.push(url);
    return {status: 200, json: async () => url.startsWith('/api/dao/bead/')
      ? {id: 'epic', children: [bead('a-end', ['z-start']), bead('z-start')]}
      : {}};
  };
  const ui = factories.beadDetailPage();
  ui.hydratePrimer = async () => {};
  ui.hydrateAuthorSession = async () => {};
  await ui.init();
  assert.equal(ui.state, 'ready');
  assert.deepEqual(ids(ui.bead.children), ['z-start', 'a-end']);
  assert.ok(calls.includes('/api/dao/bead/epic?org=autonomy'));
});
