/* The join channel factory: wsUrl derivation and the relaykit/core handshake
 * call shape, pinned to relay's frozen literals, proven with an injected core
 * so it runs before relaykit-core.js is served own-origin.
 * Run: node channel-factory.test.mjs
 */
import assert from 'node:assert/strict';

import { channelUrl, openChannel, relayWsOrigin } from '../channel-factory.js';

const TOKEN = 'd'.repeat(32);
const LINK = 'c'.repeat(64);

// 1. wsUrl is derived from the channelToken against the FIXED relay origin.
assert.equal(
  channelUrl(TOKEN),
  `wss://relay.auto.network/v1/links/${TOKEN}/channel`,
);
assert.equal(relayWsOrigin('https://registry.example:9443'), 'wss://registry.example:9443');
assert.equal(relayWsOrigin('http://127.0.0.1:9000'), 'ws://127.0.0.1:9000');
assert.throws(() => relayWsOrigin('ftp://relay.example'), /http\(s\)/);

// 2. junk never becomes a URL.
assert.throws(() => channelUrl('not-a-token'), /32 hex/);

// 3. a test/config origin may override — but it is passed in, never derived
//    from the page origin.
assert.equal(
  channelUrl(TOKEN, { origin: 'wss://localhost:9443' }),
  `wss://localhost:9443/v1/links/${TOKEN}/channel`,
);

// 4. openChannel pins the handshake solely to fragment-derived linkPub.
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
  const inputs = { org: 'o', channelToken: TOKEN, channelPub: LINK,
    relayHost: 'https://registry.example',
    rootPub: 'a'.repeat(64) };
  const channel = await openChannel(inputs, { core });
  assert.equal(calls.url, `wss://registry.example/v1/links/${TOKEN}/channel`);
  assert.deepEqual(calls.socket, { sock: true });
  assert.deepEqual(calls.params, { org: 'o', token: TOKEN, linkPub: LINK });
  assert.equal(Object.hasOwn(calls.params, 'rootPub'), false);
  assert.deepEqual(channel, { channel: true });
}

await assert.rejects(
  openChannel({ org: 'o', channelToken: TOKEN, channelPub: '' }, { core: {} }),
  /canonical 64-hex/,
);

{
  const core = {
    openSocket: async () => ({}),
    performHandshake: async () => {
      throw new Error('SERVER_HELLO signature does not verify');
    },
  };
  await assert.rejects(
    openChannel({ org: 'o', channelToken: TOKEN, channelPub: LINK }, { core }),
    (error) => error.autonetKind === 'security',
  );
}

console.log('channel-factory: all assertions passed');
