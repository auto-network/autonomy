/* Real-crypto regression guard for the root-seed leak the injected-fake tests
 * structurally could not catch (join security review, finding 1 / HIGH).
 *
 * derivePersona copies the personal root seed and hands that copy to
 * importKey('raw', …) for the HKDF. We spy on importKey to keep a reference to
 * every raw key buffer, run the REAL derivePersona with a sentinel root seed,
 * and assert no buffer still holds the sentinel afterwards — i.e. the internal
 * root-seed copy was zeroed. Before the fix this fails (the copy stays 0xA5);
 * after it, it passes. No fakes: this exercises the primitive under the seam.
 * Run: node seed-zeroization.test.mjs
 */
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';

if (!globalThis.crypto) {
  globalThis.crypto = webcrypto;
}

const SENTINEL = 0xa5;
const GENESIS = 'a'.repeat(64);

// Spy on importKey, keeping a live reference to every raw key buffer passed in.
const subtle = globalThis.crypto.subtle;
const realImportKey = subtle.importKey.bind(subtle);
const rawKeyBuffers = [];
subtle.importKey = function importKeySpy(format, keyData, ...rest) {
  if (format === 'raw' && keyData instanceof Uint8Array) {
    rawKeyBuffers.push(keyData);
  }
  return realImportKey(format, keyData, ...rest);
};

let derivePersona;
try {
  ({ derivePersona } = await import('../../ceremony/ledger-event.js'));

  const callerSeed = new Uint8Array(32).fill(SENTINEL);
  const persona = await derivePersona(callerSeed, GENESIS);
  assert.ok(persona && persona.publicHex, 'derivePersona returned a persona');

  // No raw key buffer may still hold the sentinel: that would be a live copy of
  // the personal ROOT seed left on the heap after a successful derivation.
  const leaked = rawKeyBuffers.find(
    (buf) => buf.length === 32 && buf.every((b) => b === SENTINEL),
  );
  assert.equal(
    leaked,
    undefined,
    'a copy of the personal root seed survived derivePersona (finding 1)',
  );

  // Positive: the root-seed copy WAS imported (so the spy saw it) and is now
  // zeroed — proving the zeroization ran, not that the seed was never used.
  const zeroed = rawKeyBuffers.some(
    (buf) => buf.length === 32 && buf.every((b) => b === 0),
  );
  assert.equal(zeroed, true, 'the imported root-seed copy must be zeroed after use');
} finally {
  subtle.importKey = realImportKey;
}

console.log('seed-zeroization: all assertions passed');
