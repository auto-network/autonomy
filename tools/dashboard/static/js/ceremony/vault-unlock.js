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

import { canonicalJson } from './primitives.js';
import { buildEvent, derivePersona, signEvent } from './ledger-event.js';

/** Mirrors ``tools.network.ledger.events.DELEGATE_CONSENT_DOMAIN``. */
const DELEGATE_CONSENT_DOMAIN = 'autonomy.ledger.delegate-consent.v2\n';

/** Mirrors ``tools.network.clock.DEFAULT_DELEGATE_TTL_MS`` — 12 hours. */
export const DEFAULT_DELEGATE_TTL_MS = 12 * 60 * 60 * 1000;

const GRANT_NONCE_BYTES = 32;

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

/**
 * Bring the vault up for this unlock. Call it AFTER the session exists.
 *
 * Returns `{ ready, reason }`. It never throws for an expected cold-start
 * condition: a vault that failed to wake must leave the dashboard usable and
 * say so, not break the sign-in that just succeeded.
 */
export async function wakeVault({
  personalRootSeed, generationKeys = {}, fetchImpl = fetch, now = Date.now(),
}) {
  const heads = await fetchImpl(
    '/api/network/ledger/heads?org=personal',
    { headers: personalHeaders(), credentials: 'same-origin' },
  );
  if (heads.status === 409) {
    // Not founded. Founding is a once-ever ceremony and deliberately NOT done
    // here: it needs its own confirmation, and doing it silently on a failed
    // read is how an identity acquires a second genesis.
    return { ready: false, reason: 'not-founded' };
  }
  if (!heads.ok) return { ready: false, reason: `heads-${heads.status}` };
  const { genesis_id: genesisId, heads: headIds } = await body(heads);

  const { event, delegateSigningKey } = await mintDelegate({
    personalRootSeed, genesisId, heads: headIds, now,
  });

  const granted = await fetchImpl('/api/network/ledger/delegate', {
    method: 'POST',
    headers: personalHeaders(),
    credentials: 'same-origin',
    body: JSON.stringify({ org: 'personal', event: JSON.stringify(event) }),
  });
  if (!granted.ok) {
    return { ready: false, reason: `delegate-${granted.status}` };
  }

  const up = await fetchImpl('/api/identity/unlock/vault-keys', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({
      generation_keys: generationKeys,
      delegate_signing_key: delegateSigningKey,
    }),
  });
  if (!up.ok) return { ready: false, reason: `vault-keys-${up.status}` };

  return { ready: true, reason: null };
}
