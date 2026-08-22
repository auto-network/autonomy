// Shared auto.network identity-pin commitment (v1).
// Keep byte-for-byte aligned with tools.network.link_identity.commitment.
const DOMAIN = new TextEncoder().encode('autonomy.link.identity-pin.v1\0');

function hexBytes(hex) {
  if (!/^[0-9a-f]{64}$/.test(hex)) throw new Error('invalid genesis id');
  const out = new Uint8Array(32);
  for (let i = 0; i < out.length; i += 1) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}

function concat(...parts) {
  const out = new Uint8Array(parts.reduce((n, part) => n + part.length, 0));
  let offset = 0;
  for (const part of parts) { out.set(part, offset); offset += part.length; }
  return out;
}

function base64url(bytes) {
  let binary = '';
  bytes.forEach((value) => { binary += String.fromCharCode(value); });
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
}

export async function identityPinFragment(kind, genesisId, salt) {
  if (!['personal', 'persona', 'organization'].includes(kind)) throw new Error('invalid link binding kind');
  if (!(salt instanceof Uint8Array) || salt.length !== 16) throw new Error('invalid identity-pin salt');
  const input = concat(DOMAIN, new TextEncoder().encode(`${kind}\0`), hexBytes(genesisId), salt);
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', input));
  return `ac=${base64url(digest.slice(0, 16))}`;
}
