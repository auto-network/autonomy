// A failed vault wake must be VISIBLE (auto-uhdxm). The 2026-09-06 incident:
// wakeVault's failure reasons were returned and dropped by every caller, so a
// completed sign-in with a dead vault left no trace anywhere. wakeVault now
// reports its own outcome centrally: console.error, a ceremony-error POST,
// and a sessionStorage message the shell renders. Recovery metadata failures
// remain visible; personal warm-up no longer consults an authority ledger.
import assert from 'node:assert/strict';
import test from 'node:test';

import { wakeVault } from '../vault-unlock.js';
import {
  STEP_FAILURES_STORAGE_KEY,
  describeStepFailure,
} from '../step-report.js';

function storedMessages(storage) {
  const raw = storage.getItem(STEP_FAILURES_STORAGE_KEY);
  return raw ? JSON.parse(raw) : {};
}

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
// the recovery metadata read with minimal stubbing.
const READY_INVENTORY = {
  anchors: [{ anchor_id: 'personal-root-default' }],
  classes: [{
    class_id: 'class-1',
    governance: { form: 'root-reachable', anchor_id: 'personal-root-default' },
  }],
};

function recoveryFetch(recoveryStatus, captured) {
  return async (url, options = {}) => {
    const method = options.method || 'GET';
    captured.push([method, url, options.body || null]);
    if (url === '/api/identity/vault-anchors') return reply(200, READY_INVENTORY);
    if (url === '/api/identity/unlock/vault-keys') return reply(recoveryStatus, {});
    if (url === '/api/identity/ceremony-error') return reply(200, { ok: true });
    throw new Error(`unexpected request ${method} ${url}`);
  };
}

test('a recovery failure surfaces in all three channels', async () => {
  const storage = fakeSessionStorage();
  globalThis.sessionStorage = storage;
  const errors = [];
  const origError = console.error;
  console.error = (...args) => errors.push(args.join(' '));
  try {
    const captured = [];
    const result = await wakeVault({
      personalRootSeed: ROOT_SEED,
      fetchImpl: recoveryFetch(409, captured),
    });
    assert.equal(result.ready, false);
    assert.equal(result.reason, 'recovery-409');

    // 1. The shell notice names the failed recovery-metadata read.
    const stored = storedMessages(storage)['vault-wake'];
    assert.ok(stored && stored.includes('recovery metadata'), stored);
    // 2. The console.
    assert.ok(errors.some((line) => line.includes('recovery-409')));
    // 3. The capped client-error log.
    const report = captured.find(([, url]) => url === '/api/identity/ceremony-error');
    assert.ok(report, 'ceremony-error POST must fire');
    const body = JSON.parse(report[2]);
    assert.equal(body.ceremony, 'vault-wake');
    assert.equal(body.action, 'recovery-409');
  } finally {
    console.error = origError;
    delete globalThis.sessionStorage;
  }
});

test('a missing recovery endpoint is an explicit failure', async () => {
  const storage = fakeSessionStorage();
  globalThis.sessionStorage = storage;
  const origError = console.error;
  console.error = () => {};
  try {
    const result = await wakeVault({
      personalRootSeed: ROOT_SEED,
      fetchImpl: recoveryFetch(404, []),
    });
    assert.equal(result.reason, 'recovery-404');
    const stored = storedMessages(storage)['vault-wake'];
    assert.ok(stored && stored.includes('recovery metadata'), stored);
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
    if (url === '/api/identity/unlock/vault-keys') return reply(500, {});
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
      { ready: false, reason: 'recovery-500' },
    );
  } finally {
    console.error = origError;
  }
});

test('describeStepFailure yields an operator sentence for every known gate', () => {
  for (const reason of [
    'anchor-inventory-503', 'personal-root-500', 'personal-root-public-key',
    'anchor-race-409', 'anchor-enroll-400', 'root-class-500',
    'recovery-409', 'recovery-404', 'recovery-503',
    'vault-keys-400', 'something-unmapped',
  ]) {
    const message = describeStepFailure('vault-wake', reason);
    assert.ok(message.startsWith('The vault did not come up:'), message);
    assert.ok(message.includes(`[vault-wake/${reason}]`), message);
  }
});
