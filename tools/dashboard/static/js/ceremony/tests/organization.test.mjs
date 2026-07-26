import { webcrypto } from 'node:crypto';

if (!globalThis.crypto) {
  globalThis.crypto = webcrypto;
}

const {
  armorSeed,
  buildRecoveryBlock,
  buildRegistrationEnvelope,
  generateOrgRootKey,
  importEd25519RootSigningKey,
  setArmorIterationsForTest,
} = await import('../organization.js');

setArmorIterationsForTest(10000);
const passphrase = 'organization module vector passphrase';
const pair = await generateOrgRootKey();
const seedHex = Array.from(
  pair.seed,
  (byte) => (`0${byte.toString(16)}`).slice(-2),
).join('');
try {
  const armor = await armorSeed(pair.seed, pair.pubHex, passphrase);
  const signingKey = await importEd25519RootSigningKey(pair.seed);
  const payload = {
    root_pub: pair.pubHex,
    recovery_policy: 'none',
  };
  const envelope = await buildRegistrationEnvelope(
    signingKey,
    pair.pubHex,
    payload,
    1800000000,
  );
  const recovery = await generateOrgRootKey();
  let recoverySeedHex = null;
  try {
    recoverySeedHex = Array.from(
      recovery.seed,
      (byte) => (`0${byte.toString(16)}`).slice(-2),
    ).join('');
    const recoveryBlock = buildRecoveryBlock(
      '11111111-1111-4111-8111-111111111111',
      pair.pubHex,
      recovery.pubHex,
      recoverySeedHex,
    );
    process.stdout.write(JSON.stringify({
      passphrase,
      seedHex,
      rootPub: pair.pubHex,
      armor,
      envelope,
      recoveryPub: recovery.pubHex,
      recoverySeedHex,
      recoveryBlock,
    }));
  } finally {
    recovery.seed.fill(0);
    recovery.seed = null;
    recoverySeedHex = null;
  }
} finally {
  pair.seed.fill(0);
  pair.seed = null;
}
