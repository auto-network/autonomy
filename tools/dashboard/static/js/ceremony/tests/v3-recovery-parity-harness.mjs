/*
 * v3 recovery-slot cross-language harness. Reads a JSON request on stdin.
 *
 *   {mode:"build", rootSeedHex, codeHex}
 *     → build a v3 armor (one password factor + a recovery slot sealed to the
 *       code) and print {armor, rootPub}. Python then opens it with the code.
 *   {mode:"open", armor, codeHex}
 *     → open the root with the code and print {rootPub, privateHex}. Proves a
 *       Python-built recovery slot opens in the browser.
 *
 * A byte disagreement on the v3 slot format or the RECOVERY_ARMOR seal means a
 * code enrolled in one place cannot recover in the other — asserted, not assumed.
 */
import {
  buildFactorPolicyArmor,
  addRecoverySlot,
  openRootWithRecovery,
  recoveryRecipientPublicKey,
  createPasswordFactor,
} from '../root-factor-policy.js';
import { deriveRecoveryFactors } from '../recovery.js';
import { bytesToHex } from '../primitives.js';

function readStdin() {
  return new Promise((resolve) => {
    let data = '';
    process.stdin.on('data', (chunk) => { data += chunk; });
    process.stdin.on('end', () => resolve(data));
  });
}

const req = JSON.parse(await readStdin());
const code = Uint8Array.from(req.codeHex.match(/.{2}/g).map((b) => parseInt(b, 16)));

if (req.mode === 'build') {
  const rootSeed = Uint8Array.from(req.rootSeedHex.match(/.{2}/g).map((b) => parseInt(b, 16)));
  const rootPub = req.rootPub;
  const { factor, seed } = await createPasswordFactor(rootPub, 'pw.main', 'harness-pass-pass');
  seed.fill(0);
  let armor = await buildFactorPolicyArmor({
    rootSeed, rootPub, generation: 1, factors: [factor],
    access: ['pw.main'], policy: { op: 'factor', factor_id: 'pw.main' },
  });
  const recipient = await recoveryRecipientPublicKey(code);
  const { recoveryPub } = await deriveRecoveryFactors(code);
  armor = await addRecoverySlot(armor, {
    rootSeed, recoveryRecipientPub: recipient, recoveryPub,
  });
  process.stdout.write(JSON.stringify({ armor, rootPub }));
} else if (req.mode === 'open') {
  const opened = await openRootWithRecovery(req.armor, code);
  process.stdout.write(JSON.stringify({
    rootPub: opened.rootPub,
    privateHex: bytesToHex(opened.seed),
  }));
  opened.seed.fill(0);
} else {
  throw new Error('unknown mode');
}
