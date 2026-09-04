/* Open a vault policy class in the browser — the B-1 content-key open.
 *
 * Byte-parity port of tools/vault/policy_class.py::open_cek for the single-wrap
 * policies (password / prf / root-reachable): the operator's browser opens the
 * class locally and yields ONLY this one revision's content key (CEK), which
 * decrypts nothing else. The class key IS reconstructed here — it is
 * browser-transient by construction (unwrapped from the served generation to
 * open the one sealed_cek, then zeroed) — but neither it nor the opener seeds
 * ever leave the browser; only the single-revision CEK crosses to the server.
 * The "both" (2-of-2) policy is out of phase-1 scope and refused.
 *
 * Composition (mirrors the Python open_cek exactly):
 *   1. factor key  = deriveEncapsulationKeypair(seed, VAULT_FACTOR_PURPOSE)
 *   2. class_key   = seal_open(wrap.wrapped, factor_priv, wrapPurpose)
 *   3. sealing key = deriveEncapsulationKeypair(class_key, classSealKeyPurpose)
 *   4. CEK         = seal_open(sealed_cek.ciphertext, sealing_priv, cekPurpose)
 * The purpose strings and their canonical-JSON/SHA-256 digests must match the
 * Python constructions byte-for-byte; the parity vector test guards that.
 */
import {
  deriveEncapsulationKeypair,
  openWithEncapsulationPrivateKey,
} from './sealing.js';
import { canonicalJson, hexToBytes, bytesToHex } from './primitives.js';

async function sha256Hex(bytes) {
  return bytesToHex(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)));
}

const VAULT_FACTOR_PURPOSE = 'autonomy/vault-factor/v1';
const CLASS_WRAP_PURPOSE = 'autonomy/vault-policy-class/v1';
const CLASS_SEAL_KEY_PURPOSE = 'autonomy/vault-policy-class/sealing-key/v1';
const CEK_PUBLIC_SEAL_PURPOSE = 'autonomy/vault-policy-class/cek-hpke/v1';
const CEK_PUBLIC_SEAL_FORMAT = 'hpke-x25519-v1';
const CLASS_KEY_LEN = 32;

const _encoder = new TextEncoder();

function wrapPurpose(classId, genId, policy, role) {
  return `${CLASS_WRAP_PURPOSE}|${classId}|${genId}|${policy}|${role}`;
}

async function classSealKeyPurpose(classId, genId) {
  const digest = await sha256Hex(
    _encoder.encode(canonicalJson({ class_id: classId, gen_id: genId })),
  );
  return `${CLASS_SEAL_KEY_PURPOSE}|${digest}`;
}

async function cekPublicSealPurpose(classId, genId, genesisId, settingName, policy) {
  const digest = await sha256Hex(
    _encoder.encode(canonicalJson({
      format: CEK_PUBLIC_SEAL_FORMAT,
      genesis_id: genesisId,
      class_id: classId,
      gen_id: genId,
      setting_name: settingName,
      policy,
    })),
  );
  return `${CEK_PUBLIC_SEAL_PURPOSE}|${digest}`;
}

async function openWrap(wrap, seedHex, classId, genId, policy) {
  const { privateKeyHex } = await deriveEncapsulationKeypair(
    hexToBytes(seedHex), VAULT_FACTOR_PURPOSE,
  );
  return openWithEncapsulationPrivateKey(
    hexToBytes(wrap.wrapped),
    privateKeyHex,
    wrapPurpose(classId, genId, policy, wrap.role),
  );
}

/**
 * Open the sealed content key with the operator's factor openers, entirely in
 * the browser. Returns the CEK as lowercase hex.
 *
 * `bundle` is the server's inner-blob payload (never crossing back a seed):
 *   { class_id, policy, genesis_id, setting_name,
 *     generation: { gen_id, sealing_public_key, wraps: [{factor_id, role, wrapped}] },
 *     sealed_cek: { format, gen_id, ciphertext } }
 * `openers` is { factor_id: seedHex } — the same seeds gathered from the factor
 * ceremony, consumed here instead of shipped to the server.
 */
async function openContentKey(bundle, openers) {
  const { class_id: classId, policy, genesis_id: genesisId,
    setting_name: settingName, generation, sealed_cek: sealedCek } = bundle;
  if (policy === 'both') {
    throw new Error('two-of-two policy open is not supported in the browser yet');
  }
  const genId = generation.gen_id;

  let classKey = null;
  for (const wrap of generation.wraps || []) {
    const seedHex = openers[wrap.factor_id];
    if (!seedHex) continue;
    try {
      const key = await openWrap(wrap, seedHex, classId, genId, policy);
      if (key && key.length === CLASS_KEY_LEN) { classKey = key; break; }
    } catch { /* try the next wrap */ }
  }
  if (!classKey) throw new Error('no supplied factor opens this generation');

  try {
    const { privateKeyHex: sealingPriv, publicKeyHex: sealingPub } =
      await deriveEncapsulationKeypair(classKey, await classSealKeyPurpose(classId, genId));
    if (generation.sealing_public_key && sealingPub !== generation.sealing_public_key) {
      throw new Error('generation sealing public key does not match its factor-wrapped key');
    }
    if (sealedCek.format !== CEK_PUBLIC_SEAL_FORMAT) {
      throw new Error(`unsupported sealed_cek format: ${sealedCek.format}`);
    }
    const cek = await openWithEncapsulationPrivateKey(
      hexToBytes(sealedCek.ciphertext),
      sealingPriv,
      await cekPublicSealPurpose(classId, genId, genesisId, settingName, policy),
    );
    if (!cek || cek.length !== CLASS_KEY_LEN) {
      throw new Error('opened content key is not 32 bytes');
    }
    return bytesToHex(cek);
  } finally {
    if (classKey && classKey.fill) classKey.fill(0);
  }
}

export { openContentKey };
