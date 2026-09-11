/** Personal vault warm-up: retain decryption keys, never mint ledger authority. */

import { deriveEncapsulationKeypair, importEd25519RootSigningKey } from './primitives.js';
import { deriveKemSeed } from './founding.js';
import { createRootAnchorEnvelope } from './root-anchor.js';
import { reportStepOutcome } from './step-report.js';

const PERSONAL_ROOT_ANCHOR_ID = 'personal-root-default';
const PERSONAL_ROOT_CLASS_NAME = 'Personal root vault';
// Mirrors tools.vault.personal_object.DELEGATE_AUDITED_DERIVE_PURPOSE.
const DELEGATE_AUDITED_DERIVE_PURPOSE =
  'autonomy/vault/delegate-audited-recipient/v1';

export function deriveAuditedRecipient(rootSeed) {
  return deriveEncapsulationKeypair(rootSeed, DELEGATE_AUDITED_DERIVE_PURPOSE);
}

/** Phase 2: construct the existing handoff using only pre-fetched inputs. */
export async function prepareVault(rootSeed, prepared, audited, organizations = []) {
  const result = { keys: { generation_keys: {},
    delegate_audited_private_key: audited.privateKeyHex,
    delegate_audited_public_key: audited.publicKeyHex } };
  if (prepared.recovery_genesis_id) {
    const kemSeed = await deriveKemSeed(rootSeed);
    try {
      result.keys.persona_kem_private_key = (await deriveEncapsulationKeypair(
        kemSeed, 'autonomy/persona-kem/v1/' + prepared.recovery_genesis_id,
      )).privateKeyHex;
    } finally { kemSeed.fill(0); }
  }
  result.keys.organization_kem_keys = [];
  result.failures = [];
  if (organizations.length) {
    const kemSeed = await deriveKemSeed(rootSeed, 0);
    try {
      for (const org of organizations) {
        try {
          const recovery = org.encryption_recovery;
          if (!recovery || recovery.error) throw new Error('organization-encryption-unavailable');
          if (recovery.counter !== 0 || recovery.genesis_id !== org.genesis_id) {
            throw new Error('organization-encryption-context-mismatch');
          }
          const pair = await deriveEncapsulationKeypair(kemSeed,
            'autonomy/persona-kem/v1/' + recovery.genesis_id);
          const credential = recovery.credentials.find(c => c.kem_public_key === pair.publicKeyHex);
          if (!credential) throw new Error('organization-encryption-key-mismatch');
          result.keys.organization_kem_keys.push({ organization: org.slug,
            genesis_id: recovery.genesis_id, kem_key_id: credential.kem_key_id,
            persona_kem_private_key: pair.privateKeyHex });
        } catch (error) {
          result.failures.push({ org: org.slug, step: 'organization-recovery', error: error.message });
        }
      }
    } finally { kemSeed.fill(0); }
  }
  const inventory = prepared.inventory;
  if (!rootReachableClass(inventory)) {
    let anchor = inventory.anchors.find(a => a.anchor_id === PERSONAL_ROOT_ANCHOR_ID)
      || inventory.anchors[0];
    if (!anchor) {
      let signingKey = await importEd25519RootSigningKey(rootSeed);
      try {
        anchor = await createRootAnchorEnvelope({ seed: rootSeed, signingKey,
          rootPub: prepared.root_pub }, { anchorId: PERSONAL_ROOT_ANCHOR_ID,
          displayName: 'Personal root vault access', createdAt: new Date().toISOString() });
        result.anchor = anchor;
      } finally { signingKey = null; }
    }
    result.class_anchor_id = anchor.anchor_id;
  }
  return result;
}

/** Phase 3: no root key is needed by any of these writes. */
export async function submitVault(prepared, fetchImpl = fetch) {
  async function post(url, payload) {
    const response = await fetchImpl(url, { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    const result = await body(response);
    if (!response.ok || result.ok === false) throw new Error(result.error || 'vault handoff failed');
    return result;
  }
  if (prepared.anchor) await post('/api/identity/vault-anchors', { anchor: prepared.anchor });
  if (prepared.class_anchor_id) await post(
    '/api/identity/vault-anchors/' + encodeURIComponent(prepared.class_anchor_id) + '/classes',
    { display_name: PERSONAL_ROOT_CLASS_NAME });
  return await post('/api/identity/unlock/vault-keys', prepared.keys);
}

/** Scope personal vault bootstrap requests explicitly. */
function personalHeaders() {
  return { 'Content-Type': 'application/json', 'X-Graph-Org': 'personal' };
}

async function body(response) {
  try { return await response.json(); } catch { return {}; }
}

function rootReachableClass(inventory, anchorId = null) {
  return (inventory?.classes || []).find((record) => (
    record?.governance?.form === 'root-reachable'
    && (!anchorId || record.governance.anchor_id === anchorId)
  )) || null;
}

async function rootAnchorInventory(fetchImpl) {
  const response = await fetchImpl('/api/identity/vault-anchors', {
    headers: { Accept: 'application/json' },
    credentials: 'same-origin',
  });
  return { response, inventory: await body(response) };
}

/**
 * Ensure the one stable personal-root recipient exists as part of the login
 * that has ALREADY opened the root. This is bootstrap, not a second ceremony:
 * no setting name or value participates, and repeated/concurrent logins reuse
 * the same anchor and class.
 */
export async function ensurePersonalRootVault({
  personalRootSeed, fetchImpl = fetch, now = Date.now(),
}) {
  let { response, inventory } = await rootAnchorInventory(fetchImpl);
  if (!response.ok) {
    return { ready: false, created: false, reason: `anchor-inventory-${response.status}` };
  }
  if (rootReachableClass(inventory)) {
    return { ready: true, created: false, reason: null };
  }

  let anchor = (inventory.anchors || []).find(
    (item) => item?.anchor_id === PERSONAL_ROOT_ANCHOR_ID,
  ) || (inventory.anchors || [])[0] || null;

  if (!anchor) {
    const personalResponse = await fetchImpl('/api/identity/personal', {
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    });
    const personal = await body(personalResponse);
    if (!personalResponse.ok) {
      return { ready: false, created: false, reason: `personal-root-${personalResponse.status}` };
    }
    if (typeof personal.root_pub !== 'string' || !/^[0-9a-f]{64}$/.test(personal.root_pub)) {
      return { ready: false, created: false, reason: 'personal-root-public-key' };
    }

    const signingKey = await importEd25519RootSigningKey(personalRootSeed);
    const candidate = await createRootAnchorEnvelope({
      seed: personalRootSeed,
      signingKey,
      rootPub: personal.root_pub,
    }, {
      anchorId: PERSONAL_ROOT_ANCHOR_ID,
      displayName: 'Personal root vault access',
      createdAt: new Date(now).toISOString(),
    });
    const enrolled = await fetchImpl('/api/identity/vault-anchors', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ anchor: candidate }),
    });
    if (enrolled.ok) {
      anchor = (await body(enrolled)).anchor;
    } else {
      // Another login tab may have won the insert race. Re-read and reuse its
      // signed record; never overwrite a stable anchor with our candidate.
      ({ response, inventory } = await rootAnchorInventory(fetchImpl));
      if (!response.ok) {
        return { ready: false, created: false, reason: `anchor-race-${response.status}` };
      }
      anchor = (inventory.anchors || []).find(
        (item) => item?.anchor_id === PERSONAL_ROOT_ANCHOR_ID,
      ) || null;
      if (!anchor) {
        return { ready: false, created: false, reason: `anchor-enroll-${enrolled.status}` };
      }
    }
  }

  const minted = await fetchImpl(
    `/api/identity/vault-anchors/${encodeURIComponent(anchor.anchor_id)}/classes`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ display_name: PERSONAL_ROOT_CLASS_NAME }),
    },
  );
  if (!minted.ok) {
    ({ response, inventory } = await rootAnchorInventory(fetchImpl));
    if (!response.ok || !rootReachableClass(inventory, anchor.anchor_id)) {
      return { ready: false, created: false, reason: `root-class-${minted.status}` };
    }
  }
  return { ready: true, created: true, reason: null };
}

/**
 * Bring the vault up for this unlock. Call it AFTER the session exists.
 *
 * Returns `{ ready, reason }`. It never throws for an expected cold-start
 * condition: a vault that failed to wake must leave the dashboard usable and
 * say so, not break the sign-in that just succeeded. "Say so" is enforced
 * centrally here: every outcome passes through reportStepOutcome (the one
 * surfacing mechanism, shared with the root-step runner), so no caller can
 * silently drop a failure again.
 */
export async function wakeVault(options) {
  const result = await _wakeVaultInner(options);
  reportStepOutcome('vault-wake', result, { fetchImpl: options.fetchImpl || fetch });
  return result;
}

async function _wakeVaultInner({
  personalRootSeed, generationKeys = {}, fetchImpl = fetch, now = Date.now(),
}) {
  const personalVault = await ensurePersonalRootVault({
    personalRootSeed, fetchImpl, now,
  });
  if (!personalVault.ready) return personalVault;

  // Old storage-format records carry their domain id in their descriptors.
  // Recover their decrypt-only key without a ledger, persona, or new credential.
  const recovery = await fetchImpl('/api/identity/unlock/vault-keys', {
    credentials: 'same-origin',
  });
  if (!recovery.ok) return { ready: false, reason: `recovery-${recovery.status}` };
  const { recovery_genesis_id: genesisId } = await body(recovery);
  let kemPrivateKey;
  if (genesisId) {
    const kemSeed = await deriveKemSeed(personalRootSeed);
    const pair = await deriveEncapsulationKeypair(
      kemSeed, 'autonomy/persona-kem/v1/' + genesisId,
    );
    kemPrivateKey = pair.privateKeyHex;
    kemSeed.fill(0);
  }

  // A purpose-separated X25519 recipient lets audited personal Settings be
  // written while cold and read unattended while this process remains warm.
  // The browser is the only place holding the personal root: derive here,
  // publish only the public half, and hand the private half to the same
  // authenticated memory-key route as the other unlock material.
  const auditedDelegate = await deriveEncapsulationKeypair(
    personalRootSeed, DELEGATE_AUDITED_DERIVE_PURPOSE,
  );

  const up = await fetchImpl('/api/identity/unlock/vault-keys', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({
      generation_keys: generationKeys,
      persona_kem_private_key: kemPrivateKey,
      delegate_audited_private_key: auditedDelegate.privateKeyHex,
      delegate_audited_public_key: auditedDelegate.publicKeyHex,
    }),
  });
  if (!up.ok) return { ready: false, reason: `vault-keys-${up.status}` };

  return { ready: true, reason: null };
}
