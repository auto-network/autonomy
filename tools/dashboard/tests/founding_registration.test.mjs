// Founding registers the organization inside its own opening (auto-2vseu):
// the envelope is signed by the in-memory organization root with the uuid the
// genesis bound, then the organization-scoped sign-on phases run with the
// still-held personal seed. A refusal is reported, never thrown, so the
// founded organization keeps its success screen with the outcome on it.
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  finishFoundedOrganization, registerFoundedOrganization, signRootRequest,
} from '../static/js/ceremony/founding.js';
import { canonicalJson, domainBytes, hexToBytes } from '../static/js/ceremony/primitives.js';

const subtle = globalThis.crypto.subtle;

async function orgRoot() {
  const pair = await subtle.generateKey({ name: 'Ed25519' }, true, ['sign', 'verify']);
  const raw = new Uint8Array(await subtle.exportKey('raw', pair.publicKey));
  return { key: pair.privateKey, verify: pair.publicKey,
    pubHex: Array.from(raw, b => b.toString(16).padStart(2, '0')).join('') };
}

function transportRecording(responder) {
  const calls = [];
  return { calls, fetch: async (url, options = {}) => {
    calls.push({ url, options, body: options.body ? JSON.parse(options.body) : null });
    return responder(url, options);
  } };
}

test('the registration envelope is signed by the organization root over the request domain', async () => {
  const root = await orgRoot();
  const payload = { org_uuid: 'org-1', root_pub: root.pubHex, recovery_policy: 'none' };
  const envelope = await signRootRequest(root.key, root.pubHex, 'POST', '/v1/orgs', payload, 1700000000);
  assert.equal(envelope.v, 1);
  assert.equal(envelope.signer, root.pubHex);
  assert.equal(envelope.ts, 1700000000);
  assert.deepEqual(envelope.payload, payload);
  const input = domainBytes('autonomy.network.registry.request.v1\n', canonicalJson({
    v: 1, method: 'POST', path: '/v1/orgs', ts: 1700000000, signer: root.pubHex, payload,
  }));
  assert.equal(await subtle.verify({ name: 'Ed25519' }, root.verify, hexToBytes(envelope.sig), input), true);
});

test('registration posts {org, envelope} to the dashboard with the genesis uuid and returns the binding', async () => {
  const root = await orgRoot();
  const transport = transportRecording(async () => new Response(JSON.stringify({
    ok: true, binding: { org_uuid: 'org-1', root_pub: root.pubHex, registry_url: 'https://registry.test' },
  }), { status: 200 }));
  const { binding } = await registerFoundedOrganization({
    org: 'acme', orgId: 'org-1', rootSigningKey: root.key, rootPub: root.pubHex, transport, nowS: 1700000000,
  });
  assert.equal(binding.org_uuid, 'org-1');
  assert.equal(transport.calls.length, 1);
  const call = transport.calls[0];
  assert.equal(call.url, '/api/network/register');
  assert.equal(call.options.headers['X-Graph-Org'], 'acme');
  assert.equal(call.body.org, 'acme');
  assert.equal(call.body.envelope.payload.org_uuid, 'org-1');
  assert.equal(call.body.envelope.signer, root.pubHex);
});

test('a registry refusal surfaces the server message and a foreign uuid is refused', async () => {
  const root = await orgRoot();
  const refusing = transportRecording(async () => new Response(JSON.stringify({ ok: false, error: 'registry down' }), { status: 502 }));
  await assert.rejects(registerFoundedOrganization({
    org: 'acme', orgId: 'org-1', rootSigningKey: root.key, rootPub: root.pubHex, transport: refusing,
  }), /registry down/);
  const foreign = transportRecording(async () => new Response(JSON.stringify({ ok: true, binding: { org_uuid: 'other' } }), { status: 200 }));
  await assert.rejects(registerFoundedOrganization({
    org: 'acme', orgId: 'org-1', rootSigningKey: root.key, rootPub: root.pubHex, transport: foreign,
  }), /ledger genesis binds org-1/);
  await assert.rejects(registerFoundedOrganization({
    org: 'acme', orgId: 'org-1', rootSigningKey: null, rootPub: root.pubHex, transport: foreign,
  }), /no longer in memory/);
});

test('finishing runs the organization-scoped phases with the held seed and reports only that organization', async () => {
  const seen = [];
  const phases = {
    fetchPreparation: async (fetchImpl, { org }) => { seen.push(['fetch', org]); return { enc: org }; },
    prepareSignon: async (seed, encrypted, session) => { seen.push(['prepare', seed === seedRef, encrypted.enc, session]); return { posts: [] }; },
    submitSignon: async () => ({ ready: ['acme'], failed: [{ org: 'personal', step: 'serve-cert', error: 'x' }], bindings: [] }),
  };
  const transport = transportRecording(async () => new Response('{}'));
  const seed = new Uint8Array(32).fill(7);
  const seedRef = seed;
  const setup = await finishFoundedOrganization({ org: 'acme', personalRootSeed: seed, transport, session: 'S', phases });
  assert.equal(setup.ready, true);
  assert.deepEqual(setup.failed, []);
  // The seed itself reaches the phases (no unzeroed copy); the caller zeroes it.
  assert.deepEqual(seen, [['fetch', 'acme'], ['prepare', true, 'acme', 'S']]);
  // A just-registered organization's first serve certificate is minted now,
  // so the real submitSignon lists it under repaired, never ready.
  const repaired = { ...phases, submitSignon: async () => ({ ready: [], repaired: ['acme'], failed: [] }) };
  const minted = await finishFoundedOrganization({ org: 'acme', personalRootSeed: seed, transport, session: 'S', phases: repaired });
  assert.equal(minted.ready, true);
  const untouched = { ...phases, submitSignon: async () => ({ ready: [], repaired: [], failed: [] }) };
  const nothing = await finishFoundedOrganization({ org: 'acme', personalRootSeed: seed, transport, session: 'S', phases: untouched });
  assert.equal(nothing.ready, false);
  const failing = { ...phases, submitSignon: async () => ({ ready: [], failed: [{ org: 'acme', step: 'organization-delegate', error: 'nope' }] }) };
  const bad = await finishFoundedOrganization({ org: 'acme', personalRootSeed: seed, transport, session: 'S', phases: failing });
  assert.equal(bad.ready, false);
  assert.deepEqual(bad.failed, [{ org: 'acme', step: 'organization-delegate', error: 'nope' }]);
});
