/* Password wrap for vault factor seeds — the browser mirror of
 * tools/vault/password_wrap.py. Opens the envelope a vault password
 * factor's seed is stored in. This is not identity armor: it protects a
 * per-factor random seed, never a personal or organization root.
 *
 * THE WIRE FORMAT IS FROZEN: the BEGIN/END lines, the `v: 2` format tag,
 * and the AAD domain strings are exactly what stored vault_factors rows
 * carry. The "v2" in the domain strings names this envelope's lineage,
 * not the retired identity-armor generation.
 */

let webCrypto = globalThis.crypto;
if (
  (!webCrypto || !webCrypto.subtle)
  && typeof process !== 'undefined' && process.versions && process.versions.node
) {
  ({ webcrypto: webCrypto } = await import('node:crypto'));
}

const BEGIN = '-----BEGIN AUTONOMY NETWORK ROOT KEY-----';
const END = '-----END AUTONOMY NETWORK ROOT KEY-----';
const FACTOR_AAD = 'autonomy.idkit.armor.v2.factor\n';
const SEAL_AAD = 'autonomy.idkit.armor.v2.kek-seal\n';

const textEncoder = new TextEncoder();

function b64ToBytes(value) {
  const bin = atob(value);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
  return out;
}

function canonicalJson(value) {
  if (value === null || typeof value === 'number' || typeof value === 'boolean') {
    return JSON.stringify(value);
  }
  if (typeof value === 'string') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  const keys = Object.keys(value).sort();
  return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(value[k])}`).join(',')}}`;
}

async function sha256Hex(bytes) {
  const digest = new Uint8Array(await webCrypto.subtle.digest('SHA-256', bytes));
  return Array.from(digest).map((b) => b.toString(16).padStart(2, '0')).join('');
}

function parseWrap(envelope) {
  if (typeof envelope !== 'string') throw new Error('password wrap must be text');
  const lines = envelope.split('\n').map((l) => l.trim()).filter((l) => l.length);
  if (lines.length < 3 || lines[0] !== BEGIN || lines[lines.length - 1] !== END) {
    throw new Error('password wrap is missing its BEGIN/END lines');
  }
  let data;
  try {
    data = JSON.parse(new TextDecoder().decode(b64ToBytes(lines.slice(1, -1).join(''))));
  } catch (e) {
    throw new Error('password wrap body does not decode');
  }
  const keys = Object.keys(data).sort().join(',');
  if (keys !== 'factors,kek_seal,root_pub,v' || data.v !== 2) {
    throw new Error('unsupported password wrap format');
  }
  if (!Array.isArray(data.factors) || data.factors.length !== 1
      || data.factors[0].type !== 'password') {
    throw new Error('password wrap must carry exactly one password factor');
  }
  return data;
}

async function openPasswordWrap(envelope, password) {
  if (typeof password !== 'string' || !password) {
    throw new Error('password must be a non-empty string');
  }
  const data = parseWrap(envelope);
  const rootPub = data.root_pub;
  const pw = data.factors[0];
  const material = await webCrypto.subtle.importKey(
    'raw', textEncoder.encode(password), 'PBKDF2', false, ['deriveKey'],
  );
  const pwKey = await webCrypto.subtle.deriveKey(
    {
      name: 'PBKDF2', salt: b64ToBytes(pw.kdf.salt),
      iterations: pw.kdf.iterations, hash: 'SHA-256',
    },
    material, { name: 'AES-GCM', length: 256 }, false, ['decrypt'],
  );
  let masterKey;
  try {
    masterKey = new Uint8Array(await webCrypto.subtle.decrypt(
      {
        name: 'AES-GCM', iv: b64ToBytes(pw.iv),
        additionalData: textEncoder.encode(`${FACTOR_AAD}${rootPub}\npassword`),
      },
      pwKey, b64ToBytes(pw.wrap),
    ));
  } catch (e) {
    throw new Error('password wrap does not open with that password');
  }
  const commitmentItems = [{
    type: 'password', salt: pw.kdf.salt, iterations: pw.kdf.iterations,
  }].sort((a, b) => (canonicalJson(a) < canonicalJson(b) ? -1 : 1));
  const commitment = await sha256Hex(textEncoder.encode(canonicalJson(commitmentItems)));
  const sealKey = await webCrypto.subtle.importKey(
    'raw', masterKey, 'AES-GCM', false, ['decrypt'],
  );
  let seed;
  try {
    seed = new Uint8Array(await webCrypto.subtle.decrypt(
      {
        name: 'AES-GCM', iv: b64ToBytes(data.kek_seal.iv),
        additionalData: textEncoder.encode(`${SEAL_AAD}${rootPub}\n${commitment}`),
      },
      sealKey, b64ToBytes(data.kek_seal.ct),
    ));
  } catch (e) {
    throw new Error('password wrap seal does not open — the envelope may be altered');
  } finally {
    masterKey.fill(0);
  }
  if (seed.length !== 32) throw new Error('password wrap plaintext is not a 32-byte seed');
  return { seed, rootPub };
}

export { openPasswordWrap };
