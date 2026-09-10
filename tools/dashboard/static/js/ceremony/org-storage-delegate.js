/** Local-only organization delegate preparation; retained in personal audited Settings. */
import { bytesToHex, canonicalJson, domainBytes } from './primitives.js';
import { derivePersona, buildEvent, signEvent } from './ledger-event.js';

export async function prepareStorageDelegate(rootSeed, context, now = Date.now()) {
  const metadata = context.delegate_metadata || {};
  if (metadata.key_exists && metadata.expires_at - now >= context.remint_below_ms) {
    return { action: 'reuse', organization: context.organization,
      key_reference: metadata.key_reference };
  }
  const persona = await derivePersona(rootSeed, context.genesis_id);
  let key;
  let pkcs8;
  try {
    const pair = await crypto.subtle.generateKey({ name: 'Ed25519' }, true, ['sign', 'verify']);
    key = pair.privateKey;
    pkcs8 = new Uint8Array(await crypto.subtle.exportKey('pkcs8', key));
    const childPub = bytesToHex(await crypto.subtle.exportKey('raw', pair.publicKey));
    const nonce = bytesToHex(crypto.getRandomValues(new Uint8Array(32)));
    const terms = { child_pub: childPub, scope: context.scope,
      can_redelegate: false, ttl: context.ttl_ms, grant_nonce: nonce };
    const proof = bytesToHex(await crypto.subtle.sign('Ed25519', key,
      domainBytes('autonomy.ledger.delegate-consent.v2\n', canonicalJson({
        genesis_id: context.genesis_id, issuer_key: persona.publicHex, ...terms,
      }))));
    const event = await signEvent(buildEvent({
      authorKey: persona.publicHex, parents: context.parents,
      hlc: [now, 0], payload: { type: 'delegate', ...terms, proof },
    }), persona.signingKey);
    return { action: 'new', organization: context.organization,
      private_key: bytesToHex(pkcs8.slice(16, 48)), event: canonicalJson(event) };
  } finally {
    if (pkcs8) pkcs8.fill(0);
    key = null;
    persona.signingKey = null;
  }
}
