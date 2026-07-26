import assert from 'node:assert/strict';

import { VirtualAuthenticator } from '../authenticator-node.js';

function b64uToBytes(value) {
  let base64 = value.replace(/-/g, '+').replace(/_/g, '/');
  while (base64.length % 4) base64 += '=';
  return new Uint8Array(Buffer.from(base64, 'base64'));
}

function readHead(bytes, state) {
  const initial = bytes[state.offset];
  state.offset += 1;
  const major = initial >> 5;
  const additional = initial & 0x1f;
  let length;
  if (additional < 24) {
    length = additional;
  } else if (additional === 24) {
    length = bytes[state.offset];
    state.offset += 1;
  } else if (additional === 25) {
    length = new DataView(
      bytes.buffer,
      bytes.byteOffset + state.offset,
      2,
    ).getUint16(0, false);
    state.offset += 2;
  } else {
    throw new Error(`unsupported test CBOR additional value ${additional}`);
  }
  return { major, length };
}

function decodeCbor(bytes, state = { offset: 0 }) {
  const { major, length } = readHead(bytes, state);
  if (major === 0) return length;
  if (major === 1) return -1 - length;
  if (major === 2) {
    const value = bytes.slice(state.offset, state.offset + length);
    state.offset += length;
    return value;
  }
  if (major === 3) {
    const value = new TextDecoder().decode(
      bytes.slice(state.offset, state.offset + length),
    );
    state.offset += length;
    return value;
  }
  if (major === 5) {
    const value = new Map();
    for (let index = 0; index < length; index += 1) {
      value.set(decodeCbor(bytes, state), decodeCbor(bytes, state));
    }
    return value;
  }
  throw new Error(`unsupported test CBOR major type ${major}`);
}

const authenticator = new VirtualAuthenticator();
const rpId = 'localhost';
const origin = 'https://localhost:8080';
const credentialId = new Uint8Array(32).fill(0x5a);
const registration = await authenticator.createCredential({
  rpId,
  origin,
  challenge: 'registration-challenge',
  credentialId,
  prf: true,
});

assert.equal(registration.id, registration.rawId);
assert.equal(registration.type, 'public-key');
assert.deepEqual(registration.clientExtensionResults, {
  prf: { enabled: true },
});
const registrationClientData = JSON.parse(new TextDecoder().decode(
  b64uToBytes(registration.response.clientDataJSON),
));
assert.equal(registrationClientData.type, 'webauthn.create');
assert.equal(registrationClientData.challenge, 'registration-challenge');
assert.equal(registrationClientData.origin, origin);

const attestation = decodeCbor(
  b64uToBytes(registration.response.attestationObject),
);
assert.equal(attestation.get('fmt'), 'none');
assert.equal(attestation.get('attStmt').size, 0);
const registrationAuthData = attestation.get('authData');
assert.equal(registrationAuthData[32], 0x45);
assert.equal(
  new DataView(
    registrationAuthData.buffer,
    registrationAuthData.byteOffset + 33,
    4,
  ).getUint32(0, false),
  0,
);

const saltOne = new Uint8Array(32).fill(0x11);
const saltTwo = new Uint8Array(32).fill(0x22);
const first = await authenticator.getAssertion({
  rpId,
  origin,
  challenge: 'assertion-one',
  credentialId: registration.rawId,
  prf: { first: saltOne },
});
const second = await authenticator.getAssertion({
  rpId,
  origin,
  challenge: 'assertion-two',
  credentialId: registration.rawId,
  prf: { first: saltOne },
});
const different = await authenticator.getAssertion({
  rpId,
  origin,
  challenge: 'assertion-three',
  credentialId: registration.rawId,
  prf: { first: saltTwo },
});

assert.equal(
  first.clientExtensionResults.prf.results.first,
  second.clientExtensionResults.prf.results.first,
);
assert.notEqual(
  first.clientExtensionResults.prf.results.first,
  different.clientExtensionResults.prf.results.first,
);
for (const [index, assertion] of [first, second, different].entries()) {
  const clientData = JSON.parse(new TextDecoder().decode(
    b64uToBytes(assertion.response.clientDataJSON),
  ));
  assert.equal(clientData.type, 'webauthn.get');
  const authData = b64uToBytes(assertion.response.authenticatorData);
  assert.equal(authData[32], 0x05);
  assert.equal(
    new DataView(
      authData.buffer,
      authData.byteOffset + 33,
      4,
    ).getUint32(0, false),
    index + 1,
  );
  assert.equal(b64uToBytes(assertion.response.signature)[0], 0x30);
}

process.stdout.write('authenticator-node tests passed\n');
