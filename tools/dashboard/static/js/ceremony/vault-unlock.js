/**
 * Waking the vault at unlock — the browser half.
 *
 * The vault is COLD until a human unlocks, and nothing about a previous unlock
 * survives a restart (crib §10). What that costs, concretely: the agent
 * delegate is MEMORY-class, its private half is not derivable, and so a fresh
 * one must be minted on EVERY unlock. Signing in and not doing this leaves the
 * identity looking perfectly healthy while every vault read and write fails.
 *
 * Founding is different and happens ONCE, ever: the genesis embeds a timestamp
 * and is signed, so founding twice yields a different genesis and orphans
 * everything sealed under the first.
 *
 * The root never leaves this page. The routes below receive signed wire events
 * and the delegate's own key — never the personal root, and never a password.
 *
 * The exact sequence here was executed end to end against a live host on
 * 2026-08-20 before being written; `graph://82e3bdd4-667` records it along with
 * the six things that are invisible until you run it. Three of them are load
 * bearing enough to repeat:
 *
 *   - `personal` is SCOPED, not forbidden. Every /api/network/ledger/* call
 *     403s with "cross-org access is not permitted" unless `X-Graph-Org:
 *     personal` accompanies `?org=personal`. The 403 reads as a prohibition and
 *     is a missing header.
 *   - The genesis carries the org's STABLE ID, not the slug.
 *   - The scope sort is PART OF THE BINDING, not tidiness: a scope set has no
 *     order, and signing the caller's order would make one grant produce two
 *     different signatures.
 */

import {
  canonicalJson,
  deriveEncapsulationKeypair,
  importEd25519RootSigningKey,
} from './primitives.js';
import { buildEvent, derivePersona, signEvent } from './ledger-event.js';
import { buildPersonaKemCredential, deriveKemSeed } from './founding.js';
import { createRootAnchorEnvelope } from './root-anchor.js';

/** Mirrors ``tools.network.ledger.events.DELEGATE_CONSENT_DOMAIN``. */
const DELEGATE_CONSENT_DOMAIN = 'autonomy.ledger.delegate-consent.v2\n';

/** Mirrors ``tools.network.clock.DEFAULT_DELEGATE_TTL_MS`` — 12 hours. */
export const DEFAULT_DELEGATE_TTL_MS = 12 * 60 * 60 * 1000;

const GRANT_NONCE_BYTES = 32;
const PERSONAL_ROOT_ANCHOR_ID = 'personal-root-default';
const PERSONAL_ROOT_CLASS_NAME = 'Personal root vault';
// Mirrors tools.vault.personal_object.DELEGATE_AUDITED_DERIVE_PURPOSE.
const DELEGATE_AUDITED_DERIVE_PURPOSE =
  'autonomy/vault/delegate-audited-recipient/v1';

const textEncoder = new TextEncoder();
const webCrypto = globalThis.crypto;

function bytesToHex(buffer) {
  return Array.from(new Uint8Array(buffer))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

/**
 * The EXACTLY-two execution scopes a storage agent delegate may carry.
 * Sorted, because the sort is part of what gets signed. Never `checkpoint`,
 * never an intent scope, never `*`.
 */
export function storageDelegateScopes(domainId) {
  return [
    `storage:state:advance:${domainId}`,
    `storage:capability:grant:${domainId}`,
  ].sort();
}

/** A fresh single-use grant nonce. Either side may mint it — the child signs
 *  the whole proof input and the fold enforces uniqueness, so who chose it
 *  carries no security weight. */
export function mintGrantNonce() {
  return bytesToHex(webCrypto.getRandomValues(new Uint8Array(GRANT_NONCE_BYTES)));
}

/**
 * The bytes a delegate's consent proof covers.
 *
 * Binds the genesis, the ISSUING member's key, the child key itself, and the
 * FULL grant terms plus a single-use nonce. The terms are covered so a grant
 * the child consented to cannot be reissued with wider authority under the
 * child's own signature; the nonce is what makes that consent revocable rather
 * than perpetual.
 *
 * `ttl` is OMITTED when absent rather than sent as null, mirroring the payload.
 */
export function delegateProofInput({
  genesisId, issuerKey, childPub, scope, canRedelegate = false, ttl = null,
  grantNonce,
}) {
  const body = {
    genesis_id: genesisId,
    issuer_key: issuerKey,
    child_pub: childPub,
    scope: [...new Set(scope)].sort(),
    can_redelegate: Boolean(canRedelegate),
    grant_nonce: grantNonce,
  };
  if (ttl !== null && ttl !== undefined) body.ttl = Number(ttl);
  return textEncoder.encode(DELEGATE_CONSENT_DOMAIN + canonicalJson(body));
}

/** The named delegate key signs the grant that names it — proof of possession
 *  AND consent over the grant's full terms. */
export async function signDelegateProof({ childPrivateKey, ...terms }) {
  const signature = await webCrypto.subtle.sign(
    'Ed25519', childPrivateKey, delegateProofInput(terms),
  );
  return bytesToHex(signature);
}

async function generateChildKey() {
  const pair = await webCrypto.subtle.generateKey(
    'Ed25519', true, ['sign', 'verify'],
  );
  const raw = await webCrypto.subtle.exportKey('raw', pair.publicKey);
  const pkcs8 = await webCrypto.subtle.exportKey('pkcs8', pair.privateKey);
  // The last 32 bytes of a PKCS#8 Ed25519 key are the seed the server expects.
  const seed = new Uint8Array(pkcs8).slice(-32);
  return { pair, publicHex: bytesToHex(raw), privateHex: bytesToHex(seed) };
}

/**
 * Mint one agent delegate: a fresh signing key, its consent proof, and the
 * signed ledger event granting it exactly the two storage scopes.
 *
 * Returns the wire event to POST and the child's private key, which is the one
 * piece of key material the dashboard is permitted to hold (crib §12 — the
 * attenuated delegate's, never a persona's).
 */
export async function mintDelegate({
  personalRootSeed, genesisId, heads, domainId = null, now = Date.now(),
  ttlMs = DEFAULT_DELEGATE_TTL_MS,
}) {
  const persona = await derivePersona(personalRootSeed, genesisId);
  const child = await generateChildKey();
  const scope = storageDelegateScopes(domainId);
  const grantNonce = mintGrantNonce();

  const payload = {
    type: 'delegate',
    child_pub: child.publicHex,
    scope,
    can_redelegate: false,
    ttl: Number(ttlMs),
    grant_nonce: grantNonce,
    proof: await signDelegateProof({
      childPrivateKey: child.pair.privateKey,
      genesisId,
      issuerKey: persona.publicHex,
      childPub: child.publicHex,
      scope,
      canRedelegate: false,
      ttl: Number(ttlMs),
      grantNonce,
    }),
  };

  const event = await signEvent(
    buildEvent({
      authorKey: persona.publicHex,
      parents: heads,
      hlc: [Math.floor(now), 0],
      payload,
    }),
    persona.signingKey,
  );

  return { event, delegateSigningKey: child.privateHex, scope };
}

/** `X-Graph-Org` is not optional for the personal store — see the header note. */
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

//: Key under which a failed wake leaves its operator-legible message for the
//: shell (identity-indicator) to render. Cleared by the next successful wake.
export const WAKE_FAILED_STORAGE_KEY = 'autonomy.vault.wake-failed';

const WAKE_REASON_PHRASES = [
  ['anchor-inventory', 'the vault anchor inventory could not be read'],
  ['personal-root-public-key', 'the personal identity record has no root public key'],
  ['personal-root', 'the personal identity record could not be read'],
  ['anchor-race', 'the vault anchor inventory changed mid-ceremony'],
  ['anchor-enroll', 'the root anchor could not be enrolled'],
  ['root-class', 'the root policy class could not be created'],
  ['ledger-no-genesis', 'the personal ledger database is present but holds no'
    + ' genesis — the wrong store may be resolving, or the data is damaged;'
    + ' do NOT re-found'],
  ['not-founded', 'this identity’s personal ledger is not founded, so nothing can be delegated'],
  ['heads', 'the personal ledger heads could not be read'],
  ['delegate', 'the storage delegate grant was refused by the ledger'],
  ['vault-keys', 'the dashboard refused the vault key material'],
];

/** One operator-legible sentence for a wake failure reason slug. */
export function describeWakeFailure(reason) {
  const slug = String(reason || 'unknown');
  const match = WAKE_REASON_PHRASES.find(([prefix]) => slug.startsWith(prefix));
  const phrase = match ? match[1] : 'an unexpected step failed';
  const status = /-(\d{3})$/.exec(slug);
  return 'The vault did not come up: ' + phrase
    + (status ? ' (HTTP ' + status[1] + ')' : '')
    + '. Secrets stay locked until a sign-in completes this step'
    + ' [' + slug + '].';
}

/**
 * Make a wake outcome VISIBLE. A failed vault wake used to vanish: callers
 * ignored the returned reason, nothing logged, and the operator saw a
 * completed sign-in with a dead vault (2026-09-06 incident, auto-uhdxm).
 * Every failure now lands in three places — the console, the capped
 * client-error log, and a storage flag the shell renders — and a later
 * successful wake clears the flag.
 */
function reportWakeOutcome(result, fetchImpl) {
  const storage = (typeof sessionStorage !== 'undefined') ? sessionStorage : null;
  try {
    if (result && result.ready) {
      if (storage) storage.removeItem(WAKE_FAILED_STORAGE_KEY);
      return;
    }
    const reason = (result && result.reason) || 'unknown';
    const message = describeWakeFailure(reason);
    if (typeof console !== 'undefined' && console.error) {
      console.error('vault wake failed:', message);
    }
    if (storage) storage.setItem(WAKE_FAILED_STORAGE_KEY, message);
    Promise.resolve(fetchImpl('/api/identity/ceremony-error', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ceremony: 'vault-wake',
        action: reason,
        name: 'VaultWakeFailure',
        message,
        stack: '',
        context: {},
      }),
    })).catch(() => {});
  } catch (e) { /* diagnostics must never break the unlock */ }
}

/**
 * Bring the vault up for this unlock. Call it AFTER the session exists.
 *
 * Returns `{ ready, reason }`. It never throws for an expected cold-start
 * condition: a vault that failed to wake must leave the dashboard usable and
 * say so, not break the sign-in that just succeeded. "Say so" is enforced
 * centrally here: every outcome passes through reportWakeOutcome, so no
 * caller can silently drop a failure again.
 */
export async function wakeVault(options) {
  const result = await _wakeVaultInner(options);
  reportWakeOutcome(result, options.fetchImpl || fetch);
  return result;
}

async function _wakeVaultInner({
  personalRootSeed, generationKeys = {}, fetchImpl = fetch, now = Date.now(),
}) {
  const personalVault = await ensurePersonalRootVault({
    personalRootSeed, fetchImpl, now,
  });
  if (!personalVault.ready) return personalVault;

  const heads = await fetchImpl(
    '/api/network/ledger/heads?org=personal',
    { headers: personalHeaders(), credentials: 'same-origin' },
  );
  if (heads.status === 409) {
    // 409 is NOT "never founded" — the server sends it when the ledger
    // database EXISTS but carries no genesis. On a founded identity that is
    // an alarm (wrong store resolved, or damaged data), and collapsing it
    // into "not founded" is the signal everyone missed in the 2026-09-06
    // incident. Founding stays a once-ever explicit ceremony either way:
    // doing it silently on a failed read is how an identity acquires a
    // second genesis.
    return { ready: false, reason: 'ledger-no-genesis' };
  }
  if (heads.status === 404) return { ready: false, reason: 'not-founded' };
  if (!heads.ok) return { ready: false, reason: `heads-${heads.status}` };
  const { genesis_id: genesisId, heads: headIds } = await body(heads);

  const { event, delegateSigningKey } = await mintDelegate({
    personalRootSeed, genesisId, heads: headIds, now,
  });

  const granted = await fetchImpl('/api/network/ledger/delegate', {
    method: 'POST',
    headers: personalHeaders(),
    credentials: 'same-origin',
    body: JSON.stringify({ org: 'personal', event: canonicalJson(event) }),
  });
  if (!granted.ok) {
    return { ready: false, reason: `delegate-${granted.status}` };
  }

  // Publish the persona's KEM credential and hand its private half to the
  // dashboard, exactly as tools/vault/warm_client does. Without this the vault
  // wakes for THIS process only: nothing is durably recoverable, because a
  // mint's self-grant has no published recipient to seal to. Both are derived
  // deterministically from the root here (counter 0); the dashboard holds only
  // the KEM private + delegate (crib §12), never the root. Byte-parity with the
  // Python is enforced by test_vault_unlock_crossimpl.
  const persona = await derivePersona(personalRootSeed, genesisId);
  const kemSeed = await deriveKemSeed(personalRootSeed);
  const { credential: kemCredential, kemPrivateKey } =
    await buildPersonaKemCredential({
      persona,
      genesisId,
      kemSeed,
      authorityHeads: headIds,
      createdHlc: [now, 0],
    });
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
      delegate_signing_key: delegateSigningKey,
      kem_credential: kemCredential,
      persona_kem_private_key: kemPrivateKey,
      delegate_audited_private_key: auditedDelegate.privateKeyHex,
      delegate_audited_public_key: auditedDelegate.publicKeyHex,
    }),
  });
  if (!up.ok) return { ready: false, reason: `vault-keys-${up.status}` };

  return { ready: true, reason: null };
}
