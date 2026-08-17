/* The join channel factory: wsUrl derivation and the relaykit/core handshake
 * call shape, pinned to relay's frozen literals, proven with an injected core
 * so it runs before relaykit-core.js is served own-origin.
 * Run: node channel-factory.test.mjs
 */
import assert from 'node:assert/strict';

import { channelUrl, openChannel } from '../channel-factory.js';

const TOKEN = 'd'.repeat(32);
const ROOT = 'a'.repeat(64);

// 1. wsUrl is derived from the channelToken against the FIXED relay origin.
assert.equal(
  channelUrl(TOKEN),
  `wss://relay.auto.network/v1/links/${TOKEN}/channel`,
);

// 2. junk never becomes a URL.
assert.throws(() => channelUrl('not-a-token'), /32 hex/);

// 3. a test/config origin may override — but it is passed in, never derived
//    from the page origin.
assert.equal(
  channelUrl(TOKEN, { origin: 'wss://localhost:9443' }),
  `wss://localhost:9443/v1/links/${TOKEN}/channel`,
);

// 4. openChannel calls performHandshake(openSocket(wsUrl), {org, token, rootPub}).
{
  const calls = {};
  const core = {
    openSocket: (url) => {
      calls.url = url;
      return { sock: true };
    },
    performHandshake: (socket, params) => {
      calls.socket = socket;
      calls.params = params;
      return { channel: true };
    },
  };
  const inputs = { org: 'o', channelToken: TOKEN, rootPub: ROOT };
  const channel = await openChannel(inputs, { core });
  assert.equal(calls.url, `wss://relay.auto.network/v1/links/${TOKEN}/channel`);
  assert.deepEqual(calls.socket, { sock: true });
  assert.deepEqual(calls.params, { org: 'o', token: TOKEN, rootPub: ROOT });
  assert.deepEqual(channel, { channel: true });
}

console.log('channel-factory: all assertions passed');
