#!/usr/bin/env node

/*
 * Headless personal sign-on (§21: the full path via CLI/API, no browser).
 *
 * One passphrase opens the personal root armor once; the ceremony derives a
 * persona per organization from that single unlock, evaluates the
 * opportunistic re-key interval per organization, and prints what it did.
 * No organization root key is fetched or decrypted.
 *
 * `--publish` is a separate, opt-in step that exercises the request-signing
 * seam against a registry using one named organization's persona authority.
 */

import fs from 'node:fs';
import { webcrypto } from 'node:crypto';
import { pathToFileURL } from 'node:url';

import { createNodeStorage } from '../storage.js';
import {
  configure,
  signOn,
  signRegistryRequest,
} from '../../network-signon.mjs';

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
    orgs: [],
    publish: null,
    target: null,
    ttlSeconds: null,
    passphraseFd: null,
    rekeyEndpoint: null,
    migrateLegacyOrgKeys: false,
  };

  for (let index = 0; index < argv.length; index += 1) {
    const option = argv[index];
    if (option === '--server') {
      options.server = requiredValue(argv, index, option);
      index += 1;
    } else if (option === '--org') {
      // Repeatable: restricts the unlock to the named organizations.
      // Omitted entirely, sign-on covers every organization this node knows.
      options.orgs.push(requiredValue(argv, index, option));
      index += 1;
    } else if (option === '--publish') {
      options.publish = requiredValue(argv, index, option);
      index += 1;
    } else if (option === '--rekey-endpoint') {
      options.rekeyEndpoint = requiredValue(argv, index, option);
      index += 1;
    } else if (option === '--target') {
      options.target = requiredValue(argv, index, option);
      index += 1;
    } else if (option === '--migrate-legacy-org-keys') {
      options.migrateLegacyOrgKeys = true;
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
  // The passphrase is PERSONAL now — it opens the personal root armor, not
  // an organization's key. The organization-flavoured name is still accepted
  // so existing headless callers keep working.
  for (const name of ['AUTONOMY_PERSONAL_PASSPHRASE', 'AUTONOMY_ORG_PASSPHRASE']) {
    if (Object.hasOwn(process.env, name)) return process.env[name];
  }
  throw new Error(
    'missing passphrase source: use --passphrase-fd or '
    + 'AUTONOMY_PERSONAL_PASSPHRASE',
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

/*
 * The §1d re-key executor seam. Sign-on decides PER ORGANIZATION whether the
 * opportunistic interval has elapsed and calls this only for the ones where
 * it has; the real record-writing flow is auto-biqme's. Installing it is
 * opt-in (--rekey-endpoint) so a run without one still reports every
 * per-organization decision without inventing a side effect.
 */
function rekeyAdapter(server, endpoint) {
  return async function rekey(request) {
    const response = await globalThis.fetch(new URL(endpoint, `${server}/`), {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(request.orgSlug ? { 'X-Graph-Org': request.orgSlug } : {}),
      },
      body: JSON.stringify({
        org: request.orgSlug,
        genesis_id: request.genesisId,
        persona_pub: request.personaPub,
        reason: request.reason,
      }),
    });
    if (!response.ok) {
      throw new Error(`re-key was refused (${response.status})`);
    }
    return await response.json().catch(() => ({}));
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

async function publishLink(options, storage, orgRef) {
  const session = await storage.getSession();
  const entries = Object.values((session && session.orgs) || {});
  const entry = entries.find(
    (row) => row.orgSlug === orgRef || row.genesisId === orgRef
      || row.org === orgRef,
  );
  if (!entry) {
    throw new Error(`sign-on carries no authority for ${orgRef}`);
  }
  if (!entry.registryUrl) {
    throw new Error(`${orgRef} is not bound to a registry`);
  }
  const payload = {
    org: entry.org,
    target_uuid: options.target,
    target_type: DEFAULT_TARGET_TYPE,
  };
  const envelope = await signRegistryRequest(
    'POST', LINK_PATH, payload, { org: orgRef },
  );
  const response = await globalThis.fetch(
    new URL(LINK_PATH, `${entry.registryUrl.replace(/\/$/, '')}/`),
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(envelope),
    },
  );
  return {
    org: entry.orgSlug,
    status: response.status,
    request: { method: 'POST', path: LINK_PATH, payload },
    envelope,
    registry: await responseBody(response),
  };
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
    rekey: options.rekeyEndpoint
      ? rekeyAdapter(options.server, options.rekeyEndpoint)
      : null,
  });

  const signOnOptions = {};
  if (options.orgs.length) signOnOptions.orgs = options.orgs;
  if (options.ttlSeconds !== null) {
    signOnOptions.ttlSeconds = options.ttlSeconds;
  }
  if (options.migrateLegacyOrgKeys) {
    signOnOptions.migrateLegacyOrgKeys = true;
  }
  const result = await signOn(passphrase, signOnOptions);

  const session = await storage.getSession();
  if (!session || !session.key || !session.orgs
      || !Object.keys(session.orgs).length) {
    throw new Error('sign-on did not install a personal session');
  }

  const output = {
    ok: true,
    signOn: {
      personalRootPub: result.personalRootPub,
      sessionPub: result.sessionPub,
      orgs: result.orgs.map((org) => {
        const cert = JSON.parse(session.orgs[org.genesisId].certWire);
        return {
          orgSlug: org.orgSlug,
          genesisId: org.genesisId,
          org: org.org,
          personaPub: org.personaPub,
          registryUrl: org.registryUrl,
          subject: cert.subject,
          scope: cert.scope,
          notAfter: cert.not_after,
          certWire: session.orgs[org.genesisId].certWire,
          rekey: org.rekey,
        };
      }),
      skipped: result.skipped,
      diagnostics: result.diagnostics,
    },
  };
  if (options.publish) {
    output.publish = await publishLink(options, storage, options.publish);
  }
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
