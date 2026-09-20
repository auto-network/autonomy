/* prepareSignon must mint a SYNC-ONLY fleet credential when the personal
 * organization is not registered with the registry.
 *
 * Onboarding does not require registering, so an enrolled fleet machine
 * whose personal org has no registry binding is a normal state. The runtime
 * preparation carries `org_uuid` (the registered binding, null when
 * unregistered) and `personal_org_uuid` (the org's own id, always present).
 * Since fa760a61 the fleet body is minted with
 * `orgUuid = rc.org_uuid || rc.personal_org_uuid`, so an unregistered
 * machine attaches `machine_private_seed` + `reachability_cert` under an
 * org the registry has never bound. The server refuses that credential
 * ("reachability credential delivered without a registered org_uuid" →
 * 400) and fleet arming fails instead of using the sync-only
 * credential the pre-commit flow minted with `rc.org_uuid || null`.
 *
 *   node --test tools/dashboard/tests/signon_phases_fleet_credential.test.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';

import { prepareSignon } from '../static/js/ceremony/signon-phases.js';
import { deriveAuditedRecipient } from '../static/js/ceremony/vault-unlock.js';
import { sealToEncapsulationKey } from '../static/js/ceremony/sealing.js';
import { bytesToHex } from '../static/js/ceremony/primitives.js';
import { FLEET_MACHINE_KEY_SALT, mintFleetEnrollmentEvidence } from '../static/js/ceremony/fleet-enrollment.js';
import { fleetRuntimePost } from '../static/js/ceremony/fleet-enrollment.js';

const PURPOSE = 'autonomy/identity/sign-in-preparation/v1';
const PKCS8_ED25519_PREFIX = Uint8Array.from([
  0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x04, 0x22, 0x04, 0x20,
]);
const enc = new TextEncoder();

async function ed25519PublicHex(seed) {
  const pkcs8 = new Uint8Array(PKCS8_ED25519_PREFIX.length + 32);
  pkcs8.set(PKCS8_ED25519_PREFIX, 0); pkcs8.set(seed, PKCS8_ED25519_PREFIX.length);
  const key = await webcrypto.subtle.importKey('pkcs8', pkcs8, { name: 'Ed25519' }, true, ['sign']);
  const jwk = await webcrypto.subtle.exportKey('jwk', key);
  return bytesToHex(Buffer.from(jwk.x, 'base64url'));
}

async function deriveMachineSeed(seed, machineId) {
  const key = await webcrypto.subtle.importKey('raw', seed, 'HKDF', false, ['deriveBits']);
  return new Uint8Array(await webcrypto.subtle.deriveBits({
    name: 'HKDF', hash: 'SHA-256', salt: enc.encode(FLEET_MACHINE_KEY_SALT), info: enc.encode(machineId),
  }, key, 256));
}

async function fixture({ orgUuid, firstCompletion = false }) {
  const rootSeed = webcrypto.getRandomValues(new Uint8Array(32));
  const rootPub = await ed25519PublicHex(rootSeed);
  const machineId = 'ab'.repeat(32);
  const machinePub = await ed25519PublicHex(await deriveMachineSeed(rootSeed, machineId));
  const inputs = {
    vault: { root_pub: rootPub, inventory: { anchors: [], classes: [{ governance: { form: 'root-reachable' } }] } },
    organizations: [],
    completion: null,
    personal_serve: {},
    runtime: {
      enabled: true,
      org_uuid: orgUuid,                                   // registered binding, or null
      personal_org_uuid: '7d8c2e1a-1111-4111-8111-111111111111',
      personal_root_pub: rootPub, machine_id: machineId, machine_pub: machinePub,
      serving_orgs: [],
    },
  };
  if (firstCompletion) {
    const request = {machine_id: machineId, personal_root_pub: rootPub, invite_id: 'cd'.repeat(32)};
    const channelBinding = 'ef'.repeat(32);
    const delivery = await mintFleetEnrollmentEvidence({
      personalRootSeed: new Uint8Array(rootSeed), rootPub, request, channelBinding,
    });
    inputs.completion = {
      request_id: '01'.repeat(32), request, channel_binding: channelBinding,
      approval: delivery.approval, roster_entry: delivery.rosterEntry,
      personal_org_uuid: inputs.runtime.personal_org_uuid,
    };
    inputs.runtime = {enabled: false};
  }
  const audited = await deriveAuditedRecipient(rootSeed);
  const sealed = await sealToEncapsulationKey(enc.encode(JSON.stringify(inputs)), audited.publicKeyHex, PURPOSE);
  const encrypted = { sealed: typeof sealed === 'string' ? sealed : bytesToHex(sealed) };
  const signon = { _internals: { prepareRootMaintenance: async (_seed, _orgs, runtime) => {
    if (!firstCompletion) return [];
    assert.equal(runtime.personal_root_pub, rootPub);
    assert.equal(runtime.machine_pub, machinePub);
    assert.equal(runtime.serves, true);
    return [
      {step: 'binding', url: '/api/network/register'},
      {step: 'serve-cert', url: '/api/network/serve-cert'},
    ];
  } } };
  return { rootSeed, encrypted, signon, runtime: inputs.runtime };
}

function fleetPost(prepared) {
  const post = prepared.posts.find((p) => p.step === 'fleet');
  assert.ok(post, 'a fleet post was prepared');
  return post;
}

test('first completion prepares connection setup before full runtime activation', async () => {
  const {rootSeed, encrypted, signon} = await fixture({orgUuid: null, firstCompletion: true});
  const prepared = await prepareSignon(rootSeed, encrypted, signon);
  assert.deepEqual(prepared.posts.map(p => p.url), [
    '/api/fleet/enrollment/local-completion', '/api/network/register',
    '/api/network/serve-cert', '/api/fleet/runtime',
  ]);
  const body = prepared.posts.at(-1).body;
  assert.equal(body.reachability_cert.org, '7d8c2e1a-1111-4111-8111-111111111111');
  assert.match(body.machine_private_seed, /^[0-9a-f]{64}$/);
});

test('unregistered personal org → sync-only credential: no reachability material under an unbound org', async () => {
  const { rootSeed, encrypted, signon } = await fixture({ orgUuid: null });
  const prepared = await prepareSignon(rootSeed, encrypted, signon);
  const body = fleetPost(prepared).body;
  assert.equal(body.reachability_cert, undefined,
    'no reachability cert may be minted under personal_org_uuid — the registry has not bound it');
  assert.equal(body.machine_private_seed, undefined);
});

test('registered personal org → reachability material is delivered (unchanged)', async () => {
  const { rootSeed, encrypted, signon } = await fixture({ orgUuid: '3f2a9c10-2222-4222-8222-222222222222' });
  const prepared = await prepareSignon(rootSeed, encrypted, signon);
  const body = fleetPost(prepared).body;
  assert.ok(body.reachability_cert, 'reachability cert delivered for a registered org');
  assert.ok(body.machine_private_seed);
});

test('the single mint carries every runtime field: serving seeds AND org sync certificates', async () => {
  const { rootSeed, runtime } = await fixture({ orgUuid: null });
  runtime.serving_orgs = [{ scope: 'fresh-org', org_uuid: 'fresh-registry-id', genesis_id: 'cd'.repeat(32) }];
  runtime.sync_orgs = [{ scope: 'fresh-org', org_uuid: 'fresh-registry-id', genesis_id: 'cd'.repeat(32), persona_pub: 'ab'.repeat(32) }];
  const post = await fleetRuntimePost(rootSeed, runtime);
  assert.equal(post.url, '/api/fleet/runtime');
  assert.match(post.body.serving_machine_private_seeds['fresh-registry-id'], /^[a-f0-9]{64}$/);
  assert.ok(post.body.org_sync_certs && post.body.org_sync_certs['fresh-org'],
    'the org sync certificate is minted by the one mint; on 2026-09-13 it was added to a copy and missed');
  assert.ok(rootSeed.some(byte => byte !== 0), 'the caller keeps its own seed');
});
