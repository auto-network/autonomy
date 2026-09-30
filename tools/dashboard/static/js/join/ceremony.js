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
import { mintMemberClaim as realMint, claimKey as realClaimKey } from '../ceremony/claim.js';
import { deriveKemSeed as realDeriveKemSeed } from '../ceremony/founding.js';
import { derivePersona as realDerivePersona } from '../ceremony/ledger-event.js';
import { deriveEncapsulationKeypair as realDeriveEncapsulationKeypair } from '../ceremony/primitives.js';

async function defaultFetchPersonal() {
  const resp = await fetch('/api/identity/personal', {
    headers: { Accept: 'application/json' },
  });
  if (!resp.ok) {
    throw new Error(`personal identity unavailable (${resp.status})`);
  }
  return resp.json();
}

export function makeCeremony({
  fetchPersonal = defaultFetchPersonal,
  decryptArmor = realOpenArmor,
  mintMemberClaim = realMint,
  deriveKemSeed = realDeriveKemSeed,
} = {}) {
  return async function runCeremony({ context, inputs, passphrase }) {
    if (typeof passphrase !== 'string' || !passphrase) {
      throw new Error('a passphrase is required to accept');
    }
    const personal = await fetchPersonal();
    if (!personal || typeof personal.armored_private_key !== 'string') {
      throw new Error('no personal identity on this device to join with');
    }
    // Acquire the seeds INSIDE the try so the finally covers them the instant
    // they exist: a throw between opening the armor and minting (e.g. a
    // faulting derivation) still zeroes the root seed rather than leaking it.
    let opened = null;
    let kemSeed = null;
    try {
      opened = await decryptArmor(personal.armored_private_key, passphrase);
      // Initial admission uses the same root/counter derivation as founding
      // and later sign-in (crib §§1c,14,15). This is not a transport key.
      kemSeed = await deriveKemSeed(opened.seed, 0);
      const minted = await mintMemberClaim({
        context,
        personalRootSeed: opened.seed,
        inviteRef: inputs.inviteRef,
        token: inputs.bearer || null,
        kemSeed,
      });
      return {
        event: minted.event,
        personaPub: minted.personaPub,
        claimKey: minted.claimKey,
        // The invitee's own per-org persona KEM private key — kept by them to
        // read org-sealed data. The controller holds it in memory; subsequent
        // root ceremonies can re-derive it. Never send it to the inviter.
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
  deriveKemSeed = realDeriveKemSeed,
}) {
  if (typeof openRoot !== 'function') {
    throw new Error('makeRootCeremony needs the openRoot control');
  }
  return async function runCeremony({ context, inputs, profile = {}, title, detail }) {
    const opened = await openRoot({
      title: title || 'Ask to join',
      detail: detail || 'Unlock your identity to sign your request to join.',
    });
    // null is the operator cancelling the ceremony — not an error, and not
    // something to retry or report as a failure.
    if (!opened) return null;
    let kemSeed = null;
    try {
      kemSeed = await deriveKemSeed(opened.seed, 0);
      const minted = await mintMemberClaim({
        context,
        personalRootSeed: opened.seed,
        inviteRef: inputs.inviteRef,
        token: inputs.bearer || null,
        profile,
        kemSeed,
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

/* The identity half of the ceremony, with NO claim: the persona and the
 * per-org KEM key are pure derivations of the personal root and the org's
 * genesis id (the same derivations mintMemberClaim performs), so a member
 * who was already admitted can re-derive them on a later visit and finish
 * joining over the same link -- ``status`` then ``bootstrap`` -- without a
 * second claim, which the ledger would refuse (persona exists) or could not
 * take (single-use invitation consumed). Nothing is signed and nothing is
 * sent; the root seed and the KEM seed are zeroed on every path.
 */
export function makeRootIdentityCeremony({
  openRoot,
  deriveKemSeed = realDeriveKemSeed,
  derivePersona = realDerivePersona,
  deriveEncapsulationKeypair = realDeriveEncapsulationKeypair,
  claimKey = realClaimKey,
}) {
  if (typeof openRoot !== 'function') {
    throw new Error('makeRootIdentityCeremony needs the openRoot control');
  }
  return async function runIdentityCeremony({ genesisId, inviteRef, title, detail }) {
    if (typeof genesisId !== 'string' || !/^[0-9a-f]{64}$/.test(genesisId)) {
      throw new Error('genesisId must be 64 lowercase hex characters');
    }
    const opened = await openRoot({
      title: title || 'Finish joining',
      detail: detail || 'Unlock your identity to continue your approved request to join.',
    });
    if (!opened) return null;
    let kemSeed = null;
    try {
      kemSeed = await deriveKemSeed(opened.seed, 0);
      const persona = await derivePersona(opened.seed, genesisId);
      const kem = await deriveEncapsulationKeypair(kemSeed, 'autonomy/persona-kem/v1/' + genesisId);
      return {
        personaPub: persona.publicHex,
        claimKey: await claimKey(inviteRef, persona.publicHex),
        kemPrivateKey: kem.privateKeyHex,
        kemCredential: { kem_public_key: kem.publicKeyHex },
      };
    } finally {
      if (opened && opened.seed) opened.seed.fill(0);
      if (kemSeed) kemSeed.fill(0);
    }
  };
}
