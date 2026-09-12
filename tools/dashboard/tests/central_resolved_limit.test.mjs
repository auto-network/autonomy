import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

test('Resolved renders the first 100 matches without changing stored items or other views', () => {
  const context = {window: {}};
  vm.runInNewContext(fs.readFileSync(new URL('../static/js/components/central-attention.js', import.meta.url), 'utf8'), context);
  const ui = context.window.centralAttentionSurface();
  ui.categoryFilter = 'all';
  ui.items = Array.from({length: 150}, (_, id) => ({id, attentionState: 'resolved'}));
  ui.items.push(...Array.from({length: 120}, (_, id) => ({id: 150 + id, attentionState: 'needs_attention'})));
  ui.view = 'recent';
  assert.equal(ui.filteredItems().length, 100);
  assert.equal(ui.filteredItems()[99].id, 99);
  assert.equal(ui.items.length, 270);
  ui.view = 'needs';
  assert.equal(ui.filteredItems().length, 120);
});
