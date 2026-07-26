import readline from 'node:readline';

import { VirtualAuthenticator } from '../authenticator-node.js';

const authenticator = new VirtualAuthenticator();
const input = readline.createInterface({
  input: process.stdin,
  crlfDelay: Infinity,
});

async function handle(message) {
  if (message.op === 'create') {
    return authenticator.createCredential(message.options);
  }
  if (message.op === 'get') {
    return authenticator.getAssertion(message.options);
  }
  if (message.op === 'setSignCount') {
    authenticator.setSignCount(
      message.credentialId,
      message.signCount,
    );
    return { ok: true };
  }
  throw new Error(`unknown driver operation ${message.op}`);
}

input.on('line', async (line) => {
  try {
    const message = JSON.parse(line);
    const result = await handle(message);
    process.stdout.write(`${JSON.stringify({ ok: true, result })}\n`);
  } catch (error) {
    process.stdout.write(`${JSON.stringify({
      ok: false,
      error: error.message || String(error),
    })}\n`);
  }
});
