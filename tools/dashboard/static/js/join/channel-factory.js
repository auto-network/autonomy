/* Opens the per-link-authenticated SecureChannel the join controller uses.
 *
 * The channel client itself is relaykit/core — one origin-neutral module served
 * own-origin by the dashboard at /static/js/lib/relaykit-core.js (relay serves
 * the same bytes at /l-assets/relaykit-core.js). We import it dynamically so
 * this module loads even before core is deployed; openChannel resolves it at
 * call time, and lands in dependency order once relay ships core own-origin.
 *
 * The wsUrl is derived from the invite's channelToken against the FIXED relay
 * origin, never from the dashboard page origin — the token is only a link
 * transport pointer. Public envelope fields route the connection; channelPub,
 * decoded from fragment k, is the sole viewer authentication anchor.
 */
// The build this page was served from, published by the shell. A request that
// names a build may be kept by the browser; one that does not is rechecked on
// every load. Anything fetched after render has to add it itself.
function staticVersion() {
  // Called at import time, and this module is imported by tests that run
  // outside a browser, where there is no document to read.
  if (typeof document === 'undefined') return '';
  var meta = document.querySelector('meta[name="autonomy-static-version"]');
  var v = meta && meta.getAttribute('content');
  return v ? '?v=' + encodeURIComponent(v) : '';
}

const CORE_URL = '/static/js/lib/relaykit-core.js' + staticVersion();
const RELAY_WS_ORIGIN = 'wss://relay.auto.network';

export function relayWsOrigin(relayHost) {
  if (!relayHost) return RELAY_WS_ORIGIN;
  let parsed;
  try {
    parsed = new URL(relayHost);
  } catch (_) {
    throw new Error('relayHost must be an http(s) origin');
  }
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') {
    throw new Error('relayHost must be an http(s) origin');
  }
  return `${parsed.protocol === 'https:' ? 'wss:' : 'ws:'}//${parsed.host}`;
}

export function channelUrl(channelToken, { origin = RELAY_WS_ORIGIN } = {}) {
  if (typeof channelToken !== 'string' || !/^[0-9a-f]{32}$/.test(channelToken)) {
    throw new Error('channelToken must be 32 hex chars');
  }
  return `${origin}/v1/links/${channelToken}/channel`;
}

export async function openChannel(inputs, { origin, core } = {}) {
  if (typeof inputs.channelPub !== 'string'
      || !/^[0-9a-f]{64}$/.test(inputs.channelPub)) {
    const error = new Error('channelPub must be the canonical 64-hex link key');
    error.autonetKind = 'security';
    throw error;
  }
  const lib = core || (await import(CORE_URL));
  const url = channelUrl(inputs.channelToken, {
    origin: origin || relayWsOrigin(inputs.relayHost),
  });
  const socket = await lib.openSocket(url);
  try {
    return await lib.performHandshake(socket, {
      org: inputs.org,
      token: inputs.channelToken,
      linkPub: inputs.channelPub,
    });
  } catch (error) {
    const message = String(error && error.message || error);
    if (/SERVER_HELLO|signature|fragment|public key/i.test(message)) {
      error.autonetKind = 'security';
    }
    throw error;
  }
}
