#!/usr/bin/env node

import fs from 'node:fs';
import { webcrypto } from 'node:crypto';
import { pathToFileURL } from 'node:url';

import { openArmorWithPassword } from '../root-factor-policy.js';
import {
  canonicalJson,
} from '../primitives.js';
import {
  buildEvent,
  derivePersona,
  signEvent,
} from '../ledger-event.js';
import {
  buildInviteBody,
  generateBearerToken,
} from '../invitation.js';

if (!globalThis.crypto) {
  globalThis.crypto = webcrypto;
}

const D15_DEFERRAL = (
  'key-bound invitation issuance is deferred by D15 until the registry '
  + 'can resolve personal identities'
);

function requiredValue(argv, index, option) {
  const value = argv[index + 1];
  if (value === undefined || value.startsWith('--')) {
    throw new Error(`${option} requires a value`);
  }
  return value;
}

function parseInteger(value, option) {
  if (!/^[0-9]+$/.test(value)) {
    throw new Error(`${option} must be a non-negative integer`);
  }
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed)) {
    throw new Error(`${option} exceeds JavaScript's safe integer range`);
  }
  return parsed;
}

function parseArguments(argv) {
  if (argv[0] !== 'issue-invitation') {
    throw new Error('expected issue-invitation command');
  }
  const options = {
    serverUrl: null,
    org: null,
    role: null,
    binding: 'bearer',
    expiry: null,
    ttlSeconds: null,
    personalArmorPath: null,
    personalPassphraseFd: null,
  };

  for (let index = 1; index < argv.length; index += 1) {
    const option = argv[index];
    if (option === '--server') {
      options.serverUrl = requiredValue(argv, index, option);
      index += 1;
    } else if (option === '--org') {
      options.org = requiredValue(argv, index, option);
      index += 1;
    } else if (option === '--role') {
      options.role = requiredValue(argv, index, option);
      index += 1;
    } else if (option === '--binding') {
      options.binding = requiredValue(argv, index, option);
      index += 1;
    } else if (option === '--expiry') {
      options.expiry = parseInteger(
        requiredValue(argv, index, option),
        option,
      );
      index += 1;
    } else if (option === '--ttl-seconds') {
      options.ttlSeconds = parseInteger(
        requiredValue(argv, index, option),
        option,
      );
      index += 1;
    } else if (option === '--personal-armor') {
      options.personalArmorPath = requiredValue(argv, index, option);
      index += 1;
    } else if (option === '--personal-passphrase-fd') {
      options.personalPassphraseFd = parseInteger(
        requiredValue(argv, index, option),
        option,
      );
      index += 1;
    } else {
      throw new Error(`unknown argument: ${option}`);
    }
  }

  if (options.binding !== 'bearer') {
    if (options.binding === 'key') throw new Error(D15_DEFERRAL);
    throw new Error('--binding currently accepts only bearer');
  }
  if (!options.serverUrl) throw new Error('--server is required');
  if (!options.org) throw new Error('--org is required');
  if (!options.role) throw new Error('--role is required');
  if (!options.personalArmorPath) {
    throw new Error('--personal-armor is required');
  }
  if ((options.expiry === null) === (options.ttlSeconds === null)) {
    throw new Error('supply exactly one of --expiry or --ttl-seconds');
  }
  if (options.ttlSeconds !== null && options.ttlSeconds < 1) {
    throw new Error('--ttl-seconds must be at least 1');
  }

  const server = new URL(options.serverUrl);
  if (server.protocol !== 'http:' && server.protocol !== 'https:') {
    throw new Error('--server must be an http or https URL');
  }
  options.serverUrl = server.href.replace(/\/$/, '');
  return options;
}

function readPersonalPassphrase(options) {
  if (options.personalPassphraseFd !== null) {
    return fs.readFileSync(
      options.personalPassphraseFd,
      'utf8',
    ).replace(/\r?\n$/, '');
  }
  if (Object.hasOwn(process.env, 'AUTONOMY_PERSONAL_PASSPHRASE')) {
    return process.env.AUTONOMY_PERSONAL_PASSPHRASE;
  }
  throw new Error(
    'missing personal passphrase source: use --personal-passphrase-fd or '
    + 'AUTONOMY_PERSONAL_PASSPHRASE',
  );
}

async function responseBody(response) {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

async function fetchJson(fetchImpl, url, options, action) {
  const response = await fetchImpl(url, options);
  const result = await responseBody(response);
  if (!response.ok) {
    throw new Error(
      `${action} failed with ${response.status}: `
      + `${typeof result === 'string' ? result : JSON.stringify(result)}`,
    );
  }
  return result;
}

async function issueInvitation({
  serverUrl,
  org,
  role,
  personalArmor,
  personalPassphrase,
  expiry = null,
  ttlSeconds = null,
  maxUses = null,
  nowMs = Date.now(),
  fetchImpl = globalThis.fetch,
}) {
  if (typeof fetchImpl !== 'function') {
    throw new Error('invitation issuance requires a fetch implementation');
  }
  if (!Number.isSafeInteger(nowMs) || nowMs < 0) {
    throw new Error('nowMs must be a non-negative safe integer');
  }
  if ((expiry === null) === (ttlSeconds === null)) {
    throw new Error('supply exactly one of expiry or ttlSeconds');
  }
  const resolvedExpiry = expiry === null
    ? nowMs + ttlSeconds * 1000
    : expiry;
  if (!Number.isSafeInteger(resolvedExpiry) || resolvedExpiry <= nowMs) {
    throw new Error('invitation expiry must be a future unix-ms timestamp');
  }

  const dashboard = new URL(serverUrl);
  const headers = { 'X-Graph-Org': org };
  const heads = await fetchJson(
    fetchImpl,
    new URL(
      `/api/network/ledger/heads?org=${encodeURIComponent(org)}`,
      dashboard,
    ),
    { headers },
    'loading authority-ledger heads',
  );
  if (
    !heads
    || !/^[0-9a-f]{64}$/.test(heads.genesis_id)
    || !Array.isArray(heads.heads)
    || heads.heads.some((head) => !/^[0-9a-f]{64}$/.test(head))
  ) {
    throw new Error('authority-ledger heads response is malformed');
  }

  const opened = await openArmorWithPassword(personalArmor, personalPassphrase);
  let persona;
  try {
    persona = await derivePersona(opened.seed, heads.genesis_id);
  } finally {
    opened.seed.fill(0);
    opened.seed = null;
  }

  const { token, tokenHash } = await generateBearerToken();
  const body = buildInviteBody({
    grantedRole: role,
    expiry: resolvedExpiry,
    sponsorPub: persona.publicHex,
    tokenHash,
    maxUses,
  });
  const event = await signEvent(buildEvent({
    authorKey: persona.publicHex,
    parents: heads.heads,
    hlc: [nowMs, 0],
    payload: body,
  }), persona.signingKey);
  persona = null;

  const posted = await fetchJson(
    fetchImpl,
    new URL('/api/network/ledger/invite', dashboard),
    {
      method: 'POST',
      headers: {
        ...headers,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        org,
        event: canonicalJson(event),
      }),
    },
    'invitation append',
  );
  if (!posted || typeof posted.invite_id !== 'string') {
    throw new Error('invitation append returned a malformed response');
  }
  return {
    invite_id: posted.invite_id,
    token,
    binding: 'bearer',
    role,
    expiry: resolvedExpiry,
    sponsor: event.author_key,
  };
}

async function main(argv) {
  const options = parseArguments(argv);
  // Resolve both secrets before the first request. A missing source therefore
  // has zero network effects, and the passphrase never appears in argv.
  const personalPassphrase = readPersonalPassphrase(options);
  const personalArmor = fs.readFileSync(options.personalArmorPath, 'utf8');
  const result = await issueInvitation({
    serverUrl: options.serverUrl,
    org: options.org,
    role: options.role,
    personalArmor,
    personalPassphrase,
    expiry: options.expiry,
    ttlSeconds: options.ttlSeconds,
  });
  process.stdout.write(`${JSON.stringify(result)}\n`);
  return result;
}

const invokedPath = process.argv[1]
  ? pathToFileURL(process.argv[1]).href
  : null;
if (invokedPath === import.meta.url) {
  main(process.argv.slice(2)).catch((error) => {
    process.stderr.write(
      `membership-commands: ${error.message || error}\n`,
    );
    process.exitCode = 1;
  });
}

export {
  issueInvitation,
  main,
};
