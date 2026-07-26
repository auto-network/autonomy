#!/usr/bin/env node

import fs from 'node:fs';
import { webcrypto } from 'node:crypto';
import { pathToFileURL } from 'node:url';

import { createNodeStorage } from '../storage.js';
import {
  configure,
  signOn,
  signRegistryRequest,
} from '../../network-signon.js';

if (!globalThis.crypto) {
  globalThis.crypto = webcrypto;
}

const DEFAULT_TARGET_TYPE = 'present';
const LINK_PATH = '/v1/links';

function requiredValue(argv, index, option) {
  const value = argv[index + 1];
  if (value === undefined || value.startsWith('--')) {
    throw new Error(`${option} requires a value`);
  }
  return value;
}

function parseArguments(argv) {
  const options = {
    server: null,
    org: null,
    target: null,
    ttlSeconds: null,
    passphraseFd: null,
  };

  for (let index = 0; index < argv.length; index += 1) {
    const option = argv[index];
    if (option === '--server') {
      options.server = requiredValue(argv, index, option);
      index += 1;
    } else if (option === '--org') {
      options.org = requiredValue(argv, index, option);
      index += 1;
    } else if (option === '--target') {
      options.target = requiredValue(argv, index, option);
      index += 1;
    } else if (option === '--ttl') {
      const value = requiredValue(argv, index, option);
      if (!/^[0-9]+$/.test(value)) {
        throw new Error('--ttl must be an integer number of seconds');
      }
      options.ttlSeconds = Number(value);
      index += 1;
    } else if (option === '--passphrase-fd') {
      const value = requiredValue(argv, index, option);
      if (!/^[0-9]+$/.test(value)) {
        throw new Error('--passphrase-fd must be a non-negative integer');
      }
      options.passphraseFd = Number(value);
      index += 1;
    } else {
      throw new Error(`unknown argument: ${option}`);
    }
  }

  if (!options.server) throw new Error('--server is required');
  if (!options.org) throw new Error('--org is required');

  const server = new URL(options.server);
  if (server.protocol !== 'http:' && server.protocol !== 'https:') {
    throw new Error('--server must be an http or https URL');
  }
  options.server = server.href.replace(/\/$/, '');
  options.target = options.target || crypto.randomUUID();
  return options;
}

function readPassphrase(options) {
  if (options.passphraseFd !== null) {
    return fs.readFileSync(options.passphraseFd, 'utf8').replace(/\r?\n$/, '');
  }
  if (Object.hasOwn(process.env, 'AUTONOMY_ORG_PASSPHRASE')) {
    return process.env.AUTONOMY_ORG_PASSPHRASE;
  }
  throw new Error(
    'missing passphrase source: use --passphrase-fd or '
    + 'AUTONOMY_ORG_PASSPHRASE',
  );
}

function dashboardTransport(server) {
  return {
    async fetch(url, options) {
      const absoluteUrl = new URL(String(url), `${server}/`);
      return globalThis.fetch(absoluteUrl, options);
    },
  };
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

async function main(argv) {
  const options = parseArguments(argv);
  // Resolve the secret before configuring adapters or touching either live
  // server. Missing input therefore cannot mint or submit anything.
  const passphrase = readPassphrase(options);
  const storage = createNodeStorage();

  await configure({
    storage,
    transport: dashboardTransport(options.server),
  });

  const signOnOptions = { org: options.org };
  if (options.ttlSeconds !== null) {
    signOnOptions.ttlSeconds = options.ttlSeconds;
  }
  await signOn(passphrase, signOnOptions);

  const session = await storage.getSession();
  if (!session || !session.key || !session.registryUrl || !session.org) {
    throw new Error('sign-on did not install a live bound session');
  }
  const payload = {
    org: session.org,
    target_uuid: options.target,
    target_type: DEFAULT_TARGET_TYPE,
  };
  const envelope = await signRegistryRequest('POST', LINK_PATH, payload);
  const registryResponse = await globalThis.fetch(
    new URL(LINK_PATH, `${session.registryUrl.replace(/\/$/, '')}/`),
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(envelope),
    },
  );
  const registryResult = await responseBody(registryResponse);
  if (!registryResponse.ok) {
    throw new Error(
      `registry request failed with ${registryResponse.status}: `
      + `${typeof registryResult === 'string'
        ? registryResult
        : JSON.stringify(registryResult)}`,
    );
  }

  const cert = JSON.parse(session.certWire);
  const output = {
    ok: true,
    status: registryResponse.status,
    signOn: {
      orgSlug: options.org,
      org: session.org,
      registryUrl: session.registryUrl,
      rootPub: session.rootPub,
      sessionPub: cert.child_pub,
      subject: cert.subject,
      scope: cert.scope,
      notAfter: cert.not_after,
    },
    request: {
      method: 'POST',
      path: LINK_PATH,
      payload,
    },
    envelope,
    registry: registryResult,
  };
  process.stdout.write(`${JSON.stringify(output)}\n`);
  return output;
}

const invokedPath = process.argv[1]
  ? pathToFileURL(process.argv[1]).href
  : null;
if (invokedPath === import.meta.url) {
  main(process.argv.slice(2)).catch((error) => {
    process.stderr.write(`signon: ${error.message || error}\n`);
    process.exitCode = 1;
  });
}

export {
  main,
};
