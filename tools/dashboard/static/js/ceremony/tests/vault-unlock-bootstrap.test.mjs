import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ensurePersonalRootVault,
} from '../vault-unlock.js';
import { openRootAnchorEnvelope } from '../root-anchor.js';

// RFC 8032 test vector 1: a known Ed25519 seed and its public key.
const ROOT_SEED = Uint8Array.from(Buffer.from(
  '9d61b19deffd5a60ba844af492ec2cc4'
  + '4449c5697b326919703bac031cae7f60',
  'hex',
));
const ROOT_PUB = 'd75a980182b10ab7d54bfed3c964073a'
  + '0ee172f3daa62325af021a68f707511a';

function reply(status, value) {
  return {
    status,
    ok: status >= 200 && status < 300,
    async json() { return value; },
  };
}

test('first root-opening login creates the stable anchor and class automatically', async () => {
  const calls = [];
  let enrolledAnchor = null;
  const fetchImpl = async (url, options = {}) => {
    const method = options.method || 'GET';
    calls.push([method, url]);
    if (method === 'GET' && url === '/api/identity/vault-anchors') {
      return reply(200, { anchors: [], classes: [] });
    }
    if (method === 'GET' && url === '/api/identity/personal') {
      return reply(200, { root_pub: ROOT_PUB });
    }
    if (method === 'POST' && url === '/api/identity/vault-anchors') {
      enrolledAnchor = JSON.parse(options.body).anchor;
      return reply(201, { anchor: enrolledAnchor });
    }
    if (method === 'POST' && url.endsWith('/classes')) {
      return reply(201, { policy_class: { class_id: 'class-1' } });
    }
    throw new Error(`unexpected request ${method} ${url}`);
  };

  const result = await ensurePersonalRootVault({
    personalRootSeed: ROOT_SEED,
    fetchImpl,
    now: Date.parse('2026-08-24T08:00:00Z'),
  });

  assert.deepEqual(result, { ready: true, created: true, reason: null });
  assert.equal(enrolledAnchor.anchor_id, 'personal-root-default');
  assert.equal(enrolledAnchor.root_pub, ROOT_PUB);
  const openedAnchorSeed = await openRootAnchorEnvelope(enrolledAnchor, ROOT_SEED);
  assert.equal(openedAnchorSeed.length, 32);
  openedAnchorSeed.fill(0);
  assert.deepEqual(calls, [
    ['GET', '/api/identity/vault-anchors'],
    ['GET', '/api/identity/personal'],
    ['POST', '/api/identity/vault-anchors'],
    ['POST', '/api/identity/vault-anchors/personal-root-default/classes'],
  ]);
});

test('later logins reuse the existing root-reachable class without writes', async () => {
  const calls = [];
  const fetchImpl = async (url, options = {}) => {
    calls.push([options.method || 'GET', url]);
    return reply(200, {
      anchors: [{ anchor_id: 'personal-root-default' }],
      classes: [{
        class_id: 'class-1',
        governance: {
          form: 'root-reachable',
          anchor_id: 'personal-root-default',
        },
      }],
    });
  };

  const result = await ensurePersonalRootVault({
    personalRootSeed: ROOT_SEED,
    fetchImpl,
  });

  assert.deepEqual(result, { ready: true, created: false, reason: null });
  assert.deepEqual(calls, [['GET', '/api/identity/vault-anchors']]);
});

test('a partial bootstrap finishes the class without reopening or replacing the anchor', async () => {
  const calls = [];
  const anchor = { anchor_id: 'personal-root-default' };
  const fetchImpl = async (url, options = {}) => {
    const method = options.method || 'GET';
    calls.push([method, url]);
    if (method === 'GET') return reply(200, { anchors: [anchor], classes: [] });
    if (method === 'POST' && url.endsWith('/classes')) {
      return reply(201, { policy_class: { class_id: 'class-1' } });
    }
    throw new Error(`unexpected request ${method} ${url}`);
  };

  const result = await ensurePersonalRootVault({
    personalRootSeed: ROOT_SEED,
    fetchImpl,
  });
  assert.deepEqual(result, { ready: true, created: true, reason: null });
  assert.deepEqual(calls, [
    ['GET', '/api/identity/vault-anchors'],
    ['POST', '/api/identity/vault-anchors/personal-root-default/classes'],
  ]);
});
