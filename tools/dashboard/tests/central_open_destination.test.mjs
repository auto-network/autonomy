import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const SOURCE = fs.readFileSync(new URL('../static/js/components/central-attention.js', import.meta.url), 'utf8');

function backupItem() {
  return {
    id: 'backup:drill', sourceVersion: 20260913064414, applicationScope: 'backup',
    type: 'application', category: 'apps', rendererId: 'backup.item.v1',
    summary: 'Drill 20260913-064414: verdict fail', actions: [], destinationHref: null,
  };
}

function safeItem() {
  return {
    attention_id: 'backup:drill', source_version: 20260913064414,
    application: {scope: 'backup', label: 'Backup'}, category: 'apps',
    participant_role: 'recipient', attention_state: 'needs_attention',
    title: 'Restore drill failed', summary: 'Drill 20260913-064414: verdict fail',
    occurred_at: 100, open: {mode: 'registered_renderer', renderer_id: 'backup.item.v1'},
  };
}

function surface(review, {status = 200} = {}) {
  const calls = [];
  const assigned = [];
  const window = {location: {origin: 'https://dashboard.test', search: '', assign: href => assigned.push(href)}};
  const context = {
    window,
    fetch: async (url, options) => {
      calls.push({url, method: (options && options.method) || 'GET'});
      const payload = url.endsWith('/opened') ? {presentation: {}} : {item: safeItem(), review};
      return {ok: status < 400, status, json: async () => (status < 400 ? payload : {error: 'review_unavailable', item: safeItem()})};
    },
  };
  vm.runInNewContext(SOURCE, context);
  return {ui: context.window.centralAttentionSurface(), calls, assigned};
}

test('an application item opens its server-named destination and posts the opened receipt', async () => {
  const {ui, calls, assigned} = surface({
    type: 'application', renderer_id: 'backup.item.v1', kind: 'backup.drill_failed',
    destination: {href: '/backup'}, actions: [],
  });
  const item = backupItem();
  assert.equal(await ui.openItem(item), true);
  assert.equal(item.unavailable, false);
  assert.equal(item.destinationHref, '/backup');
  assert.equal(item.sourceLabel, 'backup · backup.drill_failed');
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(calls.map(call => call.method + ' ' + call.url), [
    'GET /api/attention/items/backup%3Adrill',
    'POST /api/attention/items/backup%3Adrill/opened',
  ]);
  ui.openDestination(item);
  assert.equal(ui.selectedItem, null);
  assert.deepEqual(assigned, ['/backup']);
});

test('only a same-origin path is a destination', async () => {
  const {ui, assigned} = surface({
    type: 'application', renderer_id: 'backup.item.v1', kind: 'backup.drill_failed',
    destination: {href: 'https://elsewhere.test/backup'}, actions: [],
  });
  const item = backupItem();
  await ui.openItem(item);
  assert.equal(item.destinationHref, null);
  ui.openDestination(item);
  assert.deepEqual(assigned, []);
});

test('a refused review marks the item unavailable and never navigates', async () => {
  const {ui, calls, assigned} = surface(null, {status: 409});
  const item = backupItem();
  assert.equal(await ui.openItem(item), false);
  assert.equal(item.unavailable, true);
  assert.equal(item.destinationHref, null);
  assert.equal(calls.length, 1);
  ui.openDestination(item);
  assert.deepEqual(assigned, []);
});
