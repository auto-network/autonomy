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

export {
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
};
