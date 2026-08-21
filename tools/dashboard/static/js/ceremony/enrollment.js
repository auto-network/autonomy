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
  bindingDict,
  mintEnrollmentStatement,
};
