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
import { decryptArmor } from '../primitives.js';

if (!globalThis.crypto) {
  globalThis.crypto = webcrypto;
}

const FOUNDING_BLOCKER = 'auto-5dh9a';
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
    recoveryPolicy: 'none',
    registryUrl: null,
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
    } else if (option === '--recovery') {
      options.recoveryPolicy = requiredValue(argv, index, option);
      index += 1;
    } else if (option === '--registry') {
      options.registryUrl = requiredValue(argv, index, option);
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
  registryUrl,
  recoveryPolicy = 'none',
  fetchImpl = globalThis.fetch,
}) {
  if (!orgArmorPath) {
    throw new Error(
      'create-organization requires --org-armor from create-org-identity',
    );
  }
  if (!registryUrl) {
    throw new Error(
      'create-organization requires --registry or AUTONOMY_REGISTRY_URL',
    );
  }
  if (recoveryPolicy !== 'none' && recoveryPolicy !== 'recovery-key') {
    throw new Error('--recovery must be none or recovery-key');
  }

  const armor = fs.readFileSync(orgArmorPath, 'utf8');
  const opened = await decryptArmor(armor, passphrase);
  let rootSigningKey;
  try {
    rootSigningKey = await importEd25519RootSigningKey(opened.seed);
  } finally {
    opened.seed.fill(0);
    opened.seed = null;
  }

  let recoveryPair = null;
  let recoverySeedHex = null;
  try {
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
      founding: {
        status: 'blocked',
        blocked_on: FOUNDING_BLOCKER,
      },
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
    const registryUrl = options.registryUrl
      || process.env.AUTONOMY_REGISTRY_URL
      || null;
    output = await createOrganization({
      orgArmorPath: options.orgArmor,
      passphrase,
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
