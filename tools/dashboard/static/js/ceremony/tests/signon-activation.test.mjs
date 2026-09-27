// The runtime a sign-on activates (signon-phases.runtimeActivation).
//
// The mint delivers the machine key and the reachability cert only under a
// registered personal org uuid; a fresh identity's first sign-on registers
// that uuid in the same submission, so it must activate under it, or the
// serving connector exits UNARMED at every launch (compose simulation and
// Windows run 5, 2026-09-27).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { runtimeActivation } from '../signon-phases.js';

const fresh = {
  enabled: true, personal_root_pub: 'aa'.repeat(32), machine_id: 'bb'.repeat(32),
  personal_org_uuid: '3b9559e6-ac00-5a73-83c2-310ecc564ed0', org_uuid: null, serves: true,
};

test('a fresh identity activates under the personal org uuid this submission registers', () => {
  assert.equal(runtimeActivation(fresh, { bindingRegistered: true }).org_uuid, fresh.personal_org_uuid);
});

test('with no binding post prepared, nothing is claimed', () => {
  assert.equal(runtimeActivation(fresh).org_uuid, null);
  assert.equal(runtimeActivation(fresh, { bindingRegistered: false }).org_uuid, null);
});

test('an ordinary sign-in keeps the binding it already has', () => {
  const bound = { ...fresh, org_uuid: fresh.personal_org_uuid };
  assert.deepEqual(runtimeActivation(bound), bound);
});

test('a fleet completion activates under the uuid registered in the handoff', () => {
  assert.equal(runtimeActivation(fresh, { completion: { request_id: 'r' } }).org_uuid,
    fresh.personal_org_uuid);
});

test('a runtime with no personal org uuid is activated as it is, whatever was posted', () => {
  const none = { ...fresh, personal_org_uuid: null };
  assert.deepEqual(runtimeActivation(none, { bindingRegistered: true }), none);
});
