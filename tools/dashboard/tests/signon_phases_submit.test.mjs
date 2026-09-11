/* submitSignon must not turn a post-cookie handoff failure into a failed
 * sign-in.
 *
 * By the time submitSignon runs, the credential POST has already minted the
 * session cookie: the operator IS signed in. What follows — the vault-keys
 * handoff and the fleet runtime post — are maintenance that can fail for
 * reasons unrelated to the operator's credential (a stale delegate, a
 * connector that is down, a roster mismatch). Today submitSignon rethrows
 * both, every ceremony awaits it bare, the failure handler renders an error,
 * the redirect never runs, and `finally` nulls the prepared keys so nothing
 * can be retried. Before fa760a61 the vault wake was wrapped and logged.
 *
 * Expected: submitSignon RESOLVES with the report naming the failed step;
 * only the caller decides what a failed maintenance step means.
 *
 *   node --test tools/dashboard/tests/signon_phases_submit.test.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { submitSignon } from '../static/js/ceremony/signon-phases.js';
import { prepareVault, deriveAuditedRecipient } from '../static/js/ceremony/vault-unlock.js';
import { deriveKemSeed } from '../static/js/ceremony/founding.js';
import { deriveEncapsulationKeypair } from '../static/js/ceremony/primitives.js';

function reply(status, body) {
  return { ok: status < 400, status, json: async () => body };
}

function fetchWhere(rules) {
  const calls = [];
  const impl = async (url, options = {}) => {
    calls.push({ url: String(url), method: options.method || 'GET' });
    for (const [prefix, answer] of rules) {
      if (String(url).startsWith(prefix)) return typeof answer === 'function' ? answer() : answer;
    }
    return reply(200, { ok: true });
  };
  impl.calls = calls;
  return impl;
}

function prepared(posts) {
  return {
    ready: ['netorg'],
    fleetEnabled: posts.some((p) => p.step === 'fleet'),
    vault: { keys: { generation_keys: {}, organization_delegates: [] } },
    posts,
  };
}

test('organization derivation matches counter-zero credential and isolates mismatches with no network', async () => {
  const root = new Uint8Array(32).fill(37);
  const genesis = 'ab'.repeat(32);
  const kemSeed = await deriveKemSeed(root, 0);
  const pair = await deriveEncapsulationKeypair(kemSeed, 'autonomy/persona-kem/v1/' + genesis);
  kemSeed.fill(0);
  const good = { slug: 'good', genesis_id: genesis, encryption_recovery: {
    genesis_id: genesis, counter: 0, credentials: [{ kem_key_id: 'cd'.repeat(32), kem_public_key: pair.publicKeyHex }],
  } };
  const badKey = { ...good, slug: 'bad-key', encryption_recovery: {
    ...good.encryption_recovery, credentials: [{ kem_key_id: 'ef'.repeat(32), kem_public_key: '00'.repeat(32) }],
  } };
  const wrongCounter = { ...good, slug: 'wrong-counter', encryption_recovery: { ...good.encryption_recovery, counter: 1 } };
  const wrongGenesis = { ...good, slug: 'wrong-genesis', genesis_id: 'ff'.repeat(32) };
  const unavailable = { ...good, slug: 'unavailable', encryption_recovery: { error: 'organization-encryption-unavailable' } };
  const originalFetch = globalThis.fetch;
  globalThis.fetch = () => { throw new Error('network while root is open'); };
  try {
    const result = await prepareVault(root, { inventory: {
      classes: [{ governance: { form: 'root-reachable' } }], anchors: [],
    } }, await deriveAuditedRecipient(root), [badKey, wrongCounter, wrongGenesis, unavailable, good]);
    assert.equal(result.keys.organization_kem_keys.length, 1);
    const item = result.keys.organization_kem_keys[0];
    assert.equal(item.organization, 'good');
    assert.equal(item.genesis_id, genesis);
    assert.equal(item.kem_key_id, good.encryption_recovery.credentials[0].kem_key_id);
    assert.ok(item.persona_kem_private_key === pair.privateKeyHex, 'independent derivation matches');
    assert.deepEqual(result.failures.map(f => f.org), ['bad-key', 'wrong-counter', 'wrong-genesis', 'unavailable']);
    assert.ok(result.failures.every(f => f.step === 'organization-recovery'));
  } finally { root.fill(0); globalThis.fetch = originalFetch; }
});

test('organization delegate refusal stays visible without marking the personal vault or healthy org failed', async () => {
  const fetchImpl = fetchWhere([
    ['/api/identity/unlock/vault-keys', reply(200, { ok: true, organization_delegates: {
      netorg: { ok: false, error: 'organization delegation must cite current heads' },
      healthy: { ok: true },
    } })],
  ]);
  const handoff = prepared([{ step: 'serve-cert', org: 'healthy', url: '/api/network/serve-cert', body: {} }]);
  handoff.ready = ['netorg', 'healthy'];
  const report = await submitSignon(handoff, fetchImpl);
  assert.deepEqual(report.failed, [{ org: 'netorg', step: 'organization-delegate',
    error: 'organization delegation must cite current heads' }]);
  assert.deepEqual(report.ready, ['healthy']);
  assert.deepEqual(report.repaired, ['healthy']);
  assert.equal(handoff.vault.keys, null);
});

test('organization recovery counts and isolated refusal survive submission without private material', async () => {
  const outcomes = { netorg: { ok: false, error: 'organization-encryption-refused' },
    healthy: { ok: true, recovered: 2 } };
  const fetchImpl = fetchWhere([
    ['/api/identity/unlock/vault-keys', reply(200, { ok: true, organization_recovery: outcomes })],
  ]);
  const handoff = prepared([]);
  handoff.ready = ['netorg', 'healthy'];
  handoff.vault.keys.organization_kem_keys = [{ persona_kem_private_key: 'test-private-material' }];
  const report = await submitSignon(handoff, fetchImpl);
  assert.deepEqual(report.organization_recovery, outcomes);
  assert.deepEqual(report.failed, [{ org: 'netorg', step: 'organization-recovery', error: 'organization-encryption-refused' }]);
  assert.deepEqual(report.ready, ['healthy']);
  assert.equal(handoff.vault.keys, null);
  assert.equal(JSON.stringify(report).includes('test-private-material'), false);
});

test('a vault-keys 500 after the cookie is minted resolves with the step reported, and the other posts still run', async () => {
  const fetchImpl = fetchWhere([
    ['/api/identity/unlock/vault-keys', reply(500, { ok: false, error: 'the vault could not be brought up: organization delegate must cite current heads' })],
  ]);
  const posts = [{ step: 'serve-cert', org: 'netorg', url: '/api/network/serve-cert', body: {} }];

  const handoff = prepared(posts);
  const report = await submitSignon(handoff, fetchImpl);

  assert.ok(report, 'submitSignon resolved instead of throwing');
  assert.ok(report.failed.some((f) => f.step === 'vault-wake' || f.step === 'vault'),
    'the vault handoff failure is in the report: ' + JSON.stringify(report.failed));
  assert.ok(fetchImpl.calls.some((c) => c.url === '/api/network/serve-cert' && c.method === 'POST'),
    'per-org maintenance still ran after the vault handoff failed');
  assert.ok(fetchImpl.calls.some((c) => c.url === '/api/network/unlock-report'),
    'the diagnostics report was still posted');
  assert.deepEqual(report.ready, [], 'failed vault handoff is not ready');
  assert.equal(handoff.vault.keys, null, 'key material is still released');
});

test('a fleet runtime 400 resolves with fleet_arming=mint-failed instead of failing the sign-in', async () => {
  const fetchImpl = fetchWhere([
    ['/api/fleet/runtime', reply(400, { ok: false, error: 'reachability credential delivered without a registered org_uuid' })],
  ]);
  const posts = [
    { step: 'serve-cert', org: 'netorg', url: '/api/network/serve-cert', body: {} },
    { step: 'fleet', url: '/api/fleet/runtime', body: {} },
    { step: 'binding', org: 'netorg', url: '/api/network/renew', body: {} },
  ];

  const report = await submitSignon(prepared(posts), fetchImpl);

  assert.ok(report, 'submitSignon resolved instead of throwing');
  assert.equal(report.fleet_arming.attempted, true);
  assert.equal(report.fleet_arming.outcome, 'mint-failed');
  assert.match(report.fleet_arming.error, /registered org_uuid/);
  assert.deepEqual(report.repaired, ['netorg'], 'the serve-cert post before it still counted');
  assert.deepEqual(report.bindings, [{ org: 'netorg', action: 'renewed' }],
    'independent maintenance after the fleet failure still ran');
});

test('a clean handoff is unchanged: resolves with no failures', async () => {
  const fetchImpl = fetchWhere([]);
  const posts = [{ step: 'serve-cert', org: 'netorg', url: '/api/network/serve-cert', body: {} }];

  const report = await submitSignon(prepared(posts), fetchImpl);

  assert.deepEqual(report.failed, []);
  assert.deepEqual(report.repaired, ['netorg']);
});

test('the vault failure remains visible after navigation through the existing session storage report', async () => {
  const previous = globalThis.sessionStorage;
  const values = new Map();
  globalThis.sessionStorage = {
    getItem: key => values.get(key) || null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  };
  try {
    const fetchImpl = fetchWhere([
      ['/api/identity/unlock/vault-keys', reply(500, { error: 'vault handoff refused' })],
    ]);
    await submitSignon(prepared([]), fetchImpl);
    const failures = JSON.parse(values.get('autonomy.unlock.step-failures'));
    assert.match(failures['vault-wake'], /vault handoff refused/);
    await submitSignon(prepared([]), fetchWhere([]));
    assert.equal(values.has('autonomy.unlock.step-failures'), false,
      'only a successful later handoff clears the failure');
  } finally {
    if (previous === undefined) delete globalThis.sessionStorage;
    else globalThis.sessionStorage = previous;
  }
});
