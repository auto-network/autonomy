/* autonet.js — auto.network bootloader channel client (B3, spec §5.3).
 *
 * The browser half of the E2E channel built in B2 (tools/network/relaykit):
 * fetch the grant envelope, open the relay WebSocket, run the X25519
 * handshake with the envelope's org root public key as the trust pin (I5),
 * then speak AES-256-GCM records. Everything cryptographic is WebCrypto
 * (Ed25519 / X25519 / HKDF-SHA256 / AES-GCM — "secure curves" are native
 * in current engines); no framework, no build step, no dependencies.
 *
 * Wire compatibility contracts (must match the Python side byte-for-byte):
 *   - canonicalJson(): tools/network/idkit/canonical.py
 *   - cert verification rules: tools/network/idkit/verify.py (minus
 *     revocations — an anonymous rung-1 viewer has no denylist feed; the
 *     registry enforces revocations on the tunnel:serve hello instead)
 *   - handshake + record layer: tools/network/relaykit/channel.py
 */
import {
  SecureChannel,
  canonicalJson,
  openSocket,
  performHandshake,
  sendOp,
} from "./relaykit-core.js";

"use strict";

const autonet = (() => {
  const SEND_CHUNK_SIZE = 60 * 1024;
  const ATTACHMENT_CHUNK_SIZE = 1024 * 1024;
  const ATTACHMENT_WINDOW_SIZE = 8 * ATTACHMENT_CHUNK_SIZE;
  const ATTACHMENT_LAST_IN_WINDOW = 0x01;
  const ATTACHMENT_EOF = 0x02;
  const MAX_ARTIFACT_BYTES = 48 * 1024 * 1024;
  const MAX_TITLE_CHARS = 500;
  const MAX_ATTACHMENT_NAME_CHARS = 255;
  // Bound establishing a live channel (connect + handshake). The relay may
  // ACCEPT a viewer socket and never send SERVER_HELLO — it holds the
  // connection open when no serving tunnel is dialed in for the org — and
  // recvBinary() waits forever, so without this the page hangs on a spinner
  // instead of showing the honest offline error. The body transfer that
  // follows is NOT bounded here: a large artifact may legitimately take time.
  const CONNECT_TIMEOUT_MS = 10000;
  const VIEWER_READY_TIMEOUT_MS = 10000;
  const ICE_GATHER_TIMEOUT_MS = 2000;
  const ICE_ATTEMPT_TIMEOUT_MS = 8000;
  const ICE_MAX_CANDIDATES = 32;
  const ICE_MAX_CANDIDATE_BYTES = 2 * 1024;
  const ICE_MAX_SDP_BYTES = 64 * 1024;
  const DATA_CHANNEL_LABEL = "autonomy-v1";
  const DATA_CHANNEL_QUEUE_RECORDS = 32;
  const DATA_CHANNEL_LOW_WATER_BYTES = 256 * 1024;
  const DATA_CHANNEL_HIGH_WATER_BYTES = 512 * 1024;
  const DATA_CHANNEL_DRAIN_TIMEOUT_MS = 5000;
  const DATA_CHANNEL_MAX_WIRE_BYTES = SEND_CHUNK_SIZE + 25;
  const STUN_URL = "stun:turn.auto.network:3478";
  const TURN_URLS = [
    "turn:turn.auto.network:3478?transport=udp",
    "turn:turn.auto.network:3478?transport=tcp",
    "turns:turn.auto.network:443?transport=tcp",
  ];
  const ATTACHMENT_CURSOR_DOMAIN = "autonomy.attachment.cursor.v1";
  const ATTACHMENT_ERROR_CODES = new Set([
    "not_found", "not_authorized", "oversize", "out_of_range",
    "unavailable", "internal",
  ]);

  const te = new TextEncoder();

  // ---- byte helpers --------------------------------------------------------

  function hexToBytes(hex, expectedLen, what) {
    if (typeof hex !== "string" || hex !== hex.toLowerCase() ||
        (expectedLen && hex.length !== expectedLen) || /[^0-9a-f]/.test(hex) || hex.length % 2) {
      throw new Error(what + " is not valid lowercase hex");
    }
    const bytes = new Uint8Array(hex.length / 2);
    for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
    return bytes;
  }

  function bytesToHex(bytes) {
    return Array.from(new Uint8Array(bytes), (b) => b.toString(16).padStart(2, "0")).join("");
  }

  // ---- transport -----------------------------------------------------------

  /* Reject a promise if it does not settle within ms — the caller turns the
   * rejection into the honest offline error. clearTimeout on settle so a
   * completed handshake never trips a late timer. */
  function withTimeout(promise, ms, label, kind = null) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(
        () => reject(kind
          ? typedError(kind, "timed out establishing channel: " + label)
          : new Error("timed out establishing channel: " + label)), ms);
      promise.then(
        (value) => { clearTimeout(timer); resolve(value); },
        (err) => { clearTimeout(timer); reject(err); });
    });
  }

  function typedError(kind, message) {
    const error = new Error(message);
    error.autonetKind = kind;
    return error;
  }

  /* Every message the serving end sends carries a one-byte kind:
   *   0x00 pairwise channel record, opened with this channel's key
   *   0x01 fan-out feed frame, opened with the link's shared stream key
   * The handshake is tagged too -- a feed frame can arrive during it, because
   * the relay attaches a listener at channel OPEN, before subscribe. */
  const VIEWER_KIND_RECORD = 0x00;
  const VIEWER_KIND_FEED = 0x01;

  // ---- bounded browser WebRTC transport ----------------------------------

  function exactFields(value, fields) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const actual = Object.keys(value).sort();
    const expected = [...fields].sort();
    return actual.length === expected.length &&
      actual.every((field, index) => field === expected[index]);
  }

  function utf8Length(value) {
    return te.encode(value).length;
  }

  function isMdnsName(value) {
    return typeof value === "string" && value.length <= 253 &&
      /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+local\.?$/i.test(value);
  }

  function parseIpv4(value) {
    if (!/^(?:\d{1,3}\.){3}\d{1,3}$/.test(value)) return null;
    const parts = value.split(".").map(Number);
    return parts.every((part) => part >= 0 && part <= 255) ? parts : null;
  }

  function isGlobalIpv4(value) {
    const p = parseIpv4(value);
    if (!p) return false;
    if (p[0] === 0 || p[0] === 10 || p[0] === 127 || p[0] >= 224) return false;
    if (p[0] === 100 && p[1] >= 64 && p[1] <= 127) return false;
    if (p[0] === 169 && p[1] === 254) return false;
    if (p[0] === 172 && p[1] >= 16 && p[1] <= 31) return false;
    if (p[0] === 192 && p[1] === 0 && (p[2] === 0 || p[2] === 2)) return false;
    if (p[0] === 192 && p[1] === 88 && p[2] === 99) return false;
    if (p[0] === 192 && p[1] === 168) return false;
    if (p[0] === 198 && (p[1] === 18 || p[1] === 19 || p[1] === 51 && p[2] === 100)) return false;
    if (p[0] === 203 && p[1] === 0 && p[2] === 113) return false;
    return true;
  }

  function isGlobalIpv6(value) {
    if (typeof value !== "string" || !value.includes(":")) return false;
    const lower = value.toLowerCase();
    if (lower.startsWith("::ffff:")) return isGlobalIpv4(lower.slice(7));
    if (lower.startsWith("::") || lower.startsWith("ff") ||
        lower.startsWith("fe8") || lower.startsWith("fe9") ||
        lower.startsWith("fea") || lower.startsWith("feb") ||
        lower.startsWith("fec") || lower.startsWith("fed") ||
        lower.startsWith("fee") || lower.startsWith("fef") ||
        lower.startsWith("fc") || lower.startsWith("fd") ||
        lower.startsWith("2001:db8:") || lower.startsWith("2001:0:") ||
        lower.startsWith("2001:2:") || lower.startsWith("2001:10:") ||
        lower.startsWith("2001:20:") || lower.startsWith("2002:")) return false;
    if (!/^[0-9a-f:]+$/.test(lower)) return false;
    const first = parseInt(lower.split(":", 1)[0], 16);
    return first >= 0x2000 && first <= 0x3fff;
  }

  function isGlobalIp(value) {
    return isGlobalIpv4(value) || isGlobalIpv6(value);
  }

  function candidateTokens(value) {
    if (!exactFields(value, ["candidate", "sdpMid", "sdpMLineIndex", "usernameFragment"]) ||
        typeof value.candidate !== "string" || !value.candidate ||
        utf8Length(value.candidate) > ICE_MAX_CANDIDATE_BYTES) {
      throw new Error("malformed ICE candidate");
    }
    const tokens = value.candidate.trim().split(/\s+/);
    if (tokens.length < 8 || !tokens[0].startsWith("candidate:") ||
        tokens[6] !== "typ" || !["host", "srflx", "relay"].includes(tokens[7])) {
      throw new Error("malformed ICE candidate");
    }
    if ((tokens.length - 8) % 2) throw new Error("malformed ICE candidate extensions");
    const extensions = new Map();
    for (let index = 8; index < tokens.length; index += 2) {
      const name = tokens[index].toLowerCase();
      if (extensions.has(name)) throw new Error("duplicate ICE candidate extension");
      extensions.set(name, index + 1);
    }
    if (extensions.has("raddr") !== extensions.has("rport")) {
      throw new Error("incomplete ICE related address");
    }
    return { tokens, type: tokens[7], address: tokens[4], extensions };
  }

  /** Privacy-filter one trickled candidate; null means deliberately omitted. */
  function filterIceCandidate(value, policy) {
    if (policy !== "direct_allowed" && policy !== "relay_only") {
      throw new Error("unknown ICE policy");
    }
    const { tokens, type, address, extensions } = candidateTokens(value);
    if (policy === "relay_only" && type !== "relay") return null;
    if (type === "host") {
      if (policy === "relay_only" || !isMdnsName(address) || extensions.has("raddr")) {
        return null;
      }
    } else if (!isGlobalIp(address)) {
      // A CGNAT/double-NAT srflx address is unusable to the peer, but must
      // not abort the whole offer: a relay candidate may still work.
      if (type === "srflx") return null;
      throw new Error("ICE candidate address is not globally routable");
    }

    const relatedIndex = extensions.get("raddr");
    if (relatedIndex !== undefined) {
      const related = tokens[relatedIndex];
      if (type === "relay" || !isGlobalIp(related)) {
        tokens[relatedIndex] = related.includes(":") ? "::" : "0.0.0.0";
        tokens[extensions.get("rport")] = "9";
      }
    }
    return {
      candidate: tokens.join(" "),
      sdpMid: value.sdpMid,
      sdpMLineIndex: value.sdpMLineIndex,
      usernameFragment: value.usernameFragment,
    };
  }

  function sdpLines(sdp) {
    if (typeof sdp !== "string" || utf8Length(sdp) > ICE_MAX_SDP_BYTES) {
      throw new Error("SDP is malformed or exceeds its byte limit");
    }
    return { lines: sdp.split(/\r?\n/), newline: sdp.includes("\r\n") ? "\r\n" : "\n" };
  }

  /** Remove every candidate and neutralize every standard SDP address slot. */
  function sanitizeIceSdp(sdp) {
    const { lines, newline } = sdpLines(sdp);
    const clean = [];
    for (let line of lines) {
      const stripped = line.trim();
      if (/^a=(?:candidate:|end-of-candidates$)/i.test(stripped)) continue;
      let match = /^c=IN IP(4|6) \S+$/i.exec(stripped);
      if (match) {
        line = `c=IN IP${match[1]} ${match[1] === "4" ? "0.0.0.0" : "::"}`;
      } else {
        match = /^o=(\S+ \S+ \S+ IN IP)(4|6) \S+$/i.exec(stripped);
        if (match) line = `o=${match[1]}${match[2]} ${match[2] === "4" ? "0.0.0.0" : "::"}`;
        match = /^a=rtcp:(\d+) IN IP(4|6) \S+$/i.exec(stripped);
        if (match) line = `a=rtcp:${match[1]} IN IP${match[2]} ${match[2] === "4" ? "0.0.0.0" : "::"}`;
      }
      clean.push(line);
    }
    const result = clean.join(newline);
    assertAddressFreeIceSdp(result);
    return result;
  }

  /** Refuse candidate smuggling and non-placeholder SDP address positions. */
  function assertAddressFreeIceSdp(sdp) {
    const { lines } = sdpLines(sdp);
    let addressPositions = 0;
    for (const line of lines) {
      const stripped = line.trim();
      if (/^a=(?:candidate:|end-of-candidates$)/i.test(stripped)) {
        throw new Error("SDP contains a smuggled ICE candidate");
      }
      const fields = stripped.split(/\s+/);
      if (/^c=IN IP[46] /i.test(stripped) || /^o=/i.test(stripped) ||
          /^a=rtcp:\d+ IN IP[46] /i.test(stripped)) {
        const address = fields[fields.length - 1];
        if (address !== "0.0.0.0" && address !== "::") {
          throw new Error("SDP contains a non-placeholder address");
        }
        addressPositions += 1;
      }
    }
    if (!addressPositions) throw new Error("SDP contains no checked address position");
    return sdp;
  }

  function parseJsonMessage(bytes, what) {
    try {
      return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    } catch (_error) {
      throw new Error(what + " is not valid JSON");
    }
  }

  function validateIceConfig(value, attemptId, nowSeconds) {
    if (!exactFields(value, ["v", "op", "attempt_id", "policy", "ice_servers", "expires_at"]) ||
        value.v !== 1 || value.op !== "ice.config" || value.attempt_id !== attemptId ||
        !["direct_allowed", "relay_only"].includes(value.policy) ||
        !Number.isInteger(value.expires_at) || value.expires_at <= nowSeconds ||
        !Array.isArray(value.ice_servers) || value.ice_servers.length !== 2) {
      throw new Error("malformed ice.config");
    }
    const [stun, turn] = value.ice_servers;
    if (!exactFields(stun, ["urls"]) || !Array.isArray(stun.urls) ||
        stun.urls.length !== 1 || stun.urls[0] !== STUN_URL ||
        !exactFields(turn, ["urls", "username", "credential", "credentialType"]) ||
        !Array.isArray(turn.urls) || turn.urls.length !== TURN_URLS.length ||
        !turn.urls.every((url, index) => url === TURN_URLS[index]) ||
        turn.credentialType !== "password" || typeof turn.username !== "string" ||
        !turn.username || utf8Length(turn.username) > 512 ||
        typeof turn.credential !== "string" || !turn.credential ||
        utf8Length(turn.credential) > 512) {
      throw new Error("malformed ICE server configuration");
    }
    return value;
  }

  function validateIceAnswer(value, attemptId, policy) {
    if (!exactFields(value, ["v", "op", "attempt_id", "sdp", "candidates"]) ||
        value.v !== 1 || value.op !== "ice.answer" || value.attempt_id !== attemptId ||
        !Array.isArray(value.candidates) || value.candidates.length > ICE_MAX_CANDIDATES) {
      throw new Error("malformed ice.answer");
    }
    assertAddressFreeIceSdp(value.sdp);
    const candidates = value.candidates.map((candidate) => {
      const filtered = filterIceCandidate(candidate, policy);
      if (!filtered) throw new Error("answer candidate violates ICE policy");
      return filtered;
    });
    return { sdp: value.sdp, candidates };
  }

  function dataChannelTransport(channel, onClose) {
    if (!channel || channel.label !== DATA_CHANNEL_LABEL) {
      throw new Error("unexpected DataChannel label");
    }
    channel.binaryType = "arraybuffer";
    channel.bufferedAmountLowThreshold = DATA_CHANNEL_LOW_WATER_BYTES;
    const queue = [];
    const waiters = [];
    const feedQueue = [];
    const feedWaiters = [];
    let closed = null;

    const fail = (error) => {
      if (closed) return;
      closed = error;
      while (waiters.length) waiters.shift().reject(error);
      while (feedWaiters.length) feedWaiters.shift().reject(error);
      try { channel.close(); } catch (_error) { /* already closed */ }
      if (typeof onClose === "function") {
        try { onClose(); } catch (_error) { /* peer teardown is best effort */ }
      }
    };
    channel.addEventListener("message", (event) => {
      if (!(event.data instanceof ArrayBuffer)) return fail(new Error("DataChannel message is not binary"));
      const tagged = new Uint8Array(event.data);
      if (!tagged.length || tagged.length > DATA_CHANNEL_MAX_WIRE_BYTES + 1 ||
          (tagged[0] !== VIEWER_KIND_RECORD && tagged[0] !== VIEWER_KIND_FEED)) {
        return fail(new Error("DataChannel carried an invalid application record"));
      }
      const payload = tagged.slice(1);
      const targetQueue = tagged[0] === VIEWER_KIND_FEED ? feedQueue : queue;
      const targetWaiters = tagged[0] === VIEWER_KIND_FEED ? feedWaiters : waiters;
      if (targetWaiters.length) targetWaiters.shift().resolve(payload);
      else if (targetQueue.length < DATA_CHANNEL_QUEUE_RECORDS) targetQueue.push(payload);
      else fail(new Error("DataChannel receive queue is full"));
    });
    channel.addEventListener("close", () => fail(typedError("disconnected", "DataChannel closed")));
    channel.addEventListener("error", () => fail(typedError("disconnected", "DataChannel failed")));

    const waitForDrain = () => new Promise((resolve, reject) => {
      let timer;
      const done = (error) => {
        clearTimeout(timer);
        channel.removeEventListener("bufferedamountlow", low);
        channel.removeEventListener("close", gone);
        error ? reject(error) : resolve();
      };
      const low = () => done(null);
      const gone = () => done(typedError("disconnected", "DataChannel closed while draining"));
      channel.addEventListener("bufferedamountlow", low, { once: true });
      channel.addEventListener("close", gone, { once: true });
      timer = setTimeout(() => done(new Error("DataChannel send buffer did not drain")),
        DATA_CHANNEL_DRAIN_TIMEOUT_MS);
    });

    return {
      async send(payload) {
        if (!(payload instanceof Uint8Array) || payload.length > DATA_CHANNEL_MAX_WIRE_BYTES) {
          throw new Error("DataChannel record exceeds the v1 wire limit");
        }
        if (closed || channel.readyState !== "open") {
          throw closed || typedError("disconnected", "DataChannel is not open");
        }
        if (channel.bufferedAmount > DATA_CHANNEL_HIGH_WATER_BYTES) await waitForDrain();
        channel.send(payload);
      },
      recvBinary() {
        if (queue.length) return Promise.resolve(queue.shift());
        if (closed) return Promise.reject(closed);
        return new Promise((resolve, reject) => waiters.push({ resolve, reject }));
      },
      recvFeed() {
        if (feedQueue.length) return Promise.resolve(feedQueue.shift());
        if (closed) return Promise.reject(closed);
        return new Promise((resolve, reject) => feedWaiters.push({ resolve, reject }));
      },
      close() { fail(typedError("disconnected", "DataChannel closed")); },
    };
  }

  function waitForDataChannelOpen(channel) {
    if (channel.readyState === "open") return Promise.resolve();
    return new Promise((resolve, reject) => {
      const done = (error) => {
        channel.removeEventListener("open", opened);
        channel.removeEventListener("close", closed);
        channel.removeEventListener("error", failed);
        error ? reject(error) : resolve();
      };
      const opened = () => done(null);
      const closed = () => done(new Error("DataChannel closed before opening"));
      const failed = () => done(new Error("DataChannel failed before opening"));
      channel.addEventListener("open", opened, { once: true });
      channel.addEventListener("close", closed, { once: true });
      channel.addEventListener("error", failed, { once: true });
    });
  }

  async function gatherIceOffer(peer, policy) {
    const candidates = [];
    let gatheringError = null;
    let complete;
    const finished = new Promise((resolve) => { complete = resolve; });
    const onCandidate = (event) => {
      if (!event.candidate) return complete();
      try {
        const raw = typeof event.candidate.toJSON === "function"
          ? event.candidate.toJSON() : event.candidate;
        const filtered = filterIceCandidate(raw, policy);
        if (filtered) {
          if (candidates.length >= ICE_MAX_CANDIDATES) throw new Error("too many ICE candidates");
          candidates.push(filtered);
        }
      } catch (error) {
        gatheringError = error;
        complete();
      }
    };
    peer.addEventListener("icecandidate", onCandidate);
    try {
      const offer = await peer.createOffer();
      await peer.setLocalDescription(offer);
      if (peer.iceGatheringState !== "complete") {
        await withTimeout(finished, ICE_GATHER_TIMEOUT_MS, "ICE gathering");
      }
      if (gatheringError) throw gatheringError;
      return { sdp: sanitizeIceSdp(peer.localDescription.sdp), candidates };
    } finally {
      peer.removeEventListener("icecandidate", onCandidate);
    }
  }

  async function assertRelaySelected(peer) {
    if (typeof peer.getStats !== "function") {
      throw new Error("relay-only path cannot verify the selected ICE pair");
    }
    const stats = await peer.getStats();
    const rows = new Map();
    stats.forEach((value, key) => rows.set(key, value));
    let pair = [...rows.values()].find((row) => row.type === "candidate-pair" &&
      (row.selected === true || row.nominated === true) && row.state === "succeeded");
    if (!pair) {
      const transport = [...rows.values()].find((row) => row.type === "transport" &&
        typeof row.selectedCandidatePairId === "string");
      pair = transport && rows.get(transport.selectedCandidatePairId);
      if (!pair || pair.type !== "candidate-pair" || pair.state !== "succeeded") pair = null;
    }
    const local = pair && rows.get(pair.localCandidateId);
    if (!local || local.candidateType !== "relay") {
      throw new Error("relay-only path selected a direct candidate");
    }
  }

  /**
   * Attempt one parallel upgrade after the relay has already delivered the
   * artifact request.  The caller supplies ONE shared authentication function
   * created by the D22 link-opening layer; this transport adapter neither
   * reads the registry's root_pub nor replays identity lineage itself.
   */
  async function attemptWebRtcUpgrade({
    openSignaling,
    authenticateTransport,
    proveApplication,
    RTCPeerConnectionImpl = globalThis.RTCPeerConnection,
    attemptId = bytesToHex(crypto.getRandomValues(new Uint8Array(16))),
    nowSeconds = () => Math.floor(Date.now() / 1000),
  }) {
    if (typeof openSignaling !== "function" || typeof authenticateTransport !== "function" ||
        typeof proveApplication !== "function" || typeof RTCPeerConnectionImpl !== "function" ||
        !/^[0-9a-f]{32}$/.test(attemptId)) {
      throw new Error("WebRTC upgrade dependencies are incomplete");
    }
    const deadline = Date.now() + ICE_ATTEMPT_TIMEOUT_MS;
    const bounded = (promise, label) => withTimeout(
      Promise.resolve(promise), Math.max(1, deadline - Date.now()), label,
    );
    let signalingTransport = null;
    let signalingChannel = null;
    let peer = null;
    let directTransport = null;
    let directChannel = null;
    let complete = false;
    try {
      signalingTransport = await bounded(openSignaling(), "ICE signaling connect");
      signalingChannel = await bounded(
        authenticateTransport(signalingTransport), "ICE signaling authentication",
      );
      await bounded(signalingChannel.sendMessage(te.encode(canonicalJson({
        v: 1, op: "ice.begin", attempt_id: attemptId,
      }))), "ICE begin");
      const config = validateIceConfig(
        parseJsonMessage(await bounded(signalingChannel.recvMessage(), "ICE config"), "ice.config"),
        attemptId,
        nowSeconds(),
      );
      peer = new RTCPeerConnectionImpl({
        iceServers: config.ice_servers,
        iceCandidatePoolSize: 0,
        iceTransportPolicy: config.policy === "relay_only" ? "relay" : "all",
      });
      const dataChannel = peer.createDataChannel(DATA_CHANNEL_LABEL, { ordered: true });
      peer.addEventListener("datachannel", () => { try { peer.close(); } catch (_error) {} });
      const offer = await bounded(gatherIceOffer(peer, config.policy), "ICE gathering");
      await bounded(signalingChannel.sendMessage(te.encode(canonicalJson({
        v: 1,
        op: "ice.offer",
        attempt_id: attemptId,
        sdp: offer.sdp,
        candidates: offer.candidates,
      }))), "ICE offer");
      const answer = validateIceAnswer(
        parseJsonMessage(await bounded(signalingChannel.recvMessage(), "ICE answer"), "ice.answer"),
        attemptId,
        config.policy,
      );
      await bounded(peer.setRemoteDescription({ type: "answer", sdp: answer.sdp }), "ICE answer apply");
      for (const candidate of answer.candidates) {
        await bounded(peer.addIceCandidate(candidate), "remote ICE candidate");
      }
      await bounded(peer.addIceCandidate(null), "remote ICE completion");
      await bounded(waitForDataChannelOpen(dataChannel), "DataChannel open");
      if (config.policy === "relay_only") await bounded(assertRelaySelected(peer), "relay path proof");
      directTransport = dataChannelTransport(dataChannel, () => peer.close());
      directChannel = await bounded(
        authenticateTransport(directTransport), "DataChannel application authentication",
      );
      await bounded(proveApplication(directChannel), "DataChannel application exchange");
      complete = true;
      return {
        channel: directChannel,
        transport: directTransport,
        policy: config.policy,
        expiresAt: config.expires_at,
        peer,
      };
    } finally {
      if (signalingChannel && typeof signalingChannel.close === "function") signalingChannel.close();
      else if (signalingTransport && typeof signalingTransport.close === "function") signalingTransport.close();
      if (!complete) {
        if (directChannel && typeof directChannel.close === "function") directChannel.close();
        else if (directTransport) directTransport.close();
        if (peer) peer.close();
      }
    }
  }

  /* §5.4 direct-connect seam: try each announced endpoint before falling
   * back to the relay. Empty in v1 — the code path exists and is what the
   * seam test exercises with a stub attempt function. Returns the first
   * successful transport, or null → caller uses the relay. */
  async function attemptEndpoints(endpoints, attempt) {
    for (const endpoint of endpoints || []) {
      try {
        const transport = await attempt(endpoint);
        if (transport) return transport;
      } catch (err) { /* endpoint dead or unreachable: fall through */ }
    }
    return null;
  }

  async function attemptDirectEndpoint(_endpoint) {
    // v1: direct connect is not implemented — every announced endpoint
    // (there are none yet) falls back to the relay. When this activates
    // (companion spec, network fabric), the endpoint must still prove
    // itself with the same handshake: the pin does not change.
    return null;
  }

  // ---- channel fetch protocol v1 (the C4 seam) ------------------------------
  // request  : canonical JSON {v:1, op:"fetch"}
  // response : JSON header line + "\n" + part-addressed body bytes

  async function fetchArtifact(channel) {
    await channel.sendMessage(te.encode(canonicalJson({ v: 1, op: "fetch" })));
    const response = await channel.recvMessage();
    const newline = response.indexOf(10);
    if (newline < 0) throw new Error("malformed artifact response");
    const header = JSON.parse(new TextDecoder().decode(response.slice(0, newline)));
    return { header, body: response.slice(newline + 1) };
  }

  function hasOnlyKeys(value, allowed) {
    return value && typeof value === "object" && !Array.isArray(value)
      && Object.keys(value).every((key) => allowed.includes(key));
  }

  function codePointLength(value) {
    return Array.from(value).length;
  }

  function truncateCodePoints(value, limit) {
    return Array.from(value).slice(0, limit).join("");
  }

  function validateSlice(value, bodyLength, what, allowEmpty = true) {
    if (!hasOnlyKeys(value, ["offset", "length"])) {
      throw new Error("invalid " + what);
    }
    const offset = value.offset;
    const length = value.length;
    if (!Number.isSafeInteger(offset) || !Number.isSafeInteger(length)
        || offset < 0 || length < 0 || (!allowEmpty && length === 0)
        || offset > bodyLength || length > bodyLength - offset) {
      throw new Error("invalid " + what);
    }
    return { offset, length, end: offset + length, what };
  }

  function validateArtifact(header, body) {
    if (!(body instanceof Uint8Array) || body.length > MAX_ARTIFACT_BYTES) {
      throw new Error("invalid artifact body");
    }
    // `kind` is bounded descriptive metadata, NOT a code selector. There is
    // deliberately no allowlist: autonet.js cannot enumerate viewer types
    // without a registry deploy every time one is added, and it does not
    // need to -- the artifact arrives over a channel whose server cert
    // chained to the org root this page pinned before any bytes flowed, so
    // no third party can declare a kind.
    if (!hasOnlyKeys(header, ["v", "status", "kind", "viewer", "parts", "attachments", "branding"])
        || header.v !== 1 || header.status !== "ok"
        || typeof header.kind !== "string" || !header.kind
        || codePointLength(header.kind) > 64) {
      throw new Error("invalid artifact header");
    }

    const ranges = [validateSlice(header.viewer, body.length, "viewer", false)];

    // Generic parts: any viewer may carry them, and autonet.js never
    // interprets one. Validation is structural only -- unique refs, slices
    // inside the body -- because what a part MEANS is the viewer's business.
    let parts = null;
    if (Object.prototype.hasOwnProperty.call(header, "parts")) {
      if (!Array.isArray(header.parts)) throw new Error("invalid artifact parts");
      const partRefs = new Set();
      parts = header.parts.map((part) => {
        if (!hasOnlyKeys(part, ["ref", "mime", "offset", "length"])
            || typeof part.ref !== "string" || !part.ref || partRefs.has(part.ref)
            || typeof part.mime !== "string" || !part.mime) {
          throw new Error("invalid artifact part");
        }
        partRefs.add(part.ref);
        const range = validateSlice(
          { offset: part.offset, length: part.length }, body.length, "part"
        );
        ranges.push(range);
        return { ref: part.ref, mime: part.mime, ...range };
      });
    }
    // Attachment manifest, TOP LEVEL and structural. The host activates its
    // download controller from this manifest's PRESENCE, never from a kind.
    let attachments = null;
    if (Object.prototype.hasOwnProperty.call(header, "attachments")) {
      if (!Array.isArray(header.attachments)) {
        throw new Error("invalid attachment manifest");
      }
      const manifestRefs = new Set();
      attachments = header.attachments.map((entry) => {
        if (!hasOnlyKeys(entry, ["ref", "name", "mime", "raw_sha256", "total_size", "oversize"])
            || typeof entry.ref !== "string" || !entry.ref || manifestRefs.has(entry.ref)
            || typeof entry.name !== "string" || codePointLength(entry.name) > MAX_ATTACHMENT_NAME_CHARS
            || typeof entry.mime !== "string" || !entry.mime
            || typeof entry.raw_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(entry.raw_sha256)
            || !Number.isSafeInteger(entry.total_size) || entry.total_size < 0
            || typeof entry.oversize !== "boolean") {
          throw new Error("invalid attachment manifest entry");
        }
        manifestRefs.add(entry.ref);
        return {
          ref: entry.ref, name: entry.name, mime: entry.mime,
          raw_sha256: entry.raw_sha256, total_size: entry.total_size,
          oversize: entry.oversize,
        };
      });
    }

    let branding = null;
    if (Object.prototype.hasOwnProperty.call(header, "branding")) {
      const value = header.branding;
      if (!hasOnlyKeys(value, ["name", "color", "initial", "favicon", "favicon_url"])
          || typeof value.name !== "string" || !value.name
          || codePointLength(value.name) > 200
          || typeof value.color !== "string" || !/^#[0-9a-fA-F]{6}$/.test(value.color)
          || typeof value.initial !== "string" || codePointLength(value.initial) !== 1
          || (Object.prototype.hasOwnProperty.call(value, "favicon")
              && Object.prototype.hasOwnProperty.call(value, "favicon_url"))) {
        throw new Error("invalid artifact branding");
      }
      let favicon = null;
      if (Object.prototype.hasOwnProperty.call(value, "favicon")) {
        if (!hasOnlyKeys(value.favicon, ["mime", "offset", "length"])
            || typeof value.favicon.mime !== "string"
            || !value.favicon.mime.startsWith("image/")) {
          throw new Error("invalid artifact favicon");
        }
        favicon = validateSlice(
          { offset: value.favicon.offset, length: value.favicon.length },
          body.length, "favicon", false
        );
        favicon.mime = value.favicon.mime;
        ranges.push(favicon);
      }
      let faviconUrl = null;
      if (Object.prototype.hasOwnProperty.call(value, "favicon_url")) {
        if (typeof value.favicon_url !== "string"
            || value.favicon_url.length > 2048
            || !value.favicon_url.startsWith("https://")) {
          throw new Error("invalid artifact favicon URL");
        }
        faviconUrl = value.favicon_url;
      }
      branding = {
        name: value.name, color: value.color, initial: value.initial,
        favicon, faviconUrl,
      };
    }

    const ordered = ranges.slice().sort((a, b) => a.offset - b.offset || a.end - b.end);
    let cursor = 0;
    for (const range of ordered) {
      if (range.offset !== cursor) {
        throw new Error("artifact slices must exactly cover the body");
      }
      cursor = range.end;
    }
    if (cursor !== body.length) {
      throw new Error("artifact slices must exactly cover the body");
    }
    return {
      kind: header.kind,
      viewer: ranges[0],
      parts,
      attachments,
      branding,
    };
  }


  // ---- generic channel broker ---------------------------------------------

  /** One MessagePort per viewer document, brokering channel ops.
   *
   * The viewer speaks three shapes and nothing else:
   *   viewer -> host   {v:1, type:"request",  id, op, body}
   *   host -> viewer   {v:1, type:"response", id, ok, body|error}
   *   host -> viewer   {v:1, type:"event",    topic, body}
   *
   * The host forwards `op` and `body` opaquely: it does not know what a
   * pillar or a question is, and gains no per-viewer branch.
   *
   * The viewer never receives the bearer token, the socket, the channel key
   * or the stream key. `subscribe` is answered by the HOST -- it keeps the
   * stream key, decodes feed frames itself, and posts plaintext events. A
   * viewer that could issue channel ops with the key in hand would make the
   * sandbox mean only "cannot read the parent's DOM".
   */
  class ChannelBroker {
    constructor(channel, transport) {
      this.channel = channel;
      this.transport = transport;
      this.port = null;
      // The record layer is strictly request/response, so exchanges are
      // serialized. Correlation ids mean the VIEWER may still have several
      // in flight -- the singleton pendingOp this replaces silently
      // dropped the first of two concurrent calls.
      this.queue = Promise.resolve();
      this.streamKey = null;
      this.feeding = false;
      this.closed = false;
    }

    /** Attach a viewer's port, replacing any previous document's.
     *
     * After document.open/write/close the old document is gone and its port
     * with it, but its in-flight exchanges may still resolve. Those replies
     * are dropped rather than delivered to the new document, which never
     * asked for them and has fresh state. */
    attach(port) {
      if (this.port && this.port !== port) {
        try { this.port.close(); } catch (_err) { /* already gone */ }
      }
      this.port = port;
      port.onmessage = (event) => this.onRequest(port, event.data);
      if (typeof port.start === "function") port.start();
    }

    onRequest(port, message) {
      if (!message || message.v !== 1 || message.type !== "request"
          || typeof message.op !== "string") return;
      const id = message.id;
      this.queue = this.queue.then(async () => {
        if (this.closed || this.port !== port) return;   // stale document
        let reply;
        try {
          const request = { v: 1, op: message.op };
          if (message.body !== undefined) request.body = message.body;
          const body = await this.exchange(request);
          if (message.op === "subscribe" && body && typeof body.stream_key === "string") {
            // The key stays here. The viewer gets acknowledgement only.
            this.streamKey = hexToBytes(body.stream_key);
            delete body.stream_key;
            this.startFeed();
          }
          reply = { v: 1, type: "response", id, ok: true, body };
        } catch (err) {
          reply = {
            v: 1, type: "response", id, ok: false,
            error: String((err && err.message) || err),
          };
        }
        if (!this.closed && this.port === port) port.postMessage(reply);
      });
    }

    async exchange(request) {
      return sendOp(this.channel, request);
    }

    /** Pump feed frames. They arrive on their own socket queue because the
     * kind byte separates them from channel records before either decoder
     * sees one; nothing here guesses by trying a key. */
    startFeed() {
      if (this.feeding || !this.transport || typeof this.transport.recvFeed !== "function") return;
      this.feeding = true;
      (async () => {
        while (!this.closed) {
          let sealed;
          try {
            sealed = await this.transport.recvFeed();
          } catch (_err) {
            return;   // socket gone: the page keeps whatever it has rendered
          }
          const opened = await openStreamFrame(this.streamKey, sealed);
          if (!opened || !this.port) continue;
          let body;
          try {
            body = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(opened));
          } catch (_err) {
            continue;   // one bad frame must not end the feed
          }
          this.port.postMessage({
            v: 1, type: "event", topic: (body && body.kind) || "event", body,
          });
        }
      })();
    }

    dispose() {
      this.closed = true;
      if (this.port) {
        try { this.port.close(); } catch (_err) { /* already gone */ }
        this.port = null;
      }
    }
  }

  // ---- attachment window/resume + parent-owned storage --------------------

  function hasExactKeys(value, allowed) {
    return hasOnlyKeys(value, allowed)
      && Object.keys(value).length === allowed.length
      && allowed.every((key) => Object.prototype.hasOwnProperty.call(value, key));
  }

  async function attachmentCursorId(linkToken) {
    if (typeof linkToken !== "string" || !linkToken) {
      throw new Error("link token must be a non-empty string");
    }
    const key = await crypto.subtle.importKey(
      "raw", te.encode(ATTACHMENT_CURSOR_DOMAIN),
      { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    return bytesToHex(await crypto.subtle.sign("HMAC", key, te.encode(linkToken)));
  }

  async function attachmentSinkId(cursorId, ref) {
    const refHash = bytesToHex(await crypto.subtle.digest("SHA-256", te.encode(ref)));
    return "attachment-" + cursorId + "-" + refHash + ".part";
  }

  class AttachmentTransferError extends Error {
    constructor(code, message = null) {
      super(message || ("attachment transfer failed: " + code));
      this.name = "AttachmentTransferError";
      this.code = code;
    }
  }

  class BrowserCursorStore {
    constructor(
      indexedDBApi = globalThis.indexedDB,
      lockManager = globalThis.navigator && globalThis.navigator.locks,
    ) {
      if (!indexedDBApi) throw new AttachmentTransferError("storage_unavailable");
      if (!lockManager || typeof lockManager.request !== "function") {
        throw new AttachmentTransferError("storage_unavailable");
      }
      this.indexedDB = indexedDBApi;
      this.lockManager = lockManager;
      this.dbPromise = null;
      this.active = new Set();
      this.lockReleases = new Map();
    }

    async _db() {
      if (this.dbPromise) return this.dbPromise;
      this.dbPromise = new Promise((resolve, reject) => {
        const request = this.indexedDB.open("autonomy-attachment-cursors-v1", 1);
        request.onupgradeneeded = () => {
          if (!request.result.objectStoreNames.contains("cursors")) {
            request.result.createObjectStore("cursors");
          }
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error || new Error("cursor database open failed"));
      });
      return this.dbPromise;
    }

    async _request(mode, operation) {
      const db = await this._db();
      return new Promise((resolve, reject) => {
        const transaction = db.transaction("cursors", mode);
        const store = transaction.objectStore("cursors");
        let settled = false;
        let result;
        const fail = (error) => {
          if (settled) return;
          settled = true;
          reject(error);
        };
        let request;
        try {
          request = operation(store);
        } catch (err) {
          fail(err);
          return;
        }
        request.onsuccess = () => { result = request.result; };
        request.onerror = () => fail(
          request.error || new Error("cursor operation failed"));
        transaction.oncomplete = () => {
          if (settled) return;
          settled = true;
          resolve(result);
        };
        transaction.onabort = () => fail(
          transaction.error || new Error("cursor transaction aborted"));
        transaction.onerror = () => fail(
          transaction.error || new Error("cursor transaction failed"));
      });
    }

    async get(key) {
      const value = await this._request("readonly", (store) => store.get(key));
      return value === undefined ? null : value;
    }

    async put(key, value) {
      await this._request("readwrite", (store) => store.put(value, key));
    }

    async delete(key) {
      await this._request("readwrite", (store) => store.delete(key));
    }

    async acquire(key) {
      if (this.active.has(key)) return false;
      return new Promise((resolve) => {
        let answered = false;
        const answer = (value) => {
          if (answered) return;
          answered = true;
          resolve(value);
        };
        // The cursor id is already an HMAC of the bearer token, so the
        // origin-wide Web Lock name discloses no capability.  Holding this
        // request open prevents a second tab from writing the same OPFS file.
        void this.lockManager.request(
          "autonomy-attachment:" + key,
          { ifAvailable: true },
          (lock) => {
            if (!lock) {
              answer(false);
              return undefined;
            }
            this.active.add(key);
            answer(true);
            return new Promise((release) => {
              this.lockReleases.set(key, release);
            });
          },
        ).catch(() => answer(false));
      });
    }

    release(key) {
      this.active.delete(key);
      const release = this.lockReleases.get(key);
      this.lockReleases.delete(key);
      if (release) release();
    }
  }

  class OpfsAttachmentSink {
    constructor(directory, handle, sinkId) {
      this.directory = directory;
      this.handle = handle;
      this.sinkId = sinkId;
    }

    static async open(sinkId, storageManager = navigator.storage) {
      if (!storageManager || typeof storageManager.getDirectory !== "function") {
        throw new AttachmentTransferError("storage_unavailable");
      }
      const root = await storageManager.getDirectory();
      const directory = await root.getDirectoryHandle(
        "autonomy-attachments-v1", { create: true });
      const handle = await directory.getFileHandle(sinkId, { create: true });
      return new OpfsAttachmentSink(directory, handle, sinkId);
    }

    async size() {
      return (await this.handle.getFile()).size;
    }

    async _withWritable(operation) {
      const writable = await this.handle.createWritable({ keepExistingData: true });
      try {
        await operation(writable);
        // FileSystemWritableFileStream has no flush primitive. close() commits
        // the staged write, so every application chunk is closed before the
        // cursor transaction advances.
        await writable.close();
      } catch (err) {
        try { await writable.abort(); } catch (_ignored) { /* best effort */ }
        throw err;
      }
    }

    async writeAt(offset, bytes) {
      const current = await this.size();
      if (!Number.isSafeInteger(offset) || offset < 0 || offset !== current) {
        throw new AttachmentTransferError("storage_non_contiguous");
      }
      await this._withWritable(async (writable) => {
        await writable.seek(offset);
        await writable.write(bytes);
      });
    }

    async truncate(size) {
      if (!Number.isSafeInteger(size) || size < 0) {
        throw new AttachmentTransferError("storage_invalid_size");
      }
      await this._withWritable((writable) => writable.truncate(size));
    }

    async flush() {
      // writeAt/truncate close each writable stream before returning.
    }

    async file() {
      return this.handle.getFile();
    }

    async remove() {
      await this.directory.removeEntry(this.sinkId);
    }
  }

  const ATTACHMENT_CURSOR_FIELDS = [
    "v", "cursor_id", "note_id", "ref", "raw_sha256",
    "total_size", "committed_offset", "sink_id",
  ];

  class AttachmentDownloader {
    constructor({
      entry, cursorId, noteId, sink, cursors, fetchWindow, onState = () => {},
    }) {
      this.entry = entry;
      this.cursorId = cursorId;
      this.noteId = noteId;
      this.sink = sink;
      this.cursors = cursors;
      this.fetchWindow = fetchWindow;
      this.onState = onState;
      this.cursorKey = cursorId + ":" + entry.ref;
      this.committedOffset = 0;
      this.cancelRequested = false;
      this.pauseRequested = false;
    }

    _record(committedOffset) {
      return {
        v: 1,
        cursor_id: this.cursorId,
        note_id: this.noteId,
        ref: this.entry.ref,
        raw_sha256: this.entry.raw_sha256,
        total_size: this.entry.total_size,
        committed_offset: committedOffset,
        sink_id: this.sink.sinkId,
      };
    }

    _recordMatches(record) {
      return hasExactKeys(record, ATTACHMENT_CURSOR_FIELDS)
        && record.v === 1
        && record.cursor_id === this.cursorId
        && record.note_id === this.noteId
        && record.ref === this.entry.ref
        && record.raw_sha256 === this.entry.raw_sha256
        && record.total_size === this.entry.total_size
        && record.sink_id === this.sink.sinkId
        && Number.isSafeInteger(record.committed_offset)
        && record.committed_offset >= 0
        && record.committed_offset <= this.entry.total_size
        && record.committed_offset % ATTACHMENT_CHUNK_SIZE === 0;
    }

    async _reset() {
      await this.sink.truncate(0);
      this.committedOffset = 0;
      await this.cursors.put(this.cursorKey, this._record(0));
      return 0;
    }

    async _prepare() {
      const record = await this.cursors.get(this.cursorKey);
      if (!this._recordMatches(record)) return this._reset();
      const size = await this.sink.size();
      if (!Number.isSafeInteger(size) || size < record.committed_offset) {
        return this._reset();
      }
      await this.sink.truncate(record.committed_offset);
      this.committedOffset = record.committed_offset;
      return this.committedOffset;
    }

    async _commit(offset) {
      await this.sink.flush();
      await this.cursors.put(this.cursorKey, this._record(offset));
      this.committedOffset = offset;
    }

    _parseError(message) {
      // Body offsets are below the 16 GiB cap and therefore start with 0x00;
      // a leading "{" unambiguously identifies a canonical JSON error.
      if (message[0] !== 0x7b) return null;
      let text;
      let value;
      try {
        text = new TextDecoder("utf-8", { fatal: true }).decode(message);
        value = JSON.parse(text);
      } catch (err) {
        throw new AttachmentTransferError("invalid_message", "malformed attachment error");
      }
      const keys = Object.keys(value || {});
      const base = ["code", "op", "ref", "v"];
      const withDetail = ["code", "detail", "op", "ref", "v"];
      if (!(hasExactKeys(value, base) || hasExactKeys(value, withDetail))
          || value.v !== 1 || value.op !== "error" || value.ref !== this.entry.ref
          || !ATTACHMENT_ERROR_CODES.has(value.code)
          || ("detail" in value && typeof value.detail !== "string")
          || canonicalJson(value) !== text) {
        throw new AttachmentTransferError("invalid_message", "invalid attachment error");
      }
      return value.code;
    }

    async _consumeWindow(start) {
      const total = this.entry.total_size;
      const limit = Math.min(start + ATTACHMENT_WINDOW_SIZE, total);
      const request = {
        v: 1, op: "attachment.fetch", ref: this.entry.ref,
        offset: start, length: ATTACHMENT_WINDOW_SIZE,
      };
      const response = await this.fetchWindow(request);
      if (!response || typeof response[Symbol.asyncIterator] !== "function") {
        throw new AttachmentTransferError("disconnected", "fetch produced no stream");
      }
      let expected = start;
      let sawLast = false;
      let sawMessage = false;
      for await (const raw of response) {
        sawMessage = true;
        const message = raw instanceof Uint8Array
          ? raw : raw instanceof ArrayBuffer ? new Uint8Array(raw) : null;
        if (!message) throw new AttachmentTransferError("invalid_message");
        if (sawLast) throw new AttachmentTransferError("invalid_message", "message after window end");
        const refusal = this._parseError(message);
        if (refusal) throw new AttachmentTransferError(refusal);
        if (message.length < 9) throw new AttachmentTransferError("invalid_message");
        const view = new DataView(message.buffer, message.byteOffset, 8);
        const offsetBig = view.getBigUint64(0);
        if (offsetBig > BigInt(Number.MAX_SAFE_INTEGER)) {
          throw new AttachmentTransferError("invalid_message");
        }
        const offset = Number(offsetBig);
        const flags = message[8];
        // A view avoids a second 1 MiB browser-heap copy; writeAt closes its
        // OPFS stream before the next channel message is requested.
        const chunk = message.subarray(9);
        if (flags & ~(ATTACHMENT_LAST_IN_WINDOW | ATTACHMENT_EOF)
            || offset !== expected || chunk.length > ATTACHMENT_CHUNK_SIZE
            || (total > 0 && chunk.length === 0)) {
          throw new AttachmentTransferError("invalid_message");
        }
        const end = offset + chunk.length;
        if (end > limit
            || Boolean(flags & ATTACHMENT_LAST_IN_WINDOW) !== (end === limit)
            || Boolean(flags & ATTACHMENT_EOF) !== (end === total)
            || ((flags & ATTACHMENT_EOF) && !(flags & ATTACHMENT_LAST_IN_WINDOW))) {
          throw new AttachmentTransferError("invalid_message");
        }
        await this.sink.writeAt(offset, chunk);
        expected = end;
        sawLast = Boolean(flags & ATTACHMENT_LAST_IN_WINDOW);
        if (expected < total && expected % ATTACHMENT_CHUNK_SIZE === 0) {
          await this._commit(expected);
        }
        this.onState("downloading", expected, total);
      }
      if (!sawMessage) throw new AttachmentTransferError("disconnected");
      if (!sawLast) throw new AttachmentTransferError("invalid_message", "window ended early");
      return { end: expected, complete: expected === total };
    }

    requestCancel() {
      this.cancelRequested = true;
    }

    requestPause() {
      this.pauseRequested = true;
    }

    async clear() {
      await this.sink.truncate(0);
      await this.cursors.delete(this.cursorKey);
      this.committedOffset = 0;
    }

    async run() {
      if (this.entry.oversize) throw new AttachmentTransferError("oversize");
      if (!(await this.cursors.acquire(this.cursorKey))) {
        throw new AttachmentTransferError("already_active");
      }
      try {
        let offset = await this._prepare();
        this.onState("downloading", offset, this.entry.total_size);
        if (this.cancelRequested) {
          await this.clear();
          this.onState("cancelled");
          return { status: "cancelled", received: 0 };
        }
        if (this.pauseRequested) {
          await this.sink.truncate(this.committedOffset);
          return { status: "paused", received: this.committedOffset };
        }
        if (offset === this.entry.total_size) {
          this.onState("ready");
          return { status: "ready", received: offset };
        }
        for (;;) {
          let result;
          try {
            result = await this._consumeWindow(offset);
          } catch (err) {
            await this.sink.truncate(this.committedOffset);
            throw err;
          }
          if (this.cancelRequested) {
            await this.clear();
            this.onState("cancelled");
            return { status: "cancelled", received: 0 };
          }
          if (this.pauseRequested) {
            await this.sink.truncate(this.committedOffset);
            return { status: "paused", received: this.committedOffset };
          }
          if (result.complete) {
            try {
              await this.sink.flush();
              if (result.end % ATTACHMENT_CHUNK_SIZE === 0) {
                await this._commit(result.end);
              }
            } catch (err) {
              await this.sink.truncate(this.committedOffset);
              throw err;
            }
            this.onState("ready");
            return { status: "ready", received: result.end };
          }
          if (result.end % ATTACHMENT_CHUNK_SIZE !== 0) {
            await this.sink.truncate(this.committedOffset);
            throw new AttachmentTransferError("invalid_message");
          }
          offset = result.end;
        }
      } finally {
        this.cursors.release(this.cursorKey);
      }
    }
  }

  function safeAttachmentName(displayName) {
    let value = typeof displayName === "string" ? displayName.normalize("NFKC") : "";
    value = value.replace(/[\u0000-\u001f\u007f<>:"/\\|?*]/g, "_")
      .replace(/^\.+/, "").trim();
    if (!value || value === "." || value === "..") value = "attachment";
    return Array.from(value).slice(0, 180).join("");
  }

  function channelAttachmentFetch(channel, request) {
    return (async function* () {
      await channel.sendMessage(te.encode(canonicalJson(request)));
      yield* channel.recvMessageStream();
    })();
  }

  class AttachmentController {
    constructor({
      frame, channel, token, noteId, manifest, exportButton = null,
      cursors = null,
      sinkFactory = (sinkId) => OpfsAttachmentSink.open(sinkId),
      hostWindow = window, documentObject = document,
    }) {
      this.frame = frame;
      this.childWindow = frame.contentWindow;
      this.channel = channel;
      this.token = token;
      this.noteId = noteId;
      this.manifest = new Map(manifest.map((entry) => [entry.ref, entry]));
      this.exportButton = exportButton;
      this.cursors = cursors;
      this.sinkFactory = sinkFactory;
      this.hostWindow = hostWindow;
      this.document = documentObject;
      this.active = null;
      this.ready = new Map();
      this.exportCandidate = null;
      this.onMessage = (event) => {
        void this.handleEvent(event).catch(() => {});
      };
      this.onExportClick = () => {
        void this.exportSelected();
      };
      hostWindow.addEventListener("message", this.onMessage);
      if (exportButton) {
        exportButton.addEventListener("click", this.onExportClick);
      }
    }

    _send(ref, attachmentState, received, total, errorCode) {
      const message = {
        v: 1, type: "attachment.state", ref, state: attachmentState,
      };
      if (received !== undefined || total !== undefined) {
        if (!Number.isSafeInteger(received) || !Number.isSafeInteger(total)
            || received < 0 || total < 0 || received > total) {
          throw new Error("invalid attachment progress");
        }
        message.received = received;
        message.total = total;
      }
      if (errorCode !== undefined) message.error_code = String(errorCode);
      this.childWindow.postMessage(message, "*");
    }

    _selectExport(ref) {
      const item = this.ready.get(ref);
      if (!item || !this.exportButton) return;
      this.exportCandidate = ref;
      this.exportButton.hidden = false;
      this.exportButton.disabled = false;
      this.exportButton.textContent = "Save " + safeAttachmentName(item.entry.name);
    }

    async _prepareFallback(item) {
      if (typeof this.hostWindow.showSaveFilePicker === "function") return;
      const file = await item.sink.file();
      item.objectUrl = this.hostWindow.URL.createObjectURL(file);
    }

    async _start(entry) {
      if (entry.oversize) {
        this._send(entry.ref, "error", undefined, undefined, "oversize");
        return;
      }
      if (this.active) {
        if (this.active.ref === entry.ref) return; // coalesce same-ref selects
        this._send(entry.ref, "error", undefined, undefined, "busy");
        return;
      }
      if (this.ready.has(entry.ref)) {
        this._send(entry.ref, "ready");
        this._selectExport(entry.ref);
        return;
      }
      const placeholder = {
        ref: entry.ref, downloader: null,
        cancelRequested: false, pauseRequested: false,
      };
      this.active = placeholder;
      this._send(entry.ref, "queued");
      try {
        const cursorId = await attachmentCursorId(this.token);
        const sinkId = await attachmentSinkId(cursorId, entry.ref);
        // Storage capability is tested only when the user selects an
        // attachment. A browser without OPFS/IndexedDB/Web Locks must still
        // render ordinary shared notes unchanged.
        if (!this.cursors) this.cursors = new BrowserCursorStore();
        const sink = await this.sinkFactory(sinkId);
        const downloader = new AttachmentDownloader({
          entry, cursorId, noteId: this.noteId, sink, cursors: this.cursors,
          fetchWindow: (request) => channelAttachmentFetch(this.channel, request),
          onState: (value, received, total) => {
            // "ready" is parent-owned: do not expose it until fallback
            // export preparation and the ready-map insertion have finished.
            if (value !== "ready") {
              this._send(entry.ref, value, received, total);
            }
          },
        });
        placeholder.downloader = downloader;
        if (placeholder.cancelRequested) downloader.requestCancel();
        if (placeholder.pauseRequested) downloader.requestPause();
        const result = await downloader.run();
        if (result.status === "ready") {
          const item = { entry, sink, downloader, objectUrl: null };
          await this._prepareFallback(item);
          this.ready.set(entry.ref, item);
          this._send(entry.ref, "ready");
          this._selectExport(entry.ref);
        }
      } catch (err) {
        const code = err instanceof AttachmentTransferError
          ? err.code : "unavailable";
        if (!(err instanceof AttachmentTransferError)
            || code === "invalid_message" || code === "disconnected") {
          this.channel.close?.();
        }
        this._send(entry.ref, "error", undefined, undefined, code);
      } finally {
        if (this.active === placeholder) this.active = null;
      }
    }

    async _cancel(ref) {
      if (this.active && this.active.ref === ref) {
        this.active.cancelRequested = true;
        if (this.active.downloader) this.active.downloader.requestCancel();
        return;
      }
      const item = this.ready.get(ref);
      if (!item) return;
      if (item.objectUrl) this.hostWindow.URL.revokeObjectURL(item.objectUrl);
      await item.downloader.clear();
      try { await item.sink.remove(); } catch (_ignored) { /* best effort */ }
      this.ready.delete(ref);
      if (this.exportCandidate === ref) this._hideExport();
      this._send(ref, "cancelled");
    }

    _hideExport() {
      this.exportCandidate = null;
      if (!this.exportButton) return;
      this.exportButton.hidden = true;
      this.exportButton.disabled = true;
      this.exportButton.textContent = "Save attachment";
    }

    async _requestExport(ref) {
      if (!this.ready.has(ref)) return;
      this._selectExport(ref);
      // postMessage does not transfer user activation. Focus the real parent
      // control; only its own click is allowed to invoke a save destination.
      this.exportButton?.focus();
    }

    async exportSelected() {
      const ref = this.exportCandidate;
      const item = ref && this.ready.get(ref);
      if (!item) return;
      this._send(ref, "exporting");
      try {
        if (typeof this.hostWindow.showSaveFilePicker === "function") {
          // This call is deliberately the first await in the parent-owned
          // click handler, preserving transient user activation.
          const destination = await this.hostWindow.showSaveFilePicker({
            suggestedName: safeAttachmentName(item.entry.name),
          });
          const source = await item.sink.file();
          const writable = await destination.createWritable();
          await source.stream().pipeTo(writable);
        } else {
          if (!item.objectUrl) throw new Error("download URL is unavailable");
          const anchor = this.document.createElement("a");
          anchor.href = item.objectUrl;
          anchor.download = safeAttachmentName(item.entry.name);
          anchor.hidden = true;
          this.document.body.appendChild(anchor);
          anchor.click();
          anchor.remove();
        }
        await this.cursors.delete(item.downloader.cursorKey);
        this._send(ref, "complete");
        this.ready.delete(ref);
        this._hideExport();
        const objectUrl = item.objectUrl;
        this.hostWindow.setTimeout(() => {
          if (objectUrl) this.hostWindow.URL.revokeObjectURL(objectUrl);
          void item.sink.remove().catch(() => {});
        }, 60000);
      } catch (err) {
        // Picker cancellation/permission denial keeps the staged file and
        // cursor intact, ready for another parent-owned gesture.
        this._send(ref, "ready");
        this._selectExport(ref);
      }
    }

    async handleEvent(event) {
      if (event.source !== this.childWindow) return;
      const message = event.data;
      if (!hasExactKeys(message, ["v", "type", "ref"])
          || message.v !== 1
          || !["attachment.select", "attachment.cancel", "attachment.export"].includes(message.type)
          || typeof message.ref !== "string"
          || !this.manifest.has(message.ref)) {
        return;
      }
      const entry = this.manifest.get(message.ref);
      if (message.type === "attachment.select") {
        await this._start(entry);
      } else if (message.type === "attachment.cancel") {
        await this._cancel(entry.ref);
      } else {
        await this._requestExport(entry.ref);
      }
    }

    dispose() {
      this.hostWindow.removeEventListener("message", this.onMessage);
      this.exportButton?.removeEventListener("click", this.onExportClick);
      if (this.active) {
        this.active.pauseRequested = true;
        this.active.downloader?.requestPause();
      }
      this._hideExport();
    }
  }

  // ---- page shell -----------------------------------------------------------

  const state = {
    phase: "init", transport: null, bodyLength: 0,
    bodySha256: null, artifactTitle: null, artifactHeight: null,
    error: null, errorKind: null,
  };
  let attachmentController = null;

  function show(id) {
    for (const section of document.querySelectorAll("main > section")) {
      const selected = section.id === id;
      section.hidden = !selected;
    }
  }

  function setStatus(text) {
    const el = document.getElementById("status-line");
    if (el) el.textContent = text;
  }

  function renderBrand(branding, body) {
    if (!branding) return;
    const brand = document.getElementById("brand");
    if (!brand) return;
    brand.textContent = "";
    brand.classList.add("brand-icon");
    brand.title = branding.name;

    const showInitial = () => {
      brand.textContent = "";
      brand.style.background = branding.color;
      const initial = document.createElement("span");
      initial.className = "brand-initial";
      initial.textContent = branding.initial;
      initial.setAttribute("aria-label", branding.name);
      brand.appendChild(initial);
    };
    if (!branding.favicon && !branding.faviconUrl) {
      showInitial();
      return;
    }
    const image = document.createElement("img");
    image.alt = branding.name;
    image.addEventListener("error", showInitial, { once: true });
    if (branding.favicon) {
      const bytes = body.slice(branding.favicon.offset, branding.favicon.end);
      image.src = URL.createObjectURL(new Blob([bytes], { type: branding.favicon.mime }));
    } else {
      image.src = branding.faviconUrl;
    }
    brand.appendChild(image);
  }

  const ERROR_VIEWS = {
    invalid: "invalid-link-view",
    disconnected: "disconnected-view",
    registry: "registry-error-view",
    security: "security-error-view",
    content: "content-error-view",
    unavailable: "unavailable-content-view",
  };

  function showError(kind) {
    // Report exactly the stage already observable on the public protocol.
    // Token liveness is exposed by the envelope HTTP status; after an envelope
    // resolves, transport, authentication, and content failures are likewise
    // distinguishable to any holder of the capability URL.
    state.phase = "error";
    state.errorKind = Object.prototype.hasOwnProperty.call(ERROR_VIEWS, kind)
      ? kind : "content";
    setStatus("");
    show(ERROR_VIEWS[state.errorKind]);
  }

  //: Opens one fan-out frame. Mirrors channel.py's seal_stream_frame:
  //: [12B random nonce][AES-256-GCM ciphertext] with the domain string as
  //: AAD. Returns null when it does not authenticate -- which is also how
  //: a request RESPONSE is told apart from a pushed frame on the same
  //: channel, since a response is not sealed under the stream key.
  const STREAM_FRAME_DOMAIN = te.encode("autonomy.network.channel.stream.v1");
  const STREAM_NONCE_LEN = 12;

  async function openStreamFrame(streamKey, sealed) {
    if (!streamKey || !(sealed instanceof Uint8Array)) return null;
    if (sealed.length <= STREAM_NONCE_LEN) return null;
    try {
      const key = await crypto.subtle.importKey(
        "raw", streamKey, { name: "AES-GCM" }, false, ["decrypt"],
      );
      const plain = await crypto.subtle.decrypt(
        {
          name: "AES-GCM",
          iv: sealed.slice(0, STREAM_NONCE_LEN),
          additionalData: STREAM_FRAME_DOMAIN,
        },
        key,
        sealed.slice(STREAM_NONCE_LEN),
      );
      return new Uint8Array(plain);
    } catch (err) {
      return null;  // not ours, or tampered: indistinguishable on purpose
    }
  }

  let channelBroker = null;

  async function renderArtifact(header, body, attachmentContext = null) {
    const artifact = validateArtifact(header, body);
    state.bodyLength = body.length;
    renderBrand(artifact.branding, body);
    const frame = document.getElementById("artifact-frame");
    const capturedWindow = frame.contentWindow;
    let readySeen = false;
    let resolveReady;
    const ready = new Promise((resolve) => { resolveReady = resolve; });

    // Compare against the frame's window AT EVENT TIME. capturedWindow is
    // read before srcdoc is assigned, and whether a frame keeps its
    // contentWindow identity across that navigation is not something to bet
    // the channel on: if it does not match, `ready` is dropped, the port is
    // never handed over, and the viewer renders perfectly while every control
    // in it does nothing.
    const fromFrame = (event) =>
      event.source === frame.contentWindow || event.source === capturedWindow;

    window.addEventListener("message", (event) => {
      if (!fromFrame(event) || !event.data || event.data.v !== 1) return;
      if (event.data.op === "ready") {
        // A viewer may announce ready MORE THAN ONCE: after
        // document.open/write/close the replacement document runs its own
        // bootstrap and opens a fresh channel. Hand it a new port and drop
        // the previous document's, whose in-flight replies are no longer
        // anyone's business.
        if (channelBroker && attachmentContext && attachmentContext.channel) {
          const pair = new MessageChannel();
          channelBroker.attach(pair.port1);
          (frame.contentWindow || capturedWindow).postMessage(
            { v: 1, op: "port" }, "*", [pair.port2],
          );
        }
        if (!readySeen) {
          readySeen = true;
          resolveReady();
        }
      } else if (event.data.op === "title" && typeof event.data.title === "string") {
        state.artifactTitle = truncateCodePoints(event.data.title, MAX_TITLE_CHARS);
        setStatus(state.artifactTitle);
      } else if (event.data.op === "height" && Number.isSafeInteger(event.data.height)) {
        state.artifactHeight = Math.max(0, Math.min(event.data.height, 1000000));
      } else if (event.data.op === "chrome") {
        // A viewer that renders its own top bar asks for the viewport, and
        // the shell drops its header rather than stacking two bars. Declared
        // BY THE VIEWER, not inferred from a kind -- any viewer can own its
        // surface, and the shell learns nothing about what it is showing.
        //
        // Refused while an attachment export is offered: that button lives
        // in the shell's header, so hiding it would leave the download with
        // no way to be started.
        const exportButton = document.getElementById("attachment-export");
        const exportOffered = exportButton && !exportButton.hidden;
        document.body.classList.toggle(
          "surface-owned", event.data.own === true && !exportOffered,
        );
      }
    });
    if (attachmentController) {
      attachmentController.dispose();
      attachmentController = null;
    }
    if (channelBroker) {
      channelBroker.dispose();
      channelBroker = null;
    }
    if (attachmentContext && attachmentContext.channel) {
      channelBroker = new ChannelBroker(
        attachmentContext.channel, attachmentContext.transport || null
      );
    }
    // Manifest present -> the download controller runs. No kind check.
    if (artifact.attachments && attachmentContext) {
      attachmentController = new AttachmentController({
        frame,
        channel: attachmentContext.channel,
        token: attachmentContext.token,
        noteId: attachmentContext.noteId,
        manifest: artifact.attachments,
        exportButton: document.getElementById("attachment-export"),
      });
    }

    const viewerBytes = body.slice(
      artifact.viewer.offset, artifact.viewer.offset + artifact.viewer.length
    );
    const decoder = new TextDecoder("utf-8", { fatal: true });
    // WebKit can reject blob: HTML navigation in an HTTPS sandboxed iframe,
    // leaving the note viewer unable to emit its ready message. srcdoc keeps
    // the same sandboxed opaque origin without depending on blob navigation.
    const viewerHtml = decoder.decode(viewerBytes);
    // Every viewer takes the same path: the artifact's bytes become the
    // frame's document, unmodified. There is no per-kind branch here and
    // nothing rewrites a viewer's HTML -- a viewer that needs the channel
    // asks for a port like any other.
    document.body.classList.remove("surface-owned");
    frame.srcdoc = viewerHtml;

    // Wait for `ready` only when there is something to hand over. A viewer
    // that declares neither parts nor content is a self-contained document
    // and may never send one; waiting would hang it.
    if (artifact.parts) {
      await withTimeout(ready, VIEWER_READY_TIMEOUT_MS, "viewer ready");
    }

    // Generic parts: delivered to ANY viewer, contents never inspected.
    if (artifact.parts) {
      const generic = artifact.parts.map((part) => ({
        ref: part.ref, mime: part.mime,
        bytes: body.slice(part.offset, part.end).buffer,
      }));
      capturedWindow.postMessage(
        {
          v: 1, op: "parts", parts: generic,
          attachments: artifact.attachments || undefined,
        },
        "*", generic.map((x) => x.bytes)
      );
    }


    show("frame-view");
    state.phase = "rendered";
    setStatus(state.artifactTitle || "");
  }

  function assembleJoinContext(envelope, fragmentToken) {
    if (!envelope || typeof envelope !== "object" ||
        typeof envelope.org !== "string" || !envelope.org ||
        typeof envelope.root_pub !== "string" ||
        !/^[0-9a-f]{64}$/.test(envelope.root_pub) ||
        typeof envelope.invite_ref !== "string" ||
        !/^[0-9a-f]{64}$/.test(envelope.invite_ref) ||
        typeof fragmentToken !== "string" || fragmentToken.length === 0 ||
        fragmentToken.length > 128) {
      throw new Error("invalid organization invitation context");
    }
    return {
      org: envelope.org,
      rootPub: envelope.root_pub,
      inviteRef: envelope.invite_ref,
      token: fragmentToken,
    };
  }

  function deliverJoinContext(context) {
    const query = new URLSearchParams({
      org: context.org,
      root_pub: context.rootPub,
      invite_ref: context.inviteRef,
    });
    // BOTH credentials ride the fragment — never the query. The channel
    // token is itself a bearer-class credential (possession opens the
    // registry channel); although the registry saw it once in the /l/
    // path, copying it into a query would create a second server-visible
    // URL surface for no benefit. The fragment reaches the join page
    // (auto-y7nap) without touching any server, independent of whatever
    // access-log settings happen to be deployed.
    const fragment = new URLSearchParams({
      channel_token: (location.pathname || "").split("/").pop(),
      t: context.token,
    });
    const destination = "/network/join?" + query.toString() +
      "#" + fragment.toString();
    location.assign(destination);
    return destination;
  }

  async function boot() {
    const token = location.pathname.split("/").pop();
    if (!/^[0-9a-f]{32}$/.test(token)) return showError("invalid");

    try {
      state.phase = "envelope";
      setStatus("resolving…");
      let response;
      try {
        response = await fetch("/v1/links/" + token + "/envelope");
      } catch (err) {
        state.error = String(err && err.message || err);
        return showError("registry");
      }
      if (!response.ok) return showError("invalid");
      const envelope = await response.json();

      if (envelope.target_type === "org:join") {
        try {
          const fragmentToken = decodeURIComponent(
            location.hash.replace(/^#/, "")
          );
          const context = assembleJoinContext(envelope, fragmentToken);
          state.phase = "join";
          deliverJoinContext(context);
          return;
        } catch (err) {
          state.error = String(err && err.message || err);
          return showError("invalid");
        }
      }

      state.phase = "connecting";
      setStatus("connecting…");
      let transport;
      try {
        transport = await attemptEndpoints(envelope.endpoints, attemptDirectEndpoint);
        if (transport) {
          state.transport = "direct";
        } else {
          const scheme = location.protocol === "https:" ? "wss" : "ws";
          transport = await withTimeout(openSocket(
            scheme + "://" + location.host + "/v1/links/" + token + "/channel"),
            CONNECT_TIMEOUT_MS, "connect", "disconnected");
          state.transport = "relay";
        }
      } catch (err) {
        state.error = String(err && err.message || err);
        return showError("disconnected");
      }

      state.phase = "handshake";
      setStatus("securing…");
      // Bounded: a relay that accepts the socket but never sends SERVER_HELLO
      // (no serving tunnel dialed in) resolves to the offline error, not a hang.
      let channel;
      try {
        channel = await withTimeout(performHandshake(transport, {
          org: envelope.org, token, rootPub: envelope.root_pub,
        }), CONNECT_TIMEOUT_MS, "handshake", "disconnected");
      } catch (err) {
        try { transport.close(); } catch (_closeErr) { /* already closed */ }
        state.error = String(err && err.message || err);
        return showError(err && err.autonetKind === "disconnected"
          ? "disconnected" : "security");
      }

      state.phase = "fetching";
      setStatus("loading…");
      let artifact;
      try {
        artifact = await fetchArtifact(channel);
      } catch (err) {
        state.error = String(err && err.message || err);
        return showError(err && err.autonetKind === "disconnected"
          ? "disconnected" : "content");
      }
      const { header, body } = artifact;
      if (header.status !== "ok") return showError("unavailable");
      state.bodySha256 = bytesToHex(await crypto.subtle.digest("SHA-256", body));
      state.phase = "rendering";
      await renderArtifact(header, body, {
        // `transport` carries the socket's feed queue: the broker pumps
        // fan-out frames from it, separated from channel records by the
        // kind byte before either decoder sees one.
        channel, token, transport, noteId: envelope.target_uuid,
      });
    } catch (err) {
      state.error = String(err && err.message || err);
      showError("content");
    }
  }

  return {
    state, boot, canonicalJson, sendOp, attemptEndpoints,
    attemptDirectEndpoint, performHandshake, openSocket, fetchArtifact,
    attemptWebRtcUpgrade, dataChannelTransport,
    filterIceCandidate, sanitizeIceSdp, assertAddressFreeIceSdp,
    validateArtifact, renderArtifact, ChannelBroker,
    assembleJoinContext, deliverJoinContext,
    withTimeout, SecureChannel,
    attachmentCursorId, attachmentSinkId, safeAttachmentName,
    BrowserCursorStore, OpfsAttachmentSink,
    AttachmentTransferError, AttachmentDownloader, AttachmentController,
    channelAttachmentFetch,
    getAttachmentController: () => attachmentController,
  };
})();

window.autonet = autonet;
// Auto-boot only on the share-link shell (/l/<token>): other pages (the
// org:join bridge) load this file for its channel primitives and drive
// them explicitly — booting the /l/ flow there would just render its
// error state into a page that has no bootloader UI (auto-r7kk4).
if (typeof location !== "undefined" &&
    /^\/l\/[0-9a-f]{32}$/.test(location.pathname)) {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => autonet.boot());
  } else {
    autonet.boot();
  }
}
