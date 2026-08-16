/* Opens the org-pinned SecureChannel the join controller talks over.
 *
 * The channel client itself is relaykit/core — one origin-neutral module served
 * own-origin by the dashboard at /static/js/lib/relaykit-core.js (relay serves
 * the same bytes at /l-assets/relaykit-core.js). We import it dynamically so
 * this module loads even before core is deployed; openChannel resolves it at
 * call time, and lands in dependency order once relay ships core own-origin.
 *
 * The wsUrl is derived from the invite's channelToken against the FIXED relay
 * origin, never from the dashboard page origin — the token is only a link
 * transport pointer; org and root_pub come from the dashboard-resolved invite
 * context, and the handshake pins the channel to that root.
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

export function channelUrl(channelToken, { origin = RELAY_WS_ORIGIN } = {}) {
  if (typeof channelToken !== 'string' || !/^[0-9a-f]{64}$/.test(channelToken)) {
    throw new Error('channelToken must be 64 hex chars');
  }
  return `${origin}/v1/links/${channelToken}/channel`;
}

export async function openChannel(inputs, { origin, core } = {}) {
  const lib = core || (await import(CORE_URL));
  const url = channelUrl(inputs.channelToken, { origin });
  const socket = lib.openSocket(url);
  return lib.performHandshake(socket, {
    org: inputs.org,
    token: inputs.channelToken,
    rootPub: inputs.rootPub,
  });
}
