/* Passkey enrollment statement — the browser half of
 * tools/network/idkit/enrollment.py.
 *
 * The root's signed claim about one passkey credential. Built and signed in the
 * browser at the enrollment ceremony (the only place the root lives), verified
 * server-side against the root resolved from autonomy.identity.personal — never
 * from the statement itself. The binding and signature are byte-identical to the
 * Python mint(), guarded by test_enrollment_statement_crossimpl: a statement the
 * browser mints that the Python verifier cannot open is a passkey no operator
 * could ever promote.
 *
 * provisioning_public_key / label / transports / aaguid are OMITTED when absent,
 * not carried as null — absence is part of what the root commits to, so a row
 * that later grows one of these no longer matches the signature.
 */
import { canonicalJson, bytesToHex } from './primitives.js';
import { deriveEncapsulationKeypair } from './sealing.js';

let webCrypto = globalThis.crypto;
if (!webCrypto && typeof process !== 'undefined' && process.versions?.node) {
  ({ webcrypto: webCrypto } = await import('node:crypto'));
}
if (!webCrypto?.subtle) {
  throw new Error('passkey enrollment requires WebCrypto');
}
const textEncoder = new TextEncoder();

// Distinct from every other signed record, so a statement cannot be replayed as
// one. Mirrors idkit.enrollment.ENROLLMENT_DOMAIN byte-for-byte.
const ENROLLMENT_DOMAIN = 'autonomy.identity.passkey-enrollment.v1\n';
const ENROLLMENT_VERSION = 1;

// The provisioning key is derived under the vault factor's purpose (auto-oox5r:
// one key, one label) — so the statement's provisioning_public_key IS the vault
// factor's public key, derived byte-identically to Python's
// derive_encapsulation_keypair (guarded by the cross-impl vector).
const VAULT_FACTOR_PURPOSE = 'autonomy/vault-factor/v1';

// The WebAuthn PRF eval salt. Fixed and domain-separated: the SAME salt at
// enrollment (to publish the public half) and at every open (to re-derive the
// private half), so a promoted passkey always reproduces the same key. One salt
// today; a second, distinct salt would split root-access from vault-access if
// that separation is ever chosen.
const VAULT_FACTOR_PRF_SALT = textEncoder.encode('autonomy/vault-factor/prf/v1');

/** The `extensions` object requesting the PRF eval — identical at create() and
 *  get(), so both paths produce the same output. */
function prfEvalExtension() {
  return { prf: { eval: { first: VAULT_FACTOR_PRF_SALT } } };
}

function base64UrlToBytes(value) {
  let base64 = value.replace(/-/g, '+').replace(/_/g, '/');
  while (base64.length % 4) base64 += '=';
  const binary = atob(base64);
  const output = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    output[index] = binary.charCodeAt(index);
  }
  return output;
}

/** The 32-byte PRF output from a create()/get() client-extension result, or
 *  null when the authenticator only reported support without evaluating (the
 *  case the get() fallback exists for). Real WebAuthn returns an ArrayBuffer;
 *  the simulator (and any serialized form) returns base64url — accept either. */
function prfOutputFromResults(clientExtensionResults) {
  const first = clientExtensionResults && clientExtensionResults.prf
    && clientExtensionResults.prf.results
    && clientExtensionResults.prf.results.first;
  if (!first) return null;
  return typeof first === 'string' ? base64UrlToBytes(first) : new Uint8Array(first);
}

/** Turn a passkey's 32-byte PRF output into the provisioning keypair. The
 *  public half goes in the statement; the private half is never stored — it is
 *  re-derived from a fresh PRF eval every time the passkey is used. */
async function deriveProvisioningKey(prfOutput) {
  return deriveEncapsulationKeypair(new Uint8Array(prfOutput), VAULT_FACTOR_PURPOSE);
}

/** Obtain the passkey's PRF output, trying create() first and falling back to
 *  one get() assertion when the authenticator supports PRF but did not evaluate
 *  it at registration.
 *
 *  `createResults` is the create()'s `getClientExtensionResults()`; `getFn` is
 *  an async that performs the fallback assertion (a real
 *  `navigator.credentials.get` in the browser, the simulator in tests) and
 *  returns ITS client-extension results. Returns the 32-byte output, or null
 *  when the credential has no PRF at all — an access-only passkey, whose
 *  statement omits the provisioning key and costs one gesture, not two. */
async function evaluatePrf(createResults, getFn) {
  const atCreate = prfOutputFromResults(createResults);
  if (atCreate) return atCreate;
  const supported = createResults && createResults.prf && createResults.prf.enabled;
  if (!supported) return null;
  return prfOutputFromResults(await getFn());
}

/** The COSE public key and enrollment sign-count for the signed binding, sliced
 *  from the WebAuthn authenticatorData (rpIdHash[32] ‖ flags[1] ‖ signCount[4] ‖
 *  aaguid[16] ‖ credIdLen[2] ‖ credId ‖ COSE-key). The key is sent as the
 *  authenticator emitted it; the server canonicalizes with py_webauthn for the
 *  cross-check, so no CBOR encoder is needed here. (A normal registration has no
 *  authData extensions, so the key is the tail; if one ever appended extensions,
 *  the server's parse_cbor still reads exactly the key and the cross-check holds.) */
function attestedCredential(authData) {
  const d = new Uint8Array(authData);
  const signCount = ((d[33] << 24) | (d[34] << 16) | (d[35] << 8) | d[36]) >>> 0;
  const credIdLen = (d[53] << 8) | d[54];
  const cose = d.slice(55 + credIdLen);
  return { credentialPublicKeyHex: bytesToHex(cose), signCount };
}

/** Everything the signature covers, in the exact shape Python's binding_dict
 *  produces (canonicalJson sorts keys, so declaration order here is cosmetic). */
function bindingDict({
  credentialId,
  credentialPublicKey,
  rpId,
  origin,
  nonce,
  createdHlc,
  signer,
  initialSignCount,
  provisioningPublicKey = null,
  label = null,
  transports = [],
  aaguid = null,
}) {
  const payload = {
    version: ENROLLMENT_VERSION,
    credential_id: credentialId,
    credential_public_key: credentialPublicKey,
    rp_id: rpId,
    origin,
    nonce,
    created_hlc: [createdHlc[0], createdHlc[1]],
    signer,
  };
  payload.initial_sign_count = initialSignCount;
  if (provisioningPublicKey != null) {
    payload.provisioning_public_key = provisioningPublicKey;
  }
  if (label != null) payload.label = label;
  if (transports && transports.length) payload.transports = [...transports];
  if (aaguid != null) payload.aaguid = aaguid;
  return payload;
}

/** Mirror of enrollment.mint: sign the binding with the personal root Ed25519
 *  key and return the full statement (binding + signature). `signer` in
 *  `fields` MUST be the public hex of `rootSigningKey`; the server refuses a
 *  statement whose signature does not verify against the root it trusts. */
async function mintEnrollmentStatement(fields, rootSigningKey) {
  const binding = bindingDict(fields);
  const input = textEncoder.encode(ENROLLMENT_DOMAIN + canonicalJson(binding));
  const signature = bytesToHex(
    await webCrypto.subtle.sign('Ed25519', rootSigningKey, input),
  );
  return { ...binding, signature };
}

function bytesToB64u(bytes) {
  let binary = '';
  const view = new Uint8Array(bytes);
  for (let i = 0; i < view.length; i += 1) binary += String.fromCharCode(view[i]);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** The whole one-call promotable-passkey enrollment, in the browser. Assembles
 *  the proven pieces above; the WebAuthn calls (create/get) are the only part
 *  that needs a real authenticator (Face ID). Returns the server's register
 *  response.
 *
 *  `root` is `{ signingKey: CryptoKey, publicHex }` — the personal root, held
 *  only for this ceremony and zeroed by the caller after. `credentials`
 *  defaults to `navigator.credentials`; `fetchImpl` to same-origin `fetch`. */
async function enrollPasskey({
  root,
  label = 'This device',
  credentials = (typeof navigator !== 'undefined' ? navigator.credentials : null),
  fetchImpl = (typeof fetch !== 'undefined' ? fetch : null),
  now = Date.now(),
}) {
  const post = (path, body) => fetchImpl(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify(body || {}),
  });

  const minted = await (await post('/api/identity/passkey/register-options', {})).json();
  if (!minted.ok) throw new Error(`register-options failed: ${minted.error || '?'}`);
  const pk = minted.options;
  pk.challenge = base64UrlToBytes(pk.challenge);
  pk.user.id = base64UrlToBytes(pk.user.id);
  (pk.excludeCredentials || []).forEach((c) => { c.id = base64UrlToBytes(c.id); });
  pk.extensions = prfEvalExtension();

  const cred = await credentials.create({ publicKey: pk });
  if (!cred) throw new Error('enrollment was cancelled');

  const prfOutput = await evaluatePrf(cred.getClientExtensionResults(), async () => {
    const asrt = await credentials.get({
      publicKey: {
        // Throwaway: the PRF output is a function of the salt and the
        // credential, not the challenge, and this assertion is never sent to
        // the server — we only read its PRF result.
        challenge: webCrypto.getRandomValues(new Uint8Array(32)),
        rpId: minted.rp_id,
        allowCredentials: [{ type: 'public-key', id: cred.rawId }],
        userVerification: 'required',
        extensions: prfEvalExtension(),
      },
    });
    return asrt.getClientExtensionResults();
  });

  const { credentialPublicKeyHex, signCount } =
    attestedCredential(cred.response.getAuthenticatorData());

  let provisioningPublicKey = null;
  if (prfOutput) {
    provisioningPublicKey = (await deriveProvisioningKey(prfOutput)).publicKeyHex;
  }

  const transports = (cred.response.getTransports && cred.response.getTransports()) || [];
  const statement = await mintEnrollmentStatement({
    credentialId: bytesToB64u(cred.rawId),
    credentialPublicKey: credentialPublicKeyHex,
    rpId: minted.rp_id,
    origin: minted.origin,
    nonce: minted.nonce,
    createdHlc: [now, 0],
    signer: root.publicHex,
    initialSignCount: signCount,
    provisioningPublicKey,
    label,
    transports,
  }, root.signingKey);

  const result = await (await post('/api/identity/passkey/register', {
    label,
    credential: {
      id: cred.id,
      rawId: bytesToB64u(cred.rawId),
      type: cred.type,
      authenticatorAttachment: cred.authenticatorAttachment || undefined,
      clientExtensionResults:
        (cred.getClientExtensionResults && cred.getClientExtensionResults()) || {},
      response: {
        clientDataJSON: bytesToB64u(cred.response.clientDataJSON),
        attestationObject: bytesToB64u(cred.response.attestationObject),
        transports,
      },
    },
    statement,
  })).json();
  if (!result.ok) throw new Error(`register failed: ${result.error || '?'}`);
  return result;
}

/** The first-device detection decision, pure: given the factor list (view or
 *  envelope — both carry credential_id + recipients), the credential that just
 *  signed in, and the recipient key derived from ITS live PRF, classify:
 *  'enrolled' (this device holds a slot), 'pending-slot' (a KNOWN credential
 *  with no slot here — the first-device re-enrollment flow's trigger, with
 *  the stash payload ready), or 'unknown-credential'. Deliberately
 *  independent of any existing root authority: the flow exists precisely to
 *  RESTORE authority a migrated or newly synced device does not hold yet. */
function detectPendingSlot(factors, credentialIdB64u, recipientPublicKeyHex) {
  const known = (factors || []).find(
    (f) => f.type === 'passkey' && f.credential_id === credentialIdB64u,
  );
  if (!known) return { kind: 'unknown-credential' };
  const enrolled = (known.recipients || []).some(
    (s) => s.recipient_public_key === recipientPublicKeyHex,
  );
  if (enrolled) return { kind: 'enrolled', factor: known };
  return {
    kind: 'pending-slot',
    factor: known,
    pending: {
      factor_id: known.factor_id,
      credential_id: credentialIdB64u,
      recipient_public_key: recipientPublicKeyHex,
      label: 'New device',
    },
  };
}

export {
  detectPendingSlot,
  ENROLLMENT_DOMAIN,
  ENROLLMENT_VERSION,
  VAULT_FACTOR_PURPOSE,
  VAULT_FACTOR_PRF_SALT,
  bindingDict,
  mintEnrollmentStatement,
  prfEvalExtension,
  prfOutputFromResults,
  deriveProvisioningKey,
  evaluatePrf,
  attestedCredential,
  enrollPasskey,
};
