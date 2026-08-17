import assert from 'node:assert/strict';

import * as relaykit from '../relaykit-core.js';

assert.deepEqual(Object.keys(relaykit).sort(), [
  'SecureChannel',
  'canonicalJson',
  'openSocket',
  'performHandshake',
  'sendOp',
]);

assert.equal(
  relaykit.canonicalJson({ z: 'é', a: [true, null, 3] }),
  '{"a":[true,null,3],"z":"\\u00e9"}',
);
assert.throws(() => relaykit.canonicalJson({ bad: 1.5 }), /floats/);
assert.throws(() => relaykit.openSocket('https://relay.auto.network'), /ws or wss/);

const fakeTransport = {
  send() {},
  async recvBinary() { throw new Error('should not read'); },
  close() {},
};
await assert.rejects(
  relaykit.performHandshake(fakeTransport, {
    org: 'org', token: 'a'.repeat(64), rootPub: 'b'.repeat(64),
  }),
  /channel token/,
);

const hex = (bytes) => Buffer.from(bytes).toString('hex');
const unhex = (value) => new Uint8Array(Buffer.from(value, 'hex'));
const encoder = new TextEncoder();

// Exercise the real handshake against an identity-neutral, direct-root
// tunnel:serve certificate. The fake is only the byte transport; all key
// generation, signatures, verification, ECDH, HKDF, and parsing are real.
{
  const org = '00000000-0000-4000-8000-0000000000aa';
  const token = 'a1'.repeat(16);
  const now = Math.floor(Date.now() / 1000);
  const root = await crypto.subtle.generateKey('Ed25519', true, ['sign', 'verify']);
  const serving = await crypto.subtle.generateKey('Ed25519', true, ['sign', 'verify']);
  const rootPub = hex(await crypto.subtle.exportKey('raw', root.publicKey));
  const servingPub = hex(await crypto.subtle.exportKey('raw', serving.publicKey));
  const payload = {
    v: 1,
    child_pub: servingPub,
    scope: ['tunnel:serve'],
    org,
    subject: { kind: 'operator', id: servingPub },
    not_before: now - 60,
    not_after: now + 3600,
  };
  const certSig = hex(await crypto.subtle.sign(
    'Ed25519', root.privateKey,
    encoder.encode(`autonomy.idkit.cert.v1\n${relaykit.canonicalJson(payload)}`),
  ));
  const cert = relaykit.canonicalJson({ ...payload, sig: certSig });
  let serverHello;
  const transport = {
    async send(bytes) {
      const client = JSON.parse(new TextDecoder().decode(bytes));
      const serverEph = await crypto.subtle.generateKey(
        'X25519', true, ['deriveBits'],
      );
      const serverPub = hex(await crypto.subtle.exportKey('raw', serverEph.publicKey));
      const signed = encoder.encode(
        `autonomy.network.channel.handshake.v1\n${relaykit.canonicalJson({
          v: 1,
          org,
          token,
          client_eph: client.eph_pub,
          server_eph: serverPub,
        })}`,
      );
      const sig = hex(await crypto.subtle.sign('Ed25519', serving.privateKey, signed));
      serverHello = encoder.encode(relaykit.canonicalJson({
        v: 1, eph_pub: serverPub, cert, sig,
      }));
    },
    async recvBinary() { return serverHello; },
    close() {},
  };
  const channel = await relaykit.performHandshake(transport, {
    org, token, rootPub,
  });
  assert.ok(channel instanceof relaykit.SecureChannel);
}

// The shared record layer must carry a real multi-record message, not only
// single-record examples. Direction is swapped to model the peer reading the
// browser's c2s records with the same AES key and transcript.
const keyBytes = new Uint8Array(32).fill(7);
const key = await crypto.subtle.importKey(
  'raw', keyBytes, 'AES-GCM', false, ['encrypt', 'decrypt'],
);
const transcript = new Uint8Array(32).fill(9);
const records = [];
const txTransport = {
  send(bytes) { records.push(bytes); },
  async recvBinary() { throw new Error('tx does not receive'); },
  close() {},
};
const tx = new relaykit.SecureChannel(txTransport, key, key, transcript);
const message = new Uint8Array(130 * 1024);
for (let index = 0; index < message.length; index += 1) message[index] = index % 251;
await tx.sendMessage(message);
assert.equal(records.length, 3);
assert.ok(records.every((record) => record.length <= (60 * 1024) + 25));

const rxTransport = {
  async recvBinary() { return records.shift(); },
  send() {},
  close() {},
};
const rx = new relaykit.SecureChannel(rxTransport, key, key, transcript);
rx.receiveDirection = new TextEncoder().encode('c2s\x00');
assert.deepEqual(await rx.recvMessage(), message);

const requests = [];
const reply = { ok: true, v: 1 };
const opChannel = {
  async sendMessage(bytes) { requests.push(new TextDecoder().decode(bytes)); },
  async recvMessage() {
    return new TextEncoder().encode(`${relaykit.canonicalJson(reply)}\n`);
  },
};
assert.deepEqual(
  await relaykit.sendOp(opChannel, { z: 2, v: 1, op: 'context' }),
  reply,
);
assert.equal(requests[0], '{"op":"context","v":1,"z":2}\n');

console.log('relaykit-core: all assertions passed');
