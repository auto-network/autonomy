// A waiting approval opens by itself, one at a time, each at most once per tab.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

function surface() {
  const store = new Map();
  const window = {sessionStorage: {getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, v)}};
  vm.runInNewContext(fs.readFileSync(new URL('../static/js/components/central-attention.js', import.meta.url), 'utf8'), {window});
  const ui = window.centralAttentionSurface();
  const opened = [];
  ui.openItem = async item => { opened.push(item.id); ui._sharedApprovalItem = item; return true; };
  return {ui, opened};
}
const approval = (id, state = 'needs_attention', rendererId = 'approval.jira_write.review') =>
  ({id, type: 'approval', attentionState: state, rendererId});
const settle = () => new Promise(r => setTimeout(r, 0));

test('the first waiting approval opens, and the next opens when it closes', async () => {
  const {ui, opened} = surface();
  ui.items = [approval('a'), approval('done', 'resolved'), approval('b')];
  ui.popNextApproval();
  await settle();
  assert.deepEqual(opened, ['a']);
  ui.popNextApproval();               // a dialog is open: nothing else pops
  await settle();
  assert.deepEqual(opened, ['a']);
  ui._sharedApprovalItem = null;       // the operator closes it undecided
  ui.popNextApproval();
  await settle();
  assert.deepEqual(opened, ['a', 'b']);
  ui._sharedApprovalItem = null;
  ui.popNextApproval();               // a was closed undecided: it never pops again
  await settle();
  assert.deepEqual(opened, ['a', 'b']);
});

test('a link to one approval holds the queue', async () => {
  const {ui, opened} = surface();
  ui.items = [approval('a')];
  ui._holdPop = true;
  ui.popNextApproval();
  await settle();
  assert.deepEqual(opened, []);
});

test('a kind still on the legacy panel never pops; the shared one behind it does', async () => {
  const {ui, opened} = surface();
  ui.items = [approval('sign', 'needs_attention', 'approval.commit_sign.review'), approval('jira')];
  ui.popNextApproval();
  await settle();
  assert.deepEqual(opened, ['jira']);
});
