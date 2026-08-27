/* The one-shot v2→v3 migration payload (see unlock.js _migrateArmorV2Once —
 * DELETE together with it after the operator's migration runs).
 *
 * Extracted so the cross-implementation test can drive the REAL builder
 * against the REAL server validation instead of a test-local copy.
 *
 * Builds the single migrate_legacy operation from the live factor-policy
 * view and the login's material: the typed password re-derives its v3 factor
 * under its legacy id; the passkey used to sign in (if any) gets THIS
 * device's freshly derived root recipient; other passkeys carry over with
 * empty device slots. Returns null when nothing root-capable is in hand.
 */
import { createPasswordFactor } from './root-factor-policy.js';

export async function buildMigrationOperations(view, material) {
  const factors = [];
  const access = [];
  const leaves = [];
  const nowIso = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
  for (const f of (view.factors || [])) {
    if (f.type === 'password') {
      if (!material.password) continue;   // cannot re-derive without the password
      const made = await createPasswordFactor(view.root_pub, f.factor_id, material.password);
      made.seed.fill(0);
      factors.push(made.factor);
      access.push(f.factor_id);
      leaves.push({ op: 'factor', factor_id: f.factor_id });
    } else if (f.type === 'passkey') {
      // An armor wrap whose credential has NO dashboard registration row is
      // dead weight: it cannot sign in today, and the server's binding check
      // rightly refuses a v3 factor list naming it. Drop it (the 2026-08-27
      // production 400: an orphaned legacy wrap aborted the whole migration).
      if (material.registeredCredentialIds
          && !material.registeredCredentialIds.includes(f.credential_id)) {
        continue;
      }
      let recipients = [];
      if (material.passkeyRecipient
          && material.passkeyRecipient.credentialId === f.credential_id) {
        recipients = [{
          recipient_public_key: material.passkeyRecipient.publicKeyHex,
          label: material.passkeyRecipient.label || 'This device',
          created_at: nowIso,
        }];
        leaves.push({ op: 'factor', factor_id: f.factor_id });
      }
      factors.push({
        factor_id: f.factor_id,
        type: 'passkey',
        credential_id: f.credential_id,
        recipients,
      });
      access.push(f.factor_id);
    }
  }
  if (!leaves.length) return null;
  const policy = leaves.length === 1 ? leaves[0] : { op: 'or', children: leaves };
  return [{ op: 'migrate_legacy', factors, access, root_policy: policy }];
}
