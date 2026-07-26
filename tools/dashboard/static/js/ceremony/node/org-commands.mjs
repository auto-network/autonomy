#!/usr/bin/env node

import fs from 'node:fs';
import path from 'node:path';
import { webcrypto } from 'node:crypto';
import { pathToFileURL } from 'node:url';

import {
  armorSeed,
  buildRecoveryBlock,
  buildRegistrationEnvelope,
  generateOrgRootKey,
  importEd25519RootSigningKey,
} from '../organization.js';
import { foundOrganization } from '../founding.js';
import { decryptArmor } from '../primitives.js';

if (!globalThis.crypto) {
  globalThis.crypto = webcrypto;
}

const REGISTRY_WIRING_BLOCKER = 'N24';

function requiredValue(argv, index, option) {
  const value = argv[index + 1];
  if (value === undefined || value.startsWith('--')) {
    throw new Error(`${option} requires a value`);
  }
  return value;
}

function parseArguments(argv) {
  const command = argv[0];
  if (command !== 'create-org-identity' && command !== 'create-organization') {
    throw new Error(
      'expected create-org-identity or create-organization command',
    );
  }
  const options = {
    command,
    armorOutput: null,
    orgArmor: null,
    passphraseFd: null,
    personalArmor: null,
    personalPassphraseFd: null,
    recoveryPolicy: 'none',
    registryUrl: null,
    serverUrl: null,
    org: null,
  };
  for (let index = 1; index < argv.length; index += 1) {
    const option = argv[index];
    if (option === '--armor-output') {
      options.armorOutput = requiredValue(argv, index, option);
      index += 1;
    } else if (option === '--org-armor') {
      options.orgArmor = requiredValue(argv, index, option);
      index += 1;
    } else if (option === '--passphrase-fd') {
      const value = requiredValue(argv, index, option);
      if (!/^[0-9]+$/.test(value)) {
        throw new Error('--passphrase-fd must be a non-negative integer');
      }
      options.passphraseFd = Number(value);
      index += 1;
    } else if (option === '--personal-armor') {
      options.personalArmor = requiredValue(argv, index, option);
      index += 1;
    } else if (option === '--personal-passphrase-fd') {
      const value = requiredValue(argv, index, option);
      if (!/^[0-9]+$/.test(value)) {
        throw new Error(
          '--personal-passphrase-fd must be a non-negative integer',
        );
      }
      options.personalPassphraseFd = Number(value);
      index += 1;
    } else if (option === '--recovery') {
      options.recoveryPolicy = requiredValue(argv, index, option);
      index += 1;
    } else if (option === '--registry') {
      options.registryUrl = requiredValue(argv, index, option);
      index += 1;
    } else if (option === '--server') {
      options.serverUrl = requiredValue(argv, index, option);
      index += 1;
    } else if (option === '--org') {
      options.org = requiredValue(argv, index, option);
      index += 1;
    } else {
      throw new Error(`unknown argument: ${option}`);
    }
  }
  return options;
}

function readOrgPassphrase(options) {
  if (options.passphraseFd !== null) {
    return fs.readFileSync(options.passphraseFd, 'utf8').replace(/\r?\n$/, '');
  }
  if (Object.hasOwn(process.env, 'AUTONOMY_ORG_PASSPHRASE')) {
    return process.env.AUTONOMY_ORG_PASSPHRASE;
  }
  throw new Error(
    'missing org passphrase source: use --passphrase-fd or '
    + 'AUTONOMY_ORG_PASSPHRASE',
  );
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

async function createOrgIdentity({
  passphrase,
  armorOutput = null,
  iterations,
}) {
  if (typeof passphrase !== 'string' || passphrase.length < 8) {
    throw new Error('the org passphrase must be at least 8 characters');
  }
  const pair = await generateOrgRootKey();
  try {
    const armor = await armorSeed(
      pair.seed,
      pair.pubHex,
      passphrase,
      iterations,
    );
    if (armorOutput) {
      const outputPath = path.resolve(armorOutput);
      fs.writeFileSync(outputPath, `${armor}\n`, {
        encoding: 'utf8',
        flag: 'wx',
        mode: 0o600,
      });
      return {
        root_pub: pair.pubHex,
        armor_path: outputPath,
      };
    }
    return {
      root_pub: pair.pubHex,
      armor,
    };
  } finally {
    pair.seed.fill(0);
    pair.seed = null;
  }
}

async function createOrganization({
  orgArmorPath,
  passphrase,
  personalArmorPath,
  personalPassphrase,
  serverUrl,
  org,
  registryUrl,
  recoveryPolicy = 'none',
  fetchImpl = globalThis.fetch,
}) {
  if (!orgArmorPath) {
    throw new Error(
      'create-organization requires --org-armor from create-org-identity',
    );
  }
  if (!personalArmorPath) {
    throw new Error(
      'create-organization requires --personal-armor for founder authority',
    );
  }
  if (!serverUrl) {
    throw new Error('create-organization requires --server');
  }
  if (!org) {
    throw new Error('create-organization requires --org');
  }
  if (recoveryPolicy !== 'none' && recoveryPolicy !== 'recovery-key') {
    throw new Error('--recovery must be none or recovery-key');
  }
  if (recoveryPolicy === 'recovery-key' && !registryUrl) {
    throw new Error('--recovery recovery-key requires --registry');
  }
  if (typeof fetchImpl !== 'function') {
    throw new Error('create-organization requires a fetch implementation');
  }

  const dashboard = new URL(serverUrl);
  if (dashboard.protocol !== 'http:' && dashboard.protocol !== 'https:') {
    throw new Error('server URL must use http or https');
  }
  const orgResponse = await fetchImpl(
    new URL(`/api/orgs/${encodeURIComponent(org)}`, dashboard),
    { headers: { 'X-Graph-Org': org } },
  );
  const orgResult = await responseBody(orgResponse);
  if (
    !orgResponse.ok
    || !orgResult
    || typeof orgResult.org?.id !== 'string'
    || orgResult.org.slug !== org
  ) {
    throw new Error(
      'create-organization requires an existing local org database '
      + `(preflight ${orgResponse.status}: `
      + `${typeof orgResult === 'string'
        ? orgResult
        : JSON.stringify(orgResult)})`,
    );
  }

  const armor = fs.readFileSync(orgArmorPath, 'utf8');
  const personalArmor = fs.readFileSync(personalArmorPath, 'utf8');
  const opened = await decryptArmor(armor, passphrase);
  let personalOpened;
  try {
    personalOpened = await decryptArmor(
      personalArmor,
      personalPassphrase,
    );
  } catch (error) {
    opened.seed.fill(0);
    opened.seed = null;
    throw error;
  }
  let rootSigningKey = null;
  try {
    rootSigningKey = await importEd25519RootSigningKey(opened.seed);
  } catch (error) {
    personalOpened.seed.fill(0);
    personalOpened.seed = null;
    throw error;
  } finally {
    opened.seed.fill(0);
    opened.seed = null;
  }

  let recoveryPair = null;
  let recoverySeedHex = null;
  try {
    const founded = await foundOrganization({
      org,
      orgId: orgResult.org.id,
      rootPub: opened.rootPub,
      rootSigningKey,
      personalRootSeed: personalOpened.seed,
      now: Date.now(),
      transport: {
        fetch(route, options = {}) {
          return fetchImpl(
            new URL(route, dashboard),
            {
              ...options,
              headers: {
                ...(options.headers || {}),
                'X-Graph-Org': org,
              },
            },
          );
        },
      },
    });
    const founding = {
      ok: true,
      genesis_id: founded.genesisId,
      founder_persona_pub: founded.founderPersonaPub,
      event_ids: founded.eventIds,
    };

    if (!registryUrl) {
      return {
        root_pub: opened.rootPub,
        founding,
        registration: null,
      };
    }

    const payload = {
      root_pub: opened.rootPub,
      recovery_policy: recoveryPolicy,
    };
    if (recoveryPolicy === 'recovery-key') {
      recoveryPair = await generateOrgRootKey();
      recoverySeedHex = Array.from(
        recoveryPair.seed,
        (byte) => (`0${byte.toString(16)}`).slice(-2),
      ).join('');
      payload.recovery_pub = recoveryPair.pubHex;
    }

    const envelope = await buildRegistrationEnvelope(
      rootSigningKey,
      opened.rootPub,
      payload,
    );
    const registry = new URL(registryUrl);
    if (registry.protocol !== 'http:' && registry.protocol !== 'https:') {
      throw new Error('registry URL must use http or https');
    }
    const response = await fetchImpl(
      new URL('/v1/orgs', registry),
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(envelope),
      },
    );
    const result = await responseBody(response);
    if (!response.ok) {
      throw new Error(
        `registry request failed with ${response.status}: `
        + `${typeof result === 'string' ? result : JSON.stringify(result)}`,
      );
    }
    if (
      !result
      || typeof result.org_uuid !== 'string'
      || result.root_pub !== opened.rootPub
    ) {
      throw new Error('registry returned an invalid D21 binding');
    }

    return {
      status: response.status,
      binding: result,
      envelope,
      recovery_block: recoveryPair
        ? buildRecoveryBlock(
          result.org_uuid,
          opened.rootPub,
          recoveryPair.pubHex,
          recoverySeedHex,
        )
        : null,
      founding,
      production_registry_wiring: {
        status: 'blocked',
        blocked_on: REGISTRY_WIRING_BLOCKER,
      },
    };
  } finally {
    if (recoveryPair) {
      recoveryPair.seed.fill(0);
      recoveryPair.seed = null;
    }
    recoverySeedHex = null;
    rootSigningKey = null;
    personalOpened.seed.fill(0);
    personalOpened.seed = null;
  }
}

async function main(argv) {
  const options = parseArguments(argv);
  const passphrase = readOrgPassphrase(options);
  let output;
  if (options.command === 'create-org-identity') {
    output = await createOrgIdentity({
      passphrase,
      armorOutput: options.armorOutput,
    });
  } else {
    const personalPassphrase = readPersonalPassphrase(options);
    const registryUrl = options.registryUrl
      || process.env.AUTONOMY_REGISTRY_URL
      || null;
    output = await createOrganization({
      orgArmorPath: options.orgArmor,
      passphrase,
      personalArmorPath: options.personalArmor,
      personalPassphrase,
      serverUrl: options.serverUrl,
      org: options.org,
      registryUrl,
      recoveryPolicy: options.recoveryPolicy,
    });
  }
  process.stdout.write(`${JSON.stringify(output)}\n`);
  return output;
}

const invokedPath = process.argv[1]
  ? pathToFileURL(process.argv[1]).href
  : null;
if (invokedPath === import.meta.url) {
  main(process.argv.slice(2)).catch((error) => {
    process.stderr.write(`org-commands: ${error.message || error}\n`);
    process.exitCode = 1;
  });
}

export {
  createOrgIdentity,
  createOrganization,
  main,
};
