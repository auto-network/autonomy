/*
 * In-memory Node virtual authenticator for the dashboard's WebAuthn routes.
 *
 * Credentials are ES256/P-256. Private keys and per-credential PRF secrets
 * remain non-extractable/in-memory; only WebAuthn response JSON leaves the
 * adapter.
 */

import { webcrypto } from 'node:crypto';

const cryptoApi = globalThis.crypto?.subtle
  ? globalThis.crypto
  : webcrypto;
const textEncoder = new TextEncoder();

// Ported byte-for-byte in behavior from network-onboarding.js. The shared
// primitives expose standard base64, not WebAuthn's unpadded base64url.
function b64uToBytes(s) {
  let b64 = s.replace(/-/g, '+').replace(/_/g, '/');
  while (b64.length % 4) b64 += '=';
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
  return out;
}

function bytesToB64u(bytes) {
  const b = new Uint8Array(bytes);
  let bin = '';
  for (let i = 0; i < b.length; i += 1) {
    bin += String.fromCharCode(b[i]);
  }
  return btoa(bin)
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '');
}

function concatBytes(...parts) {
  const length = parts.reduce((total, part) => total + part.length, 0);
  const output = new Uint8Array(length);
  let offset = 0;
  for (const part of parts) {
    output.set(part, offset);
    offset += part.length;
  }
  return output;
}

function uint16(value) {
  const output = new Uint8Array(2);
  new DataView(output.buffer).setUint16(0, value, false);
  return output;
}

function uint32(value) {
  const output = new Uint8Array(4);
  new DataView(output.buffer).setUint32(0, value, false);
  return output;
}

function cborHead(majorType, length) {
  if (!Number.isSafeInteger(length) || length < 0) {
    throw new Error('CBOR length must be a non-negative safe integer');
  }
  if (length < 24) {
    return new Uint8Array([(majorType << 5) | length]);
  }
  if (length < 0x100) {
    return new Uint8Array([(majorType << 5) | 24, length]);
  }
  if (length < 0x10000) {
    return concatBytes(
      new Uint8Array([(majorType << 5) | 25]),
      uint16(length),
    );
  }
  if (length <= 0xffffffff) {
    return concatBytes(
      new Uint8Array([(majorType << 5) | 26]),
      uint32(length),
    );
  }
  throw new Error('CBOR value is too large');
}

function cborInteger(value) {
  if (!Number.isSafeInteger(value)) {
    throw new Error('CBOR integer must be safe');
  }
  if (value >= 0) return cborHead(0, value);
  return cborHead(1, -1 - value);
}

function cborBytes(value) {
  const bytes = new Uint8Array(value);
  return concatBytes(cborHead(2, bytes.length), bytes);
}

function cborText(value) {
  const bytes = textEncoder.encode(value);
  return concatBytes(cborHead(3, bytes.length), bytes);
}

function cborMap(entries) {
  const encoded = [cborHead(5, entries.length)];
  for (const [key, value] of entries) {
    encoded.push(cborEncode(key), cborEncode(value));
  }
  return concatBytes(...encoded);
}

function cborEncode(value) {
  if (Number.isSafeInteger(value)) return cborInteger(value);
  if (typeof value === 'string') return cborText(value);
  if (value instanceof Uint8Array) return cborBytes(value);
  if (value instanceof Map) return cborMap(Array.from(value.entries()));
  if (
    value
    && typeof value === 'object'
    && !Array.isArray(value)
  ) {
    return cborMap(Object.entries(value));
  }
  throw new Error(`unsupported CBOR value: ${typeof value}`);
}

function clientData(type, challenge, origin) {
  return textEncoder.encode(JSON.stringify({
    type,
    challenge,
    origin,
    crossOrigin: false,
  }));
}

async function rpIdHash(rpId) {
  return new Uint8Array(await cryptoApi.subtle.digest(
    'SHA-256',
    textEncoder.encode(rpId),
  ));
}

function derInteger(bytes) {
  let start = 0;
  while (
    start < bytes.length - 1
    && bytes[start] === 0
    && (bytes[start + 1] & 0x80) === 0
  ) {
    start += 1;
  }
  let value = bytes.slice(start);
  if (value[0] & 0x80) {
    value = concatBytes(new Uint8Array([0]), value);
  }
  return concatBytes(new Uint8Array([0x02, value.length]), value);
}

function rawEcdsaToDer(rawSignature) {
  const raw = new Uint8Array(rawSignature);
  if (raw.length !== 64) {
    throw new Error(
      `WebCrypto ES256 signature must be 64 raw bytes, got ${raw.length}`,
    );
  }
  const r = derInteger(raw.slice(0, 32));
  const s = derInteger(raw.slice(32));
  const body = concatBytes(r, s);
  return concatBytes(new Uint8Array([0x30, body.length]), body);
}

function credentialBytes(value) {
  if (value === undefined || value === null) {
    return cryptoApi.getRandomValues(new Uint8Array(32));
  }
  if (typeof value === 'string') return b64uToBytes(value);
  const bytes = new Uint8Array(value);
  if (bytes.length === 0 || bytes.length > 0xffff) {
    throw new Error('credential id must contain 1..65535 bytes');
  }
  return bytes;
}

function prfSalt(prf) {
  if (!prf) return null;
  const candidate = prf.eval?.first ?? prf.first ?? prf;
  if (typeof candidate === 'string') return b64uToBytes(candidate);
  if (
    candidate instanceof Uint8Array
    || candidate instanceof ArrayBuffer
    || ArrayBuffer.isView(candidate)
  ) {
    return new Uint8Array(
      candidate.buffer || candidate,
      candidate.byteOffset || 0,
      candidate.byteLength,
    );
  }
  throw new Error('PRF input must carry a first evaluation salt');
}

class VirtualAuthenticator {
  constructor() {
    this.credentials = new Map();
  }

  async createCredential({
    rpId,
    origin,
    challenge,
    credentialId,
    prf = false,
  }) {
    if (!rpId || !origin || !challenge) {
      throw new Error('createCredential requires rpId, origin, and challenge');
    }
    const credentialIdBytes = credentialBytes(credentialId);
    const credentialIdB64u = bytesToB64u(credentialIdBytes);
    if (this.credentials.has(credentialIdB64u)) {
      throw new Error('credential id is already present');
    }

    const keyPair = await cryptoApi.subtle.generateKey(
      { name: 'ECDSA', namedCurve: 'P-256' },
      false,
      ['sign', 'verify'],
    );
    const publicJwk = await cryptoApi.subtle.exportKey(
      'jwk',
      keyPair.publicKey,
    );
    const coseKey = cborEncode(new Map([
      [1, 2],
      [3, -7],
      [-1, 1],
      [-2, b64uToBytes(publicJwk.x)],
      [-3, b64uToBytes(publicJwk.y)],
    ]));
    const authenticatorData = concatBytes(
      await rpIdHash(rpId),
      new Uint8Array([0x45]),
      uint32(0),
      new Uint8Array(16),
      uint16(credentialIdBytes.length),
      credentialIdBytes,
      coseKey,
    );
    const attestationObject = cborEncode({
      fmt: 'none',
      attStmt: new Map(),
      authData: authenticatorData,
    });
    const hmacSecret = cryptoApi.getRandomValues(new Uint8Array(32));
    this.credentials.set(credentialIdB64u, {
      privateKey: keyPair.privateKey,
      hmacSecret,
      signCount: 0,
      rpId,
    });

    return {
      id: credentialIdB64u,
      rawId: credentialIdB64u,
      type: 'public-key',
      authenticatorAttachment: 'platform',
      clientExtensionResults: prf ? { prf: { enabled: true } } : {},
      response: {
        clientDataJSON: bytesToB64u(
          clientData('webauthn.create', challenge, origin),
        ),
        attestationObject: bytesToB64u(attestationObject),
        transports: ['internal'],
      },
    };
  }

  async getAssertion({
    rpId,
    origin,
    challenge,
    credentialId,
    prf = null,
  }) {
    if (!rpId || !origin || !challenge || !credentialId) {
      throw new Error(
        'getAssertion requires rpId, origin, challenge, and credentialId',
      );
    }
    const credentialIdB64u = typeof credentialId === 'string'
      ? credentialId
      : bytesToB64u(credentialId);
    const credential = this.credentials.get(credentialIdB64u);
    if (!credential || credential.rpId !== rpId) {
      throw new Error('credential is not registered for this RP ID');
    }

    credential.signCount += 1;
    const authenticatorData = concatBytes(
      await rpIdHash(rpId),
      new Uint8Array([0x05]),
      uint32(credential.signCount),
    );
    const clientDataJSON = clientData(
      'webauthn.get',
      challenge,
      origin,
    );
    const clientDataHash = new Uint8Array(await cryptoApi.subtle.digest(
      'SHA-256',
      clientDataJSON,
    ));
    const rawSignature = await cryptoApi.subtle.sign(
      { name: 'ECDSA', hash: 'SHA-256' },
      credential.privateKey,
      concatBytes(authenticatorData, clientDataHash),
    );

    const salt = prfSalt(prf);
    let clientExtensionResults = {};
    if (salt) {
      const hmacKey = await cryptoApi.subtle.importKey(
        'raw',
        credential.hmacSecret,
        { name: 'HMAC', hash: 'SHA-256' },
        false,
        ['sign'],
      );
      const result = await cryptoApi.subtle.sign('HMAC', hmacKey, salt);
      clientExtensionResults = {
        prf: {
          results: {
            first: bytesToB64u(result),
          },
        },
      };
    }

    return {
      id: credentialIdB64u,
      rawId: credentialIdB64u,
      type: 'public-key',
      authenticatorAttachment: 'platform',
      clientExtensionResults,
      response: {
        clientDataJSON: bytesToB64u(clientDataJSON),
        authenticatorData: bytesToB64u(authenticatorData),
        signature: bytesToB64u(rawEcdsaToDer(rawSignature)),
      },
    };
  }

  setSignCount(credentialId, signCount) {
    if (!Number.isSafeInteger(signCount) || signCount < 0) {
      throw new Error('sign count must be a non-negative safe integer');
    }
    const credential = this.credentials.get(credentialId);
    if (!credential) throw new Error('unknown credential');
    credential.signCount = signCount;
  }
}

export {
  VirtualAuthenticator,
};
