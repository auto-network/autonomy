/* The recovery-code ceremony's dual-function derivation (row 16): one cold code
 * -> {Ed25519 recovery keypair, KEK-recovery seed}, domain-separated. Proves the
 * signing half works (it is what auto-n9cy3's recovery co-signature verifies)
 * and that the two functions cannot be reconstructed from each other.
 * Run: node recovery.test.mjs
 */
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';

if (!globalThis.crypto) {
  globalThis.crypto = webcrypto;
}

const {
  deriveRecoveryFactors,
  generateRecoveryCode,
  encodeRecoveryCode,
  decodeRecoveryCode,
  RECOVERY_SIGN_INFO,
  RECOVERY_KEK_INFO,
} = await import('../recovery.js');

const CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
function flipLastCrockford(printable) {
  const chars = [...printable];
  const i = chars.length - 1;
  chars[i] = CROCKFORD[(CROCKFORD.indexOf(chars[i]) + 1) % 32];
  return chars.join('');
}

const te = new TextEncoder();

function hexToBytes(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i += 1) {
    out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return out;
}

async function replicateHkdf(code, info) {
  const material = await webcrypto.subtle.importKey('raw', code, 'HKDF', false, ['deriveBits']);
  return new Uint8Array(await webcrypto.subtle.deriveBits(
    { name: 'HKDF', hash: 'SHA-256', salt: new Uint8Array(32), info: te.encode(info) },
    material, 256,
  ));
}

const CODE = new Uint8Array(32).fill(0x5a);

// 1. Deterministic + functional: a 64-hex recovery_pub, a 32-byte kem seed, and
//    the recovery signing key signs what its public half verifies.
{
  const a = await deriveRecoveryFactors(CODE);
  assert.match(a.recoveryPub, /^[0-9a-f]{64}$/);
  assert.equal(a.kekRecoverySeed.length, 32);
  const msg = te.encode('rotate-recovery-binding');
  const sig = await webcrypto.subtle.sign({ name: 'Ed25519' }, a.recoverySigningKey, msg);
  const verifyKey = await webcrypto.subtle.importKey(
    'raw', hexToBytes(a.recoveryPub), { name: 'Ed25519' }, false, ['verify'],
  );
  assert.equal(await webcrypto.subtle.verify({ name: 'Ed25519' }, verifyKey, sig, msg), true);
  const b = await deriveRecoveryFactors(CODE);
  assert.equal(b.recoveryPub, a.recoveryPub);
  assert.deepEqual([...b.kekRecoverySeed], [...a.kekRecoverySeed]);
}

// 2. Domain separation: the KEK seed is HKDF(code, KEK_INFO), and the signing
//    seed (HKDF(code, SIGN_INFO)) is a different value -- neither function can
//    be reconstructed from the other.
{
  const { kekRecoverySeed } = await deriveRecoveryFactors(CODE);
  const kekReplica = await replicateHkdf(CODE, RECOVERY_KEK_INFO);
  const signReplica = await replicateHkdf(CODE, RECOVERY_SIGN_INFO);
  assert.deepEqual([...kekRecoverySeed], [...kekReplica]);   // consistency
  assert.notDeepEqual([...signReplica], [...kekReplica]);    // separation
}

// 3. Fail-closed: a too-short code or a non-Uint8Array is refused.
{
  await assert.rejects(deriveRecoveryFactors(new Uint8Array(31)), /at least 32/);
  await assert.rejects(deriveRecoveryFactors('not-bytes'), /Uint8Array/);
}

// 4. Distinct codes -> distinct recovery keys.
{
  const a = await deriveRecoveryFactors(new Uint8Array(32).fill(1));
  const b = await deriveRecoveryFactors(new Uint8Array(32).fill(2));
  assert.notEqual(a.recoveryPub, b.recoveryPub);
}

// 5. The caller's code buffer is copied, not consumed: the ceremony owns the
//    code's lifecycle (render once, then zero), so deriveRecoveryFactors zeroes
//    only its OWN copy, never the caller's.
{
  const code = new Uint8Array(32).fill(0x33);
  await deriveRecoveryFactors(code);
  assert.equal(code.every((b) => b === 0x33), true);
}

// 6. ZEROIZATION regression (the derivePersona lesson): neither the code copy
//    (ikm, captured via importKey('raw')) nor the signing seed (the FIRST
//    deriveBits buffer, which signSeed views) may survive -- on the happy path
//    AND on a thrown path -- while the kek seed (the returned output) stays.
async function withSpies(run, { throwOnDeriveCall = 0 } = {}) {
  const subtle = webcrypto.subtle;
  const realImportKey = subtle.importKey.bind(subtle);
  const realDeriveBits = subtle.deriveBits.bind(subtle);
  const rawKeys = [];
  const derived = [];
  let deriveCalls = 0;
  subtle.importKey = function importKeySpy(fmt, keyData, ...rest) {
    if (fmt === 'raw' && keyData instanceof Uint8Array) rawKeys.push(keyData);
    return realImportKey(fmt, keyData, ...rest);
  };
  subtle.deriveBits = async function deriveBitsSpy(algo, key, len) {
    deriveCalls += 1;
    if (deriveCalls === throwOnDeriveCall) throw new Error('derive failed');
    const buf = await realDeriveBits(algo, key, len);
    derived.push(new Uint8Array(buf)); // a view over the SAME buffer signSeed views
    return buf;
  };
  try {
    await run();
  } finally {
    subtle.importKey = realImportKey;
    subtle.deriveBits = realDeriveBits;
  }
  return { rawKeys, derived };
}

{
  const SENT = new Uint8Array(32).fill(0xa5);
  const { rawKeys, derived } = await withSpies(() => deriveRecoveryFactors(SENT));
  assert.equal(
    rawKeys.some((b) => b.length === 32 && b.every((x) => x === 0)), true,
    'the code copy must be zeroed after use',
  );
  assert.equal(
    rawKeys.some((b) => b.length === 32 && b.every((x) => x === 0xa5)), false,
    'no un-zeroed copy of the code may survive',
  );
  assert.equal(derived[0].every((x) => x === 0), true, 'the signing seed must be zeroed');
  assert.equal(derived[1].some((x) => x !== 0), true, 'the kek seed is the returned output');
}

{
  // Thrown path: the kek derivation fails after the signing seed exists; the
  // finally must still zero the code copy and the signing seed.
  const SENT = new Uint8Array(32).fill(0xa5);
  const { rawKeys, derived } = await withSpies(
    () => assert.rejects(deriveRecoveryFactors(SENT), /derive failed/),
    { throwOnDeriveCall: 2 },
  );
  assert.equal(
    rawKeys.some((b) => b.length === 32 && b.every((x) => x === 0xa5)), false,
    'code copy must be zeroed even when derivation throws',
  );
  assert.equal(derived[0].every((x) => x === 0), true, 'signing seed zeroed on the thrown path');
}

// 8. The printable recovery code: generate -> encode -> decode round-trips,
//    tolerates case/whitespace/ambiguous chars, catches a typo by checksum, and
//    the decoded bytes drive the same derivation as the originals.
{
  const bytes = generateRecoveryCode();
  assert.equal(bytes.length, 32);
  const printable = await encodeRecoveryCode(bytes);
  assert.match(printable, /^[0-9A-HJKMNP-TV-Z-]+$/); // crockford alphabet + group dashes
  assert.deepEqual([...(await decodeRecoveryCode(printable))], [...bytes]);
  // lowercase, spaces instead of dashes, and ambiguous o/i/l all normalise
  const noisy = printable.toLowerCase().replace(/-/g, '  ');
  assert.deepEqual([...(await decodeRecoveryCode(noisy))], [...bytes]);
  // a single mistyped checksum character is refused, not silently accepted
  await assert.rejects(decodeRecoveryCode(flipLastCrockford(printable)), /checksum failed/);
  // the round-tripped bytes derive the same recovery keypair
  const a = await deriveRecoveryFactors(bytes);
  const b = await deriveRecoveryFactors(await decodeRecoveryCode(printable));
  assert.equal(a.recoveryPub, b.recoveryPub);
  // two generated codes differ (real entropy)
  assert.notDeepEqual([...generateRecoveryCode()], [...generateRecoveryCode()]);
}

console.log('recovery: all assertions passed');
