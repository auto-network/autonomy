/* Gather the factor seed(s) for one vault_open approval sheet.
 *
 * This module owns no dialog. The generic approval sheet displays the frozen
 * action and password input; its one primary button calls this gatherer. For a
 * passkey policy that same button performs WebAuthn and authorizes delivery —
 * there is no generic approval followed by a second decrypt dialog.
 */
import * as primitives from './primitives.js';
import { prfEvalExtension, prfOutputFromResults } from './enrollment.js';
import { openRootAnchorEnvelope } from './root-anchor.js';

function b64uToBytes(value) {
  let base64 = value.replace(/-/g, '+').replace(/_/g, '/');
  while (base64.length % 4) base64 += '=';
  const binary = atob(base64);
  return Uint8Array.from(binary, (ch) => ch.charCodeAt(0));
}

function bytesToB64u(value) {
  let binary = '';
  for (const byte of new Uint8Array(value)) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
}

function bytesToHex(value) {
  return Array.from(new Uint8Array(value))
    .map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

async function passwordSeed(factors, password, decryptArmor) {
  if (!password) throw new Error('Enter your vault password.');
  let lastError = null;
  for (const factor of factors.filter((item) => item.type === 'password')) {
    try {
      const opened = await decryptArmor(factor.armor, password);
      return { factorId: factor.factor_id, seed: opened.seed };
    } catch (error) {
      lastError = error;
    }
  }
  throw new Error(
    (lastError && lastError.message) || 'That password did not open this vault class.',
  );
}

function rpMatchesHost(rpId, hostname) {
  return !rpId || rpId === hostname || hostname.endsWith(`.${rpId}`);
}

async function passkeySeed(factors, credentials, cryptoApi, currentHostname) {
  if (!credentials || typeof credentials.get !== 'function') {
    throw new Error('This browser cannot use a passkey.');
  }
  const passkeys = factors.filter(
    (item) => item.type === 'passkey' && rpMatchesHost(item.rp_id, currentHostname),
  );
  if (!passkeys.length) throw new Error('This vault class has no passkey factor.');
  const rpIds = [...new Set(passkeys.map((item) => item.rp_id).filter(Boolean))];
  if (rpIds.length > 1) throw new Error('This vault class spans incompatible passkey sites.');
  let assertion;
  try {
    assertion = await credentials.get({
      publicKey: {
        challenge: cryptoApi.getRandomValues(new Uint8Array(32)),
        ...(rpIds[0] ? { rpId: rpIds[0] } : {}),
        allowCredentials: passkeys.map((item) => ({
          type: 'public-key',
          id: b64uToBytes(item.credential_id),
          ...(Array.isArray(item.transports) && item.transports.length
            ? { transports: item.transports } : {}),
        })),
        userVerification: 'required',
        extensions: prfEvalExtension(),
      },
    });
  } catch (error) {
    if (error && error.name === 'NotAllowedError') {
      throw new Error('Passkey was cancelled — try again.');
    }
    throw error;
  }
  const credentialId = bytesToB64u(assertion.rawId);
  const factor = passkeys.find((item) => item.credential_id === credentialId);
  if (!factor) throw new Error('The passkey result was not one of the frozen factors.');
  const seed = prfOutputFromResults(assertion.getClientExtensionResults());
  if (!seed || seed.length !== 32) {
    throw new Error('This passkey did not return the vault PRF output.');
  }
  return { factorId: factor.factor_id, seed };
}

async function rootPasskeyPrf(root, credentials, cryptoApi, currentHostname) {
  if (!credentials || typeof credentials.get !== 'function') {
    throw new Error('This browser cannot use a passkey.');
  }
  const passkeys = (root.passkeys || []).filter(
    (item) => item.credential_id && rpMatchesHost(item.rp_id, currentHostname),
  );
  if (!passkeys.length) throw new Error('No personal-root passkey works on this site.');
  const rpIds = [...new Set(passkeys.map((item) => item.rp_id).filter(Boolean))];
  if (rpIds.length > 1) throw new Error('Your root passkeys span incompatible sites.');
  let assertion;
  try {
    assertion = await credentials.get({
      publicKey: {
        challenge: cryptoApi.getRandomValues(new Uint8Array(32)),
        ...(rpIds[0] ? { rpId: rpIds[0] } : {}),
        allowCredentials: passkeys.map((item) => ({
          type: 'public-key',
          id: b64uToBytes(item.credential_id),
          ...(Array.isArray(item.transports) && item.transports.length
            ? { transports: item.transports } : {}),
        })),
        userVerification: 'required',
        extensions: prfEvalExtension(),
      },
    });
  } catch (error) {
    if (error && error.name === 'NotAllowedError') {
      throw new Error('Passkey was cancelled — try again.');
    }
    throw error;
  }
  const credentialId = bytesToB64u(assertion.rawId);
  if (!passkeys.some((item) => item.credential_id === credentialId)) {
    throw new Error('The passkey result was not one of your frozen root passkeys.');
  }
  const prf = prfOutputFromResults(assertion.getClientExtensionResults());
  if (!prf || prf.length !== 32) {
    throw new Error('This passkey did not return the root PRF output.');
  }
  return prf;
}

async function gatherRootAnchor(ceremony, password, dependencies) {
  const { root, anchor, governance } = ceremony;
  if (!root || !anchor || !governance || governance.form !== 'root-reachable'
      || governance.anchor_id !== anchor.anchor_id
      || root.root_pub !== anchor.root_pub
      || !Array.isArray(root.methods) || !root.methods.length) {
    throw new Error('This approval has no valid personal-root ceremony.');
  }
  const method = dependencies.rootMethod
    || (root.methods.length === 1 ? root.methods[0] : null);
  if (!method || !root.methods.includes(method)) {
    throw new Error('Choose how to open your personal root.');
  }
  let prf = null;
  let opened = null;
  try {
    if (method === 'passkey' || method === 'both') {
      prf = await rootPasskeyPrf(
        root,
        dependencies.credentials,
        dependencies.cryptoApi,
        dependencies.currentHostname,
      );
    }
    if (method === 'both') {
      if (!password) throw new Error('Enter your personal password.');
      opened = await dependencies.decryptArmorWithCombined(root.armor, password, prf);
    } else if (method === 'passkey') {
      opened = await dependencies.decryptArmorWithPasskey(root.armor, prf);
    } else if (method === 'password') {
      if (!password) throw new Error('Enter your personal password.');
      opened = await dependencies.decryptArmor(root.armor, password);
    } else {
      throw new Error('This personal-root opener is unsupported.');
    }
    if (opened.rootPub && opened.rootPub !== root.root_pub) {
      throw new Error('The opened root does not match the frozen vault anchor.');
    }
    const anchorSeed = await dependencies.openAnchor(anchor, opened.seed);
    return {
      openers: { [anchor.anchor_id]: bytesToHex(anchorSeed) },
      seeds: [anchorSeed],
    };
  } finally {
    if (prf) prf.fill(0);
    if (opened && opened.seed) opened.seed.fill(0);
  }
}

export async function gatherVaultOpeners(
  ceremony,
  password,
  {
    decryptArmor = primitives.decryptArmor,
    decryptArmorWithPasskey = primitives.decryptArmorWithPasskey,
    decryptArmorWithCombined = primitives.decryptArmorWithCombined,
    openAnchor = openRootAnchorEnvelope,
    credentials = globalThis.navigator && globalThis.navigator.credentials,
    cryptoApi = globalThis.crypto,
    currentHostname = globalThis.location && globalThis.location.hostname,
    rootMethod = null,
  } = {},
) {
  const dependencies = {
    decryptArmor,
    decryptArmorWithPasskey,
    decryptArmorWithCombined,
    openAnchor,
    credentials,
    cryptoApi,
    currentHostname,
    rootMethod,
  };
  if (ceremony && ceremony.v === 2) {
    return gatherRootAnchor(ceremony, password, dependencies);
  }
  if (!ceremony || ceremony.v !== 1 || !Array.isArray(ceremony.factors)) {
    throw new Error('This approval has no valid vault ceremony.');
  }
  if (!['password', 'prf', 'both'].includes(ceremony.policy)) {
    throw new Error('This approval names an unsupported vault policy.');
  }
  const seeds = [];
  const openers = {};
  try {
    if (ceremony.policy === 'password' || ceremony.policy === 'both') {
      const opened = await passwordSeed(ceremony.factors, password, decryptArmor);
      seeds.push(opened.seed);
      openers[opened.factorId] = bytesToHex(opened.seed);
    }
    if (ceremony.policy === 'prf' || ceremony.policy === 'both') {
      const opened = await passkeySeed(
        ceremony.factors, credentials, cryptoApi, currentHostname,
      );
      seeds.push(opened.seed);
      openers[opened.factorId] = bytesToHex(opened.seed);
    }
    return { openers, seeds };
  } catch (error) {
    for (const seed of seeds) seed.fill(0);
    for (const factorId of Object.keys(openers)) openers[factorId] = '';
    throw error;
  }
}

export function clearVaultOpeners(gathered) {
  if (!gathered) return;
  for (const seed of gathered.seeds || []) seed.fill(0);
  for (const factorId of Object.keys(gathered.openers || {})) {
    gathered.openers[factorId] = '';
  }
}

export default gatherVaultOpeners;
