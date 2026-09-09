/* The acceptance ceremony seam for the org:join controller.
 *
 * Browser-local, and jealously so (I1): it fetches the invitee's OWN personal
 * armor, opens it with the passphrase THEY type, derives the per-org persona
 * and signs the member.claim — then zeroes the root seed. Only the signed
 * public claim leaves, plus the invitee's own persona KEM private key, which is
 * theirs to keep for reading org-sealed data. The passphrase and the personal
 * root seed never leave this function and are never sent anywhere.
 *
 * Joining a new org is a COLD-root operation: the per-org persona is derived
 * from the personal root, which is not kept warm after sign-on, so the invitee
 * re-enters the passphrase here exactly as they would to genesis or recover.
 *
 * The crypto dependencies are injected so the orchestration — fetch, decrypt,
 * mint, zeroize, no-leak — can be tested without a full ceremony vector; the
 * primitives themselves are covered by ceremony/claim.js's own tests.
 */
import { openArmorWithPassword as realOpenArmor } from '../ceremony/root-factor-policy.js';
import { mintMemberClaim as realMint } from '../ceremony/claim.js';

async function defaultFetchPersonal() {
  const resp = await fetch('/api/identity/personal', {
    headers: { Accept: 'application/json' },
  });
  if (!resp.ok) {
    throw new Error(`personal identity unavailable (${resp.status})`);
  }
  return resp.json();
}

function defaultRandomSeed() {
  return crypto.getRandomValues(new Uint8Array(32));
}

export function makeCeremony({
  fetchPersonal = defaultFetchPersonal,
  decryptArmor = realOpenArmor,
  mintMemberClaim = realMint,
  randomSeed = defaultRandomSeed,
} = {}) {
  // ``approvals`` and ``position`` are supplied only by finalize(): the
  // second submit must re-mint at the staged claim's pinned causal position
  // carrying the countersignatures already gathered, or the ledger sees a
  // fresh unapproved claim and the signatures are lost.
  return async function runCeremony({
    context, inputs, passphrase, approvals = [], position = null,
  }) {
    if (typeof passphrase !== 'string' || !passphrase) {
      throw new Error('a passphrase is required to accept');
    }
    const personal = await fetchPersonal();
    if (!personal || typeof personal.armored_private_key !== 'string') {
      throw new Error('no personal identity on this device to join with');
    }
    // Acquire the seeds INSIDE the try so the finally covers them the instant
    // they exist: a throw between opening the armor and minting (e.g. a
    // faulting randomSeed) still zeroes the root seed rather than leaking it.
    let opened = null;
    let kemSeed = null;
    try {
      opened = await decryptArmor(personal.armored_private_key, passphrase);
      kemSeed = randomSeed();
      const minted = await mintMemberClaim({
        context,
        personalRootSeed: opened.seed,
        inviteRef: inputs.inviteRef,
        token: inputs.bearer || null,
        kemSeed,
        approvals,
        position,
      });
      return {
        event: minted.event,
        personaPub: minted.personaPub,
        claimKey: minted.claimKey,
        // The invitee's own per-org persona KEM private key — kept by them to
        // read org-sealed data. It is never sent; the controller persists it
        // locally only once the claim is admitted.
        kemPrivateKey: minted.kemPrivateKey,
        kemCredential: minted.kemCredential,
      };
    } finally {
      // mintMemberClaim copies and zeroes its own inputs; zero ours too, on
      // every path, so a thrown mint never leaves the root seed in memory.
      if (opened && opened.seed) opened.seed.fill(0);
      if (kemSeed) kemSeed.fill(0);
    }
  };
}

/* The same ceremony, opened through the STANDARD root control.
 *
 * :func:`makeCeremony` above takes a passphrase, which is right for a caller
 * that already collected one. The join page must not: constraint I1 forbids a
 * password field on it, and a passphrase box assumes one factor when an
 * identity may be opened by a passkey or a recovery code instead. ``openRoot``
 * is the shared control that presents whichever factors this identity has,
 * pins the one the session signed in with, and returns the opened seed — so
 * the page invokes it rather than reimplementing a narrower version of it.
 *
 * The seed is zeroed on every path, as above; the caller never sees it.
 */
export function makeRootCeremony({
  openRoot,
  mintMemberClaim = realMint,
  randomSeed = defaultRandomSeed,
}) {
  if (typeof openRoot !== 'function') {
    throw new Error('makeRootCeremony needs the openRoot control');
  }
  return async function runCeremony({
    context, inputs, approvals = [], position = null, title, detail,
  }) {
    const opened = await openRoot({
      title: title || 'Ask to join',
      detail: detail || 'Unlock your identity to sign your request to join.',
    });
    // null is the operator cancelling the ceremony — not an error, and not
    // something to retry or report as a failure.
    if (!opened) return null;
    let kemSeed = null;
    try {
      kemSeed = randomSeed();
      const minted = await mintMemberClaim({
        context,
        personalRootSeed: opened.seed,
        inviteRef: inputs.inviteRef,
        token: inputs.bearer || null,
        kemSeed,
        approvals,
        position,
      });
      return {
        event: minted.event,
        personaPub: minted.personaPub,
        claimKey: minted.claimKey,
        kemPrivateKey: minted.kemPrivateKey,
        kemCredential: minted.kemCredential,
      };
    } finally {
      if (opened && opened.seed) opened.seed.fill(0);
      if (kemSeed) kemSeed.fill(0);
    }
  };
}
