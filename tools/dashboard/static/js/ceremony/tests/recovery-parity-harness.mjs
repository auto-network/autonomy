/*
 * Emit what the browser derives from a recovery code, for Python to match.
 *
 * A code is printed by whichever side ran the ceremony and later typed into
 * whichever side is doing the recovering. If the two disagree by one byte, a
 * person holding a correct code cannot get back in -- the one failure a
 * recovery mechanism may never have. So the agreement is asserted, not assumed.
 *
 * usage: node recovery-parity-harness.mjs CODE_HEX
 */
import {
  deriveRecoveryFactors,
  encodeRecoveryCode,
  decodeRecoveryCode,
} from '../recovery.js';
import { bytesToHex } from '../primitives.js';

const codeHex = process.argv[2];
const code = Uint8Array.from(codeHex.match(/.{2}/g).map((b) => parseInt(b, 16)));

const factors = await deriveRecoveryFactors(new Uint8Array(code));
const printable = await encodeRecoveryCode(code);
// The printed form must also read back here, so the round trip is covered on
// both sides rather than only in whichever one produced it.
const readBack = bytesToHex(await decodeRecoveryCode(printable));

process.stdout.write(JSON.stringify({
  recovery_pub: factors.recoveryPub,
  kek_recovery_seed: bytesToHex(factors.kekRecoverySeed),
  printable,
  read_back: readBack,
}));
