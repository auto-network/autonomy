const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const SOURCE = path.join(ROOT, 'tools/dashboard/static/js/lib/system-auth.js');

function harness() {
  const events = [];
  class FakeCustomEvent {
    constructor(type, init) { this.type = type; this.detail = init.detail; }
  }
  const window = {
    Autonomy: {},
    dispatchEvent(event) { events.push(event); },
  };
  const sandbox = { window, CustomEvent: FakeCustomEvent, Promise };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(SOURCE, 'utf8'), sandbox, { filename: 'system-auth.js' });
  return { api: window.Autonomy.systemAuth, events };
}

describe('system authentication bracket', () => {
  it('remains active until nested ceremonies both settle', async () => {
    const h = harness();
    const endOuter = h.api.begin();
    const endInner = h.api.begin();
    assert.equal(h.api.active, true);
    assert.equal(h.api.depth, 2);
    endInner();
    assert.equal(h.api.active, true);
    endOuter();
    assert.equal(h.api.active, false);
    assert.deepEqual(h.events.map((event) => event.detail.depth), [1, 2, 1, 0]);
  });

  it('releases the bracket after success, cancellation, and thrown errors', async () => {
    const h = harness();
    assert.equal(await h.api.run(async () => 'ok'), 'ok');
    await assert.rejects(h.api.run(async () => { throw new Error('cancelled'); }), /cancelled/);
    assert.equal(h.api.active, false);
    assert.equal(h.api.depth, 0);
    assert.equal(h.events.at(-1).detail.active, false);
  });
});
