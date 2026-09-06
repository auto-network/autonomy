// A failed vault wake must be VISIBLE (auto-uhdxm). The 2026-09-06 incident:
// wakeVault's failure reasons were returned and dropped by every caller, so a
// completed sign-in with a dead vault left no trace anywhere. wakeVault now
// reports its own outcome centrally: console.error, a ceremony-error POST,
// and a sessionStorage message the shell renders — and a 409 heads read
// (ledger present, no genesis) is its own loud reason, never collapsed into
// "not founded".
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  WAKE_FAILED_STORAGE_KEY,
  describeWakeFailure,
  wakeVault,
} from '../vault-unlock.js';

const ROOT_SEED = Uint8Array.from(Buffer.from(
  '9d61b19deffd5a60ba844af492ec2cc4'
  + '4449c5697b326919703bac031cae7f60',
  'hex',
));

function reply(status, value) {
  return {
    status,
    ok: status >= 200 && status < 300,
    async json() { return value; },
  };
}

function fakeSessionStorage() {
  const items = new Map();
  return {
    items,
    getItem(k) { return items.has(k) ? items.get(k) : null; },
    setItem(k, v) { items.set(k, String(v)); },
    removeItem(k) { items.delete(k); },
  };
}

// Anchor inventory answering "ready, nothing to create" so wakeVault reaches
// the ledger-heads gate (the 2026-09-06 bail point) with minimal stubbing.
const READY_INVENTORY = {
  anchors: [{ anchor_id: 'personal-root-default' }],
  classes: [{
    class_id: 'class-1',
    governance: { form: 'root-reachable', anchor_id: 'personal-root-default' },
  }],
};

function headsGateFetch(headsStatus, captured) {
  return async (url, options = {}) => {
    const method = options.method || 'GET';
    captured.push([method, url, options.body || null]);
    if (url === '/api/identity/vault-anchors') return reply(200, READY_INVENTORY);
    if (url.startsWith('/api/network/ledger/heads')) return reply(headsStatus, {});
    if (url === '/api/identity/ceremony-error') return reply(200, { ok: true });
    throw new Error(`unexpected request ${method} ${url}`);
  };
}

test('a heads 409 surfaces as ledger-no-genesis in all three channels', async () => {
  const storage = fakeSessionStorage();
  globalThis.sessionStorage = storage;
  const errors = [];
  const origError = console.error;
  console.error = (...args) => errors.push(args.join(' '));
  try {
    const captured = [];
    const result = await wakeVault({
      personalRootSeed: ROOT_SEED,
      fetchImpl: headsGateFetch(409, captured),
    });
    assert.equal(result.ready, false);
    assert.equal(result.reason, 'ledger-no-genesis');

    // 1. The shell notice: stored message names genesis and warns off re-founding.
    const stored = storage.getItem(WAKE_FAILED_STORAGE_KEY);
    assert.ok(stored && stored.includes('genesis'), stored);
    assert.ok(stored.includes('do NOT re-found'), stored);
    // 2. The console.
    assert.ok(errors.some((line) => line.includes('ledger-no-genesis')));
    // 3. The capped client-error log.
    const report = captured.find(([, url]) => url === '/api/identity/ceremony-error');
    assert.ok(report, 'ceremony-error POST must fire');
    const body = JSON.parse(report[2]);
    assert.equal(body.ceremony, 'vault-wake');
    assert.equal(body.action, 'ledger-no-genesis');
  } finally {
    console.error = origError;
    delete globalThis.sessionStorage;
  }
});

test('a heads 404 stays the quiet never-founded reason, distinct from 409', async () => {
  const storage = fakeSessionStorage();
  globalThis.sessionStorage = storage;
  const origError = console.error;
  console.error = () => {};
  try {
    const result = await wakeVault({
      personalRootSeed: ROOT_SEED,
      fetchImpl: headsGateFetch(404, []),
    });
    assert.equal(result.reason, 'not-founded');
    const stored = storage.getItem(WAKE_FAILED_STORAGE_KEY);
    assert.ok(stored && stored.includes('not founded'), stored);
    assert.ok(!stored.includes('genesis missing'), stored);
  } finally {
    console.error = origError;
    delete globalThis.sessionStorage;
  }
});

test('reporting failures never break the wake result', async () => {
  // No sessionStorage at all, and the ceremony-error POST itself fails:
  // the caller still gets the honest result.
  const fetchImpl = async (url, options = {}) => {
    if (url === '/api/identity/vault-anchors') return reply(200, READY_INVENTORY);
    if (url.startsWith('/api/network/ledger/heads')) return reply(500, {});
    if (url === '/api/identity/ceremony-error') throw new Error('offline');
    throw new Error(`unexpected request ${url}`);
  };
  const origError = console.error;
  console.error = () => {};
  try {
    const result = await wakeVault({
      personalRootSeed: ROOT_SEED, fetchImpl,
    });
    assert.deepEqual(
      { ready: result.ready, reason: result.reason },
      { ready: false, reason: 'heads-500' },
    );
  } finally {
    console.error = origError;
  }
});

test('describeWakeFailure yields an operator sentence for every known gate', () => {
  for (const reason of [
    'anchor-inventory-503', 'personal-root-500', 'personal-root-public-key',
    'anchor-race-409', 'anchor-enroll-400', 'root-class-500',
    'ledger-no-genesis', 'not-founded', 'heads-502', 'delegate-403',
    'vault-keys-400', 'something-unmapped',
  ]) {
    const message = describeWakeFailure(reason);
    assert.ok(message.startsWith('The vault did not come up:'), message);
    assert.ok(message.includes(`[${reason}]`), message);
  }
});
