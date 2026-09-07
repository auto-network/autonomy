// The shared unlock-step surfacing mechanism (ceremony/step-report.js).
//
// Its whole reason to exist is that a silently-failed step is invisible: the
// vault that never woke (auto-uhdxm) and the seed-mint that never ran
// (auto-ujrh7) both presented as a completed sign-in. These tests pin the
// properties the root-step runner depends on: several steps and several orgs
// coexist, each clears on its OWN success, a skipped step changes nothing,
// and reporting can never throw into the ceremony.
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  STEP_FAILURES_STORAGE_KEY,
  classifyStepResult,
  describeStepFailure,
  reportStepOutcome,
  stepEntryKey,
} from '../step-report.js';

function fakeSessionStorage() {
  const items = new Map();
  return {
    getItem(k) { return items.has(k) ? items.get(k) : null; },
    setItem(k, v) { items.set(k, String(v)); },
    removeItem(k) { items.delete(k); },
    has(k) { return items.has(k); },
  };
}

function withStubbedShell(run) {
  const storage = fakeSessionStorage();
  const posts = [];
  const origError = console.error;
  globalThis.sessionStorage = storage;
  console.error = () => {};
  const fetchImpl = async (url, options = {}) => {
    posts.push([url, JSON.parse(options.body)]);
    return { ok: true, status: 200, async json() { return {}; } };
  };
  try {
    return run({ storage, posts, fetchImpl });
  } finally {
    console.error = origError;
    delete globalThis.sessionStorage;
  }
}

function stored(storage) {
  const raw = storage.getItem(STEP_FAILURES_STORAGE_KEY);
  return raw ? JSON.parse(raw) : {};
}

test('classifyStepResult accepts every shape the callers already produce', () => {
  assert.equal(classifyStepResult({ ready: true }), 'succeeded');
  assert.equal(classifyStepResult({ ready: false, reason: 'heads-500' }), 'failed');
  assert.equal(classifyStepResult({ ok: true }), 'succeeded');
  assert.equal(classifyStepResult({ ok: false }), 'failed');
  assert.equal(classifyStepResult({ status: 'ran' }), 'succeeded');
  assert.equal(classifyStepResult({ status: 'failed' }), 'failed');
  assert.equal(classifyStepResult({ status: 'skipped' }), 'skipped');
  assert.equal(classifyStepResult(null), 'failed');
});

test('per-org failures coexist and each clears on its own success', () => {
  withStubbedShell(({ storage, fetchImpl }) => {
    reportStepOutcome('checkpoint', { status: 'failed', reason: 'heads-409' },
      { fetchImpl, org: 'autonomy' });
    reportStepOutcome('checkpoint', { status: 'failed', reason: 'heads-500' },
      { fetchImpl, org: 'anchore' });
    reportStepOutcome('vault-wake', { ready: false, reason: 'not-founded' },
      { fetchImpl });

    assert.deepEqual(Object.keys(stored(storage)).sort(),
      ['checkpoint@anchore', 'checkpoint@autonomy', 'vault-wake']);

    // One org's success must not clear the other org's failure.
    reportStepOutcome('checkpoint', { status: 'ran' }, { fetchImpl, org: 'autonomy' });
    assert.deepEqual(Object.keys(stored(storage)).sort(),
      ['checkpoint@anchore', 'vault-wake']);

    reportStepOutcome('checkpoint', { status: 'ran' }, { fetchImpl, org: 'anchore' });
    reportStepOutcome('vault-wake', { ready: true }, { fetchImpl });
    assert.deepEqual(stored(storage), {});
    // Nothing left behind: the key itself is gone, not an empty object.
    assert.equal(storage.getItem(STEP_FAILURES_STORAGE_KEY), null);
  });
});

test('a skipped step neither records nor clears', () => {
  withStubbedShell(({ storage, posts, fetchImpl }) => {
    reportStepOutcome('binding', { status: 'failed', reason: 'heads-502' },
      { fetchImpl, org: 'autonomy' });
    const before = stored(storage);
    const postCount = posts.length;

    assert.equal(
      reportStepOutcome('binding', { status: 'skipped' }, { fetchImpl, org: 'autonomy' }),
      'skipped',
    );
    assert.deepEqual(stored(storage), before, 'a skipped run must not clear the failure');
    assert.equal(posts.length, postCount, 'a skipped run must not report');
  });
});

test('a failure reports to the capped client-error log with its step and org', () => {
  withStubbedShell(({ posts, fetchImpl }) => {
    reportStepOutcome('checkpoint', { status: 'failed', reason: 'seq0-403' },
      { fetchImpl, org: 'autonomy' });
    const [url, body] = posts.at(-1);
    assert.equal(url, '/api/identity/ceremony-error');
    assert.equal(body.ceremony, 'checkpoint');
    assert.equal(body.action, 'seq0-403');
    assert.deepEqual(body.context, { org: 'autonomy' });
    assert.ok(body.message.includes('[checkpoint/seq0-403]'), body.message);
  });
});

test('every step gets a sentence naming what failed, the cost, and the slug', () => {
  for (const step of ['vault-wake', 'checkpoint', 'serve-cert', 'binding', 'rekey',
    'a-step-nobody-has-written-yet']) {
    const message = describeStepFailure(step, 'heads-500', 'autonomy');
    assert.ok(/^[A-Z]/.test(message), message);
    assert.ok(message.includes('(autonomy)'), message);
    assert.ok(message.includes('(HTTP 500)'), message);
    assert.ok(message.includes(`[${step}/heads-500]`), message);
    assert.ok(message.length > 40, message);
  }
});

test('reporting never throws into the ceremony', () => {
  const origError = console.error;
  console.error = () => {};
  try {
    // No sessionStorage, and a transport that rejects.
    const exploding = async () => { throw new Error('offline'); };
    assert.equal(
      reportStepOutcome('checkpoint', { status: 'failed', reason: 'x' },
        { fetchImpl: exploding, org: 'autonomy' }),
      'failed',
    );
  } finally {
    console.error = origError;
  }
});

test('stepEntryKey distinguishes per-org steps from global ones', () => {
  assert.equal(stepEntryKey('vault-wake'), 'vault-wake');
  assert.equal(stepEntryKey('checkpoint', 'autonomy'), 'checkpoint@autonomy');
});
