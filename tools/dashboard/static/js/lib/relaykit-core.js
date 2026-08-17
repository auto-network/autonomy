/* Origin-neutral RelayKit browser core.
 *
 * This exact module is served by both the Dashboard and auto.network. It owns
 * only transport authentication, encrypted records, and canonical operation
 * framing. Invitation, identity ceremony, WebRTC/ICE selection, and product
 * semantics belong to callers.
 */

const CERT_DOMAIN = 'autonomy.idkit.cert.v1\n';
const HANDSHAKE_DOMAIN = 'autonomy.network.channel.handshake.v1\n';
const KEYS_INFO = 'autonomy.network.channel.keys.v1';
const DIR_C2S = 'c2s\x00';
const DIR_S2C = 's2c\x00';
const SEND_CHUNK_SIZE = 60 * 1024;
const MAX_RECORD_CHUNK_SIZE = 128 * 1024;
const MAX_MESSAGE_SIZE = 64 * 1024 * 1024;
const MAX_CHAIN_DEPTH = 16;
const HANDSHAKE_VERSION = 1;
const STREAM_FINAL = 0x01;
const MSG_END = 0x02;
const KNOWN_RECORD_FLAGS = STREAM_FINAL | MSG_END;
const VIEWER_KIND_RECORD = 0x00;
const VIEWER_KIND_FEED = 0x01;
const MAX_SOCKET_QUEUE_FRAMES = 128;
const MAX_SOCKET_QUEUE_BYTES = 8 * 1024 * 1024;
const MAX_SOCKET_FRAME_BYTES = MAX_RECORD_CHUNK_SIZE + 64;

const te = new TextEncoder();
const td = new TextDecoder('utf-8', { fatal: true });

export function canonicalJson(value) {
  const parts = [];
  encodeValue(value, parts);
  return parts.join('');
}

function encodeValue(value, parts) {
  if (value === null) { parts.push('null'); return; }
  const kind = typeof value;
  if (kind === 'boolean') { parts.push(value ? 'true' : 'false'); return; }
  if (kind === 'number') {
    if (!Number.isInteger(value)) throw new Error('floats are not canonical');
    parts.push(String(value));
    return;
  }
  if (kind === 'string') { parts.push(encodeString(value)); return; }
  if (Array.isArray(value)) {
    parts.push('[');
    value.forEach((item, index) => {
      if (index) parts.push(',');
      encodeValue(item, parts);
    });
    parts.push(']');
    return;
  }
  if (kind === 'object') {
    parts.push('{');
    Object.keys(value).sort().forEach((key, index) => {
      if (index) parts.push(',');
      parts.push(encodeString(key), ':');
      encodeValue(value[key], parts);
    });
    parts.push('}');
    return;
  }
  throw new Error(`type not allowed in canonical JSON: ${kind}`);
}

const SHORT_ESCAPES = {
  8: '\\b', 9: '\\t', 10: '\\n', 12: '\\f', 13: '\\r', 34: '\\"', 92: '\\\\',
};

function encodeString(value) {
  let out = '"';
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (SHORT_ESCAPES[code]) out += SHORT_ESCAPES[code];
    else if (code < 0x20 || code > 0x7e) {
      out += `\\u${code.toString(16).padStart(4, '0')}`;
    } else out += value[index];
  }
  return `${out}"`;
}

function hexToBytes(hex, expectedLength, what) {
  if (
    typeof hex !== 'string'
    || hex !== hex.toLowerCase()
    || (expectedLength && hex.length !== expectedLength)
    || /[^0-9a-f]/.test(hex)
    || hex.length % 2
  ) {
    throw new Error(`${what} is not valid lowercase hex`);
  }
  const bytes = new Uint8Array(hex.length / 2);
  for (let index = 0; index < bytes.length; index += 1) {
    bytes[index] = parseInt(hex.substr(index * 2, 2), 16);
  }
  return bytes;
}

function bytesToHex(bytes) {
  return Array.from(
    new Uint8Array(bytes),
    (byte) => byte.toString(16).padStart(2, '0'),
  ).join('');
}

function concatBytes(...arrays) {
  const total = arrays.reduce((size, array) => size + array.length, 0);
  const result = new Uint8Array(total);
  let offset = 0;
  for (const array of arrays) {
    result.set(array, offset);
    offset += array.length;
  }
  return result;
}

function hasExactFields(value, fields) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const actual = Object.keys(value).sort();
  const expected = [...fields].sort();
  return actual.length === expected.length
    && actual.every((field, index) => field === expected[index]);
}

function isCanonicalStringList(value) {
  return Array.isArray(value)
    && value.length > 0
    && value.every((item) => typeof item === 'string' && item.length > 0)
    && new Set(value).size === value.length
    && value.every((item, index) => index === 0 || value[index - 1] < item);
}

function validateCertificateShape(cert) {
  const fields = [
    'v', 'child_pub', 'scope', 'org', 'subject', 'not_before', 'not_after', 'sig',
  ];
  if (cert.target_types !== undefined) fields.push('target_types');
  if (cert.parent_cert !== undefined) fields.push('parent_cert');
  if (
    !hasExactFields(cert, fields)
    || cert.v !== 1
    || typeof cert.org !== 'string'
    || !cert.org
    || !hasExactFields(cert.subject, ['kind', 'id'])
    || typeof cert.subject.kind !== 'string'
    || typeof cert.subject.id !== 'string'
    || !Number.isSafeInteger(cert.not_before)
    || !Number.isSafeInteger(cert.not_after)
    || cert.not_before < 0
    || cert.not_after <= cert.not_before
    || !isCanonicalStringList(cert.scope)
    || (
      cert.target_types !== undefined
      && !isCanonicalStringList(cert.target_types)
    )
  ) {
    throw new Error('certificate has invalid fields');
  }
  hexToBytes(cert.child_pub, 64, 'certificate child public key');
  hexToBytes(cert.sig, 128, 'certificate signature');
  if (
    cert.parent_cert !== undefined
    && (!cert.parent_cert || typeof cert.parent_cert !== 'object'
      || Array.isArray(cert.parent_cert))
  ) {
    throw new Error('certificate parent is invalid');
  }
}

function sequenceBytes(sequence) {
  const result = new Uint8Array(8);
  new DataView(result.buffer).setBigUint64(0, BigInt(sequence));
  return result;
}

async function ed25519Verify(publicHex, signatureHex, data) {
  const key = await crypto.subtle.importKey(
    'raw', hexToBytes(publicHex, 64, 'public key'), 'Ed25519', false, ['verify'],
  );
  return crypto.subtle.verify(
    'Ed25519', key, hexToBytes(signatureHex, 128, 'signature'), data,
  );
}

function certPayload(cert) {
  const payload = {
    v: cert.v,
    child_pub: cert.child_pub,
    scope: cert.scope,
    org: cert.org,
    subject: cert.subject,
    not_before: cert.not_before,
    not_after: cert.not_after,
  };
  if (cert.target_types !== undefined) payload.target_types = cert.target_types;
  if (cert.parent_cert !== undefined) payload.parent_cert = certToDict(cert.parent_cert);
  return payload;
}

function certToDict(cert) {
  return { ...certPayload(cert), sig: cert.sig };
}

function certificateChain(cert) {
  const chain = [];
  for (let cursor = cert; cursor; cursor = cursor.parent_cert) {
    chain.push(cursor);
    if (chain.length > MAX_CHAIN_DEPTH) throw new Error('chain exceeds depth cap');
  }
  chain.reverse();
  return chain;
}

function isStrictSubset(child, parent) {
  const parentValues = new Set(parent);
  return child.every((item) => parentValues.has(item)) && child.length < parentValues.size;
}

async function verifyChain(certWire, rootPublicHex, org, now) {
  let cert;
  try {
    cert = JSON.parse(certWire);
  } catch (_) {
    throw new Error('certificate is not valid JSON');
  }
  if (canonicalJson(cert) !== certWire) throw new Error('cert is not in canonical wire form');

  const chain = certificateChain(cert);
  let signerPublic = rootPublicHex;
  let parent = null;
  for (const hop of chain) {
    validateCertificateShape(hop);
    const payload = te.encode(CERT_DOMAIN + canonicalJson(certPayload(hop)));
    if (!(await ed25519Verify(signerPublic, hop.sig, payload))) {
      throw new Error('hop signature does not verify against its parent key');
    }
    if (hop.org !== org) throw new Error('cert org mismatch');
    if (now < hop.not_before || now > hop.not_after) {
      throw new Error('cert hop outside validity window');
    }
    if (parent) {
      if (!isStrictSubset(hop.scope, parent.scope)) throw new Error('scope escalation');
      if (hop.not_before < parent.not_before || hop.not_after >= parent.not_after) {
        throw new Error('validity window not nested');
      }
      if (
        parent.target_types !== undefined
        && (
          hop.target_types === undefined
          || !hop.target_types.every((target) => parent.target_types.includes(target))
        )
      ) {
        throw new Error('target_types escalation');
      }
    }
    signerPublic = hop.child_pub;
    parent = hop;
  }
  return chain[chain.length - 1];
}

function requireNeutralViewerCertificate(leaf) {
  if (
    leaf.parent_cert !== undefined
    || !Array.isArray(leaf.scope)
    || leaf.scope.length !== 1
    || leaf.scope[0] !== 'tunnel:serve'
    || !leaf.subject
    || leaf.subject.kind !== 'operator'
    || leaf.subject.id !== leaf.child_pub
  ) {
    throw new Error('SERVER_HELLO certificate is not identity-neutral');
  }
}

function requireTransport(transport) {
  if (
    !transport
    || typeof transport.send !== 'function'
    || typeof transport.recvBinary !== 'function'
    || typeof transport.close !== 'function'
  ) {
    throw new Error('performHandshake requires a RelayKit transport');
  }
}

export async function performHandshake(transport, { org, token, rootPub } = {}) {
  requireTransport(transport);
  try {
    if (typeof org !== 'string' || !org) throw new Error('org is required');
    hexToBytes(token, 32, 'channel token');
    hexToBytes(rootPub, 64, 'root public key');

    const ephemeral = await crypto.subtle.generateKey('X25519', false, ['deriveBits']);
    const clientEphemeral = bytesToHex(
      await crypto.subtle.exportKey('raw', ephemeral.publicKey),
    );
    await Promise.resolve(transport.send(te.encode(canonicalJson({
      v: HANDSHAKE_VERSION, eph_pub: clientEphemeral,
    }))));

    const helloBytes = await transport.recvBinary();
    let hello;
    try {
      hello = JSON.parse(td.decode(helloBytes));
    } catch (_) {
      throw new Error('malformed SERVER_HELLO');
    }
    if (
      !hello
      || typeof hello !== 'object'
      || Array.isArray(hello)
      || !hasExactFields(hello, ['v', 'eph_pub', 'cert', 'sig'])
      || hello.v !== HANDSHAKE_VERSION
      || typeof hello.eph_pub !== 'string'
      || typeof hello.cert !== 'string'
      || typeof hello.sig !== 'string'
    ) {
      throw new Error('malformed SERVER_HELLO');
    }

    const leaf = await verifyChain(
      hello.cert, rootPub, org, Math.floor(Date.now() / 1000),
    );
    requireNeutralViewerCertificate(leaf);
    const signedPayload = te.encode(HANDSHAKE_DOMAIN + canonicalJson({
      v: HANDSHAKE_VERSION,
      org,
      token,
      client_eph: clientEphemeral,
      server_eph: hello.eph_pub,
    }));
    if (!(await ed25519Verify(leaf.child_pub, hello.sig, signedPayload))) {
      throw new Error('SERVER_HELLO signature does not verify');
    }

    const transcript = new Uint8Array(await crypto.subtle.digest(
      'SHA-256',
      te.encode(HANDSHAKE_DOMAIN + canonicalJson({
        v: HANDSHAKE_VERSION,
        org,
        token,
        client_eph: clientEphemeral,
        server_eph: hello.eph_pub,
        cert: hello.cert,
      })),
    ));
    const serverKey = await crypto.subtle.importKey(
      'raw', hexToBytes(hello.eph_pub, 64, 'server eph'), 'X25519', false, [],
    );
    const shared = await crypto.subtle.deriveBits(
      { name: 'X25519', public: serverKey }, ephemeral.privateKey, 256,
    );
    const hkdfKey = await crypto.subtle.importKey('raw', shared, 'HKDF', false, ['deriveBits']);
    const keyMaterial = new Uint8Array(await crypto.subtle.deriveBits({
      name: 'HKDF',
      hash: 'SHA-256',
      salt: transcript,
      info: te.encode(KEYS_INFO),
    }, hkdfKey, 512));
    const sendKey = await crypto.subtle.importKey(
      'raw', keyMaterial.slice(0, 32), 'AES-GCM', false, ['encrypt'],
    );
    const receiveKey = await crypto.subtle.importKey(
      'raw', keyMaterial.slice(32), 'AES-GCM', false, ['decrypt'],
    );
    return new SecureChannel(transport, sendKey, receiveKey, transcript);
  } catch (error) {
    try { transport.close(); } catch (_) { /* already closed */ }
    throw error;
  }
}

export class SecureChannel {
  constructor(transport, sendKey, receiveKey, transcript) {
    requireTransport(transport);
    this.transport = transport;
    this.sendKey = sendKey;
    this.receiveKey = receiveKey;
    this.transcript = transcript;
    this.sendSequence = 0;
    this.receiveSequence = 0;
    this.sendDirection = te.encode(DIR_C2S);
    this.receiveDirection = te.encode(DIR_S2C);
  }

  async sendMessage(bytes) {
    if (!(bytes instanceof Uint8Array)) throw new Error('message must be bytes');
    if (bytes.length > MAX_MESSAGE_SIZE) {
      this.close();
      throw new Error('message exceeds maximum size');
    }
    for (let offset = 0; ; offset += SEND_CHUNK_SIZE) {
      const chunk = bytes.slice(offset, offset + SEND_CHUNK_SIZE);
      const final = offset + SEND_CHUNK_SIZE >= bytes.length;
      const sequence = sequenceBytes(this.sendSequence);
      this.sendSequence += 1;
      const plaintext = concatBytes(new Uint8Array([final ? STREAM_FINAL : 0]), chunk);
      const ciphertext = new Uint8Array(await crypto.subtle.encrypt({
        name: 'AES-GCM',
        iv: concatBytes(this.sendDirection, sequence),
        additionalData: concatBytes(
          this.transcript, this.sendDirection, sequence,
        ),
      }, this.sendKey, plaintext));
      await Promise.resolve(this.transport.send(concatBytes(sequence, ciphertext)));
      if (final) return;
    }
  }

  close() {
    this.transport.close();
  }

  async receiveRecord() {
    try {
      const record = await this.transport.recvBinary();
      if (!(record instanceof Uint8Array) || record.length < 25) {
        throw new Error('record is too short');
      }
      const sequence = record.slice(0, 8);
      if (
        new DataView(sequence.buffer, sequence.byteOffset, 8).getBigUint64(0)
        !== BigInt(this.receiveSequence)
      ) {
        throw new Error('record out of sequence');
      }
      const plaintext = new Uint8Array(await crypto.subtle.decrypt({
        name: 'AES-GCM',
        iv: concatBytes(this.receiveDirection, sequence),
        additionalData: concatBytes(
          this.transcript, this.receiveDirection, sequence,
        ),
      }, this.receiveKey, record.slice(8)));
      this.receiveSequence += 1;
      if (plaintext.length < 1) throw new Error('record plaintext is empty');
      const flags = plaintext[0];
      const chunk = plaintext.slice(1);
      if (flags & ~KNOWN_RECORD_FLAGS) throw new Error('record has unknown flags');
      if (chunk.length > MAX_RECORD_CHUNK_SIZE) {
        throw new Error('record chunk exceeds maximum size');
      }
      return { flags, chunk };
    } catch (error) {
      this.close();
      throw error;
    }
  }

  async *recvMessageStream() {
    let chunks = [];
    let size = 0;
    for (;;) {
      const { flags, chunk } = await this.receiveRecord();
      size += chunk.length;
      if (size > MAX_MESSAGE_SIZE) {
        this.close();
        throw new Error('message exceeds maximum size');
      }
      chunks.push(chunk);
      if (flags & (MSG_END | STREAM_FINAL)) {
        yield concatBytes(...chunks);
        chunks = [];
        size = 0;
      }
      if (flags & STREAM_FINAL) return;
    }
  }

  async recvMessage() {
    let message = null;
    for await (const candidate of this.recvMessageStream()) {
      if (message !== null) {
        throw new Error('one-shot response carried multiple messages');
      }
      message = candidate;
    }
    if (message === null) throw new Error('response exchange ended without a message');
    return message;
  }
}

function typedTransportError(message) {
  const error = new Error(message);
  error.autonetKind = 'disconnected';
  return error;
}

export function openSocket(url) {
  if (typeof url !== 'string' || !/^wss?:\/\//.test(url)) {
    throw new Error('socket URL must use ws or wss');
  }
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(url);
    socket.binaryType = 'arraybuffer';
    const records = [];
    const recordWaiters = [];
    const feeds = [];
    const feedWaiters = [];
    let closed = null;
    let recordBytes = 0;
    let feedBytes = 0;
    const fail = (error) => {
      if (closed) return;
      closed = error;
      records.length = 0;
      feeds.length = 0;
      recordBytes = 0;
      feedBytes = 0;
      while (recordWaiters.length) recordWaiters.shift().reject(error);
      while (feedWaiters.length) feedWaiters.shift().reject(error);
    };
    const take = (queue, waiters, kind) => {
      if (queue.length) {
        const value = queue.shift();
        if (kind === VIEWER_KIND_RECORD) recordBytes -= value.length;
        else feedBytes -= value.length;
        return Promise.resolve(value);
      }
      if (closed) return Promise.reject(closed);
      return new Promise((resolveValue, rejectValue) => {
        waiters.push({ resolve: resolveValue, reject: rejectValue });
      });
    };
    socket.onopen = () => resolve({
      send: (bytes) => socket.send(bytes),
      recvBinary: () => take(records, recordWaiters, VIEWER_KIND_RECORD),
      recvFeed: () => take(feeds, feedWaiters, VIEWER_KIND_FEED),
      close: () => socket.close(),
    });
    socket.onmessage = (event) => {
      if (!(event.data instanceof ArrayBuffer)) {
        fail(typedTransportError('websocket sent a non-binary frame'));
        socket.close();
        return;
      }
      const tagged = new Uint8Array(event.data);
      if (!tagged.length || tagged.length - 1 > MAX_SOCKET_FRAME_BYTES) {
        fail(typedTransportError('websocket frame violates size bounds'));
        socket.close();
        return;
      }
      const payload = tagged.subarray(1);
      if (tagged[0] === VIEWER_KIND_FEED) {
        if (feedWaiters.length) feedWaiters.shift().resolve(payload);
        else {
          if (
            feeds.length >= MAX_SOCKET_QUEUE_FRAMES
            || feedBytes + payload.length > MAX_SOCKET_QUEUE_BYTES
          ) {
            fail(typedTransportError('websocket feed queue overflow'));
            socket.close();
            return;
          }
          feeds.push(payload);
          feedBytes += payload.length;
        }
      } else if (tagged[0] === VIEWER_KIND_RECORD) {
        if (recordWaiters.length) recordWaiters.shift().resolve(payload);
        else {
          if (
            records.length >= MAX_SOCKET_QUEUE_FRAMES
            || recordBytes + payload.length > MAX_SOCKET_QUEUE_BYTES
          ) {
            fail(typedTransportError('websocket record queue overflow'));
            socket.close();
            return;
          }
          records.push(payload);
          recordBytes += payload.length;
        }
      } else {
        fail(typedTransportError('websocket frame has unknown kind'));
        socket.close();
      }
    };
    socket.onerror = () => {
      const error = typedTransportError('websocket error');
      fail(error);
      reject(error);
    };
    socket.onclose = (event) => fail(typedTransportError(
      `websocket closed (${event.code})`,
    ));
  });
}

export async function sendOp(channel, request) {
  if (
    !channel
    || typeof channel.sendMessage !== 'function'
    || typeof channel.recvMessage !== 'function'
  ) {
    throw new Error('sendOp requires a SecureChannel');
  }
  try {
    if (
      !request
      || typeof request !== 'object'
      || Array.isArray(request)
      || request.v !== 1
      || typeof request.op !== 'string'
      || !request.op
    ) {
      throw new Error('sendOp request must be a {v:1, op:string, ...} object');
    }
    await channel.sendMessage(te.encode(`${canonicalJson(request)}\n`));
    const raw = await channel.recvMessage();
    if (!(raw instanceof Uint8Array)) throw new Error('channel reply must be bytes');
    let wire;
    try {
      wire = td.decode(raw);
    } catch (_) {
      throw new Error('channel reply is not valid UTF-8');
    }
    if (!wire.endsWith('\n') || wire.slice(0, -1).includes('\n')) {
      throw new Error('channel reply must be one newline-terminated JSON object');
    }
    const jsonWire = wire.slice(0, -1);
    let reply;
    try {
      reply = JSON.parse(jsonWire);
    } catch (_) {
      throw new Error('channel reply is not valid JSON');
    }
    if (
      !reply
      || typeof reply !== 'object'
      || Array.isArray(reply)
      || reply.v !== 1
      || canonicalJson(reply) !== jsonWire
    ) {
      throw new Error('channel reply must be canonical {v:1, ...} JSON');
    }
    return reply;
  } catch (error) {
    try { channel.close(); } catch (_) { /* already closed */ }
    throw error;
  }
}
