import assert from 'node:assert/strict';
import { webcrypto as crypto } from 'node:crypto';
import { readFile } from 'node:fs/promises';

import {
  bytesToHex,
  canonicalJson,
} from '../primitives.js';
import {
  createBrowserStorage,
  createNodeStorage,
} from '../storage.js';

const filePath = process.argv[2];
if (!filePath) {
  throw new Error('usage: node storage.test.mjs SESSION_FILE.json');
}

const METHOD_NAMES = [
  'getSession',
  'putSession',
  'clearSession',
  'getSubjectId',
  'setSubjectId',
];

function assertInterface(adapter) {
  for (const name of METHOD_NAMES) {
    assert.equal(typeof adapter[name], 'function', `${name} must be a function`);
  }
}

const browserStorage = createBrowserStorage();
const memoryStorage = createNodeStorage();
assertInterface(browserStorage);
assertInterface(memoryStorage);

assert.equal(await browserStorage.getSubjectId(), null);
assert.equal(await browserStorage.setSubjectId('browser-ignored'), undefined);

const browserGet = browserStorage.getSession();
assert.equal(browserGet instanceof Promise, true);
await assert.rejects(browserGet, /indexedDB/);
const browserPut = browserStorage.putSession({});
assert.equal(browserPut instanceof Promise, true);
await assert.rejects(browserPut, /indexedDB/);
const browserClear = browserStorage.clearSession();
assert.equal(browserClear instanceof Promise, true);
await assert.rejects(browserClear, /indexedDB/);

assert.equal(await memoryStorage.getSession(), null);
assert.equal(await memoryStorage.getSubjectId(), null);

const keyPair = await crypto.subtle.generateKey(
  { name: 'Ed25519' },
  false,
  ['sign', 'verify'],
);
const childPublicHex = bytesToHex(
  await crypto.subtle.exportKey('raw', keyPair.publicKey),
);
const nowSeconds = Math.floor(Date.now() / 1000);
const certWire = canonicalJson({
  child_pub: childPublicHex,
  not_after: nowSeconds + 3600,
  not_before: nowSeconds - 60,
  org: 'org-1234',
  scope: ['link:publish'],
  sig: '00'.repeat(64),
  subject: {
    id: 'browser-abcd1234',
    kind: 'operator',
  },
  v: 1,
});
const record = {
  key: keyPair.privateKey,
  certWire,
  org: 'org-1234',
  registryUrl: 'https://registry.example',
  rootPub: '11'.repeat(32),
  orgSlug: null,
  createdAt: Date.now(),
};
const serializableRecord = {
  certWire,
  org: record.org,
  registryUrl: record.registryUrl,
  rootPub: record.rootPub,
  orgSlug: record.orgSlug,
  createdAt: record.createdAt,
};

const fileStorage = createNodeStorage({ filePath });
assertInterface(fileStorage);
assert.equal(await fileStorage.getSession(), null);
assert.equal(await fileStorage.getSubjectId(), null);

for (const operation of [
  fileStorage.getSession(),
  fileStorage.putSession(record),
  fileStorage.clearSession(),
  fileStorage.getSubjectId(),
  fileStorage.setSubjectId('browser-probe'),
]) {
  assert.equal(operation instanceof Promise, true);
  await operation;
}

// Reinstall the full record after the interface probe cleared it.
await fileStorage.putSession(record);
await fileStorage.setSubjectId('browser-abcd1234');

const roundTripped = await fileStorage.getSession();
assert.strictEqual(roundTripped, record);
assert.strictEqual(roundTripped.key, keyPair.privateKey);
assert.equal(roundTripped.key.extractable, false);
assert.equal(roundTripped.key.type, 'private');
const signature = new Uint8Array(await crypto.subtle.sign(
  'Ed25519',
  roundTripped.key,
  new Uint8Array([1, 2, 3]),
));
assert.equal(signature.length, 64);
assert.deepEqual(
  Object.fromEntries(
    Object.entries(roundTripped).filter(([name]) => name !== 'key'),
  ),
  serializableRecord,
);

const persisted = JSON.parse(await readFile(filePath, 'utf8'));
assert.equal(persisted.subjectId, 'browser-abcd1234');
assert.deepEqual(persisted.session, serializableRecord);
assert.equal(Object.hasOwn(persisted.session, 'key'), false);

const reloadedStorage = createNodeStorage({ filePath });
assert.equal(await reloadedStorage.getSubjectId(), 'browser-abcd1234');
const reloadedSession = await reloadedStorage.getSession();
assert.deepEqual(reloadedSession, {
  key: null,
  ...serializableRecord,
});
assert.equal(reloadedSession.key, null);

await reloadedStorage.clearSession();
assert.equal(await reloadedStorage.getSession(), null);
assert.equal(await reloadedStorage.getSubjectId(), 'browser-abcd1234');
const cleared = JSON.parse(await readFile(filePath, 'utf8'));
assert.equal(cleared.session, null);
assert.equal(cleared.subjectId, 'browser-abcd1234');

console.log('ceremony storage: browser/Node adapter contract verified');
