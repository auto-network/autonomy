import assert from 'node:assert/strict';

import * as relaykit from '../relaykit-core.js';

assert.deepEqual(Object.keys(relaykit).sort(), [
  'SecureChannel',
  'canonicalJson',
  'openSocket',
  'performHandshake',
  'sendOp',
  'verifyDelegationCert',
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
    org: 'org', token: 'a'.repeat(64), linkPub: 'b'.repeat(64),
  }),
  /channel token/,
);

const hex = (bytes) => Buffer.from(bytes).toString('hex');
const unhex = (value) => new Uint8Array(Buffer.from(value, 'hex'));
const encoder = new TextEncoder();

// Every failed handshake owns teardown. A caller must never lose the only
// reference to a still-live unauthenticated transport.
{
  let closed = false;
  const transport = {
    send() {},
    async recvBinary() { return encoder.encode('{'); },
    close() { closed = true; },
  };
  await assert.rejects(relaykit.performHandshake(transport, {
    org: 'org', token: 'a1'.repeat(16), linkPub: 'b2'.repeat(32),
  }), /malformed SERVER_HELLO/);
  assert.equal(closed, true);
}

{
  let closed = false;
  const transport = {
    send() {},
    async recvBinary() {
      return encoder.encode(relaykit.canonicalJson({
        v: 1, eph_pub: 'a1'.repeat(32), cert: '{}', sig: 'b2'.repeat(64),
        unexpected: true,
      }));
    },
    close() { closed = true; },
  };
  await assert.rejects(relaykit.performHandshake(transport, {
    org: 'org', token: 'a1'.repeat(16), linkPub: 'b2'.repeat(32),
  }), /malformed SERVER_HELLO/);
  assert.equal(closed, true);
}

// Reassembly overflow closes the authenticated channel immediately.
{
  let closed = false;
  const transport = { send() {}, recvBinary() {}, close() { closed = true; } };
  const channel = new relaykit.SecureChannel(transport, {}, {}, new Uint8Array());
  channel.receiveRecord = async () => ({ flags: 0, chunk: new Uint8Array(128 * 1024) });
  await assert.rejects(channel.recvMessage(), /message exceeds maximum size/);
  assert.equal(closed, true);
}

// Socket demux queues have hard count/byte ceilings and reject unknown kinds.
{
  const OriginalWebSocket = globalThis.WebSocket;
  let socket;
  class FakeWebSocket {
    constructor() { socket = this; queueMicrotask(() => this.onopen()); }
    send() {}
    close() { this.closed = true; }
  }
  globalThis.WebSocket = FakeWebSocket;
  try {
    const transport = await relaykit.openSocket('wss://relay.invalid/channel');
    for (let index = 0; index < 129; index += 1) {
      socket.onmessage({ data: new Uint8Array([0, index]).buffer });
    }
    assert.equal(socket.closed, true);
    await assert.rejects(transport.recvBinary(), /queue overflow/);

    const second = await relaykit.openSocket('wss://relay.invalid/channel');
    socket.onmessage({ data: new Uint8Array([9, 1]).buffer });
    assert.equal(socket.closed, true);
    socket.onmessage({ data: new Uint8Array([0, 7]).buffer });
    await assert.rejects(second.recvBinary(), /unknown kind/);

    const third = await relaykit.openSocket('wss://relay.invalid/channel');
    const largeFeed = new Uint8Array((2 * 1024 * 1024) + 1);
    largeFeed[0] = 1;
    socket.onmessage({ data: largeFeed.buffer });
    assert.equal((await third.recvFeed()).length, 2 * 1024 * 1024);

    const fourth = await relaykit.openSocket('wss://relay.invalid/channel');
    fourth.close();
    socket.onmessage({ data: new Uint8Array([0, 8]).buffer });
    await assert.rejects(fourth.recvBinary(), /websocket closed/);
  } finally {
    globalThis.WebSocket = OriginalWebSocket;
  }
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

let badReplyClosed = false;
const badReplyChannel = {
  async sendMessage() {},
  async recvMessage() { return encoder.encode('{"v":1}'); },
  close() { badReplyClosed = true; },
};
await assert.rejects(
  relaykit.sendOp(badReplyChannel, { v: 1, op: 'context' }),
  /newline-terminated/,
);
assert.equal(badReplyClosed, true);

let badRequestClosed = false;
const badRequestChannel = {
  async sendMessage() {},
  async recvMessage() { throw new Error('must not receive'); },
  close() { badRequestClosed = true; },
};
await assert.rejects(
  relaykit.sendOp(badRequestChannel, { v: 1, op: '' }),
  /request must be/,
);
assert.equal(badRequestClosed, true);

let oversizeClosed = false;
const boundedSend = new relaykit.SecureChannel({
  send() {}, recvBinary() {}, close() { oversizeClosed = true; },
}, key, key, transcript);
await assert.rejects(
  boundedSend.sendMessage(new Uint8Array((64 * 1024 * 1024) + 1)),
  /message exceeds maximum size/,
);
assert.equal(oversizeClosed, true);

console.log('relaykit-core: all assertions passed');

// Per-link handshake (graph://807b4e11-3e9): the serving side signs with the
// link's channel private key; the viewer verifies against the public key it
// decoded from the URL fragment. No certificate, no root pin, no registry
// trust. The fake is only the byte transport — signatures, ECDH, HKDF real.
{
  const org = '00000000-0000-4000-8000-0000000000cc';
  const token = 'c1'.repeat(16);
  const link = await crypto.subtle.generateKey('Ed25519', true, ['sign', 'verify']);
  const linkPub = hex(await crypto.subtle.exportKey('raw', link.publicKey));

  const makeTransport = () => {
    let serverHello;
    return {
      closed: false,
      async send(bytes) {
        const client = JSON.parse(new TextDecoder().decode(bytes));
        const serverEph = await crypto.subtle.generateKey('X25519', true, ['deriveBits']);
        const serverPub = hex(await crypto.subtle.exportKey('raw', serverEph.publicKey));
        const signed = encoder.encode(
          `autonomy.network.channel.handshake.v1\n${relaykit.canonicalJson({
            v: 1, org, token, client_eph: client.eph_pub, server_eph: serverPub,
          })}`,
        );
        const sig = hex(await crypto.subtle.sign('Ed25519', link.privateKey, signed));
        serverHello = encoder.encode(relaykit.canonicalJson({
          v: 1, eph_pub: serverPub, link_sig: sig,
        }));
      },
      async recvBinary() { return serverHello; },
      close() { this.closed = true; },
    };
  };

  // Happy path: full handshake, real key derivation.
  const channel = await relaykit.performHandshake(makeTransport(), {
    org, token, linkPub,
  });
  assert.ok(channel instanceof relaykit.SecureChannel);

  // Wrong fragment key: signature refuses.
  const wrong = hex(await crypto.subtle.exportKey(
    'raw',
    (await crypto.subtle.generateKey('Ed25519', true, ['sign', 'verify'])).publicKey,
  ));
  const t2 = makeTransport();
  await assert.rejects(
    relaykit.performHandshake(t2, { org, token, linkPub: wrong }),
    /signature does not verify/,
  );
  assert.equal(t2.closed, true);

  // Missing fragment authority is rejected before any server bytes are read.
  const t3 = makeTransport();
  await assert.rejects(
    relaykit.performHandshake(t3, { org, token }),
    /link public key/,
  );
  assert.equal(t3.closed, true);
}
