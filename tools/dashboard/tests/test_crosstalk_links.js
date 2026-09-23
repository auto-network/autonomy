const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname,
  '../templates/partials/session-entries.html'), 'utf8');
const tile = template.split('<!-- ── Semantic Bash:')[0]
  .split('<template x-if="resolveEntry(dEntry).type === \'crosstalk\'">')[1];
const links = [...tile.matchAll(/:href="([^"]+)"/g)].map(match => match[1]);

function destinations(entry) {
  return links.map(expression => vm.runInNewContext(expression, {
    dEntry: entry, resolveEntry: value => value, encodeURIComponent,
  }));
}

test('CrossTalk sender and provenance open session viewer without a live href', () => {
  assert.deepEqual(destinations({sender: 'host-old', source_id: 'source-uuid', turn: '7'}),
    ['/session/host-old', '/session/host-old']);
});

test('CrossTalk retains resolved session links and escapes fallback names', () => {
  assert.deepEqual(destinations({sender: 'host/a b', href: '/session/core/host-live'}),
    ['/session/core/host-live', '/session/host%2Fa%20b']);
});
