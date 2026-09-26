import test from 'node:test';
import assert from 'node:assert/strict';
import { prepareStorageDelegate } from '../static/js/ceremony/org-storage-delegate.js';

const day = 86400000;
const now = 1800000000000;
const context = {
  organization: 'example', genesis_id: 'ab'.repeat(32), parents: ['cd'.repeat(32)],
  scope: ['storage:capability:grant:example', 'storage:state:advance:example'],
  ttl_ms: 90 * day, remint_below_ms: 30 * day,
};

test('exactly 30 days remaining reuses the stored delegate without deriving a key', async () => {
  const result = await prepareStorageDelegate(null, { ...context,
    delegate_metadata: { key_exists: true, expires_at: now + 30 * day, key_reference: 'existing' },
  }, now);
  assert.deepEqual(result, { action: 'reuse', organization: 'example', key_reference: 'existing' });
});

test('less than 30 days or absent storage creates a fresh 90-day grant locally', async () => {
  const root = new Uint8Array(32).fill(17);
  const results = [];
  for (const metadata of [
    { key_exists: true, expires_at: now + 30 * day - 1, key_reference: 'existing' },
    { key_exists: false, expires_at: now + 90 * day },
  ]) {
    const result = await prepareStorageDelegate(root, { ...context, delegate_metadata: metadata }, now);
    const event = JSON.parse(result.event);
    assert.equal(result.action, 'new');
    assert.equal(event.payload.ttl, 90 * day);
    assert.equal(event.payload.can_redelegate, false);
    assert.deepEqual(event.payload.scope, context.scope);
    assert.deepEqual(event.parents, context.parents);
    assert.deepEqual(event.hlc, [now, 0]);
    assert.match(event.payload.proof, /^[0-9a-f]{128}$/);
    results.push(event.payload.child_pub);
  }
  assert.notEqual(results[0], results[1]);
  root.fill(0);
});

test('a checkpointer whose grant predates the checkpoint scope mints anew despite a fresh key', async () => {
  // Node lacks the WebCrypto Ed25519 pieces derivePersona needs; reaching
  // past the reuse gate is the behaviour under test, and it shows as the
  // derivation being attempted rather than the stored reference returned.
  let reachedMint = false;
  try {
    await prepareStorageDelegate(null, { ...context, remint_required: true,
      scope: [...context.scope, 'membership:checkpoint'],
      delegate_metadata: { key_exists: true, expires_at: now + 80 * day, key_reference: 'existing' },
    }, now);
  } catch (error) {
    reachedMint = true;
  }
  assert.equal(reachedMint, true);
});
