/* Fast browser-transport contract for the shipped autonet.js.
 *
 * This is deliberately a Node harness around the production browser code:
 * ICE shape validation, address scrubbing, DataChannel demultiplexing, and
 * upgrade ordering run without a browser.  A real-browser/WebRTC acceptance
 * remains the final integration gate; these tests are the millisecond
 * regression layer beneath it.
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

if (!globalThis.crypto) globalThis.crypto = require("crypto").webcrypto;
globalThis.window = {};
globalThis.document = { readyState: "loading", addEventListener() {} };

const source = fs.readFileSync(
  path.join(__dirname, "..", "..", "registry", "bootloader", "autonet.js"),
  "utf8",
);
vm.runInThisContext(source);
const A = globalThis.window.autonet;

function check(condition, message) {
  if (!condition) throw new Error(message);
}

function bytes(value) {
  return new TextEncoder().encode(value);
}

class FakeDataChannel {
  constructor(label, options) {
    this.label = label;
    this.options = options;
    this.readyState = "connecting";
    this.binaryType = "blob";
    this.bufferedAmount = 0;
    this.bufferedAmountLowThreshold = 0;
    this.sent = [];
    this.listeners = new Map();
  }

  addEventListener(name, callback) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(callback);
    this.listeners.set(name, listeners);
  }

  removeEventListener(name, callback) {
    this.listeners.set(name, (this.listeners.get(name) || []).filter((item) => item !== callback));
  }

  emit(name, event = {}) {
    for (const callback of this.listeners.get(name) || []) callback(event);
  }

  open() {
    this.readyState = "open";
    this.emit("open");
  }

  send(payload) {
    if (this.readyState !== "open") throw new Error("closed");
    this.sent.push(new Uint8Array(payload));
  }

  close() {
    if (this.readyState === "closed") return;
    this.readyState = "closed";
    this.emit("close");
  }
}

class FakePeerConnection {
  constructor(configuration) {
    this.configuration = configuration;
    this.iceGatheringState = "new";
    this.connectionState = "new";
    this.listeners = new Map();
    this.addedCandidates = [];
    FakePeerConnection.instances.push(this);
  }

  addEventListener(name, callback) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(callback);
    this.listeners.set(name, listeners);
  }

  removeEventListener(name, callback) {
    this.listeners.set(name, (this.listeners.get(name) || []).filter((item) => item !== callback));
  }

  emit(name, event = {}) {
    for (const callback of this.listeners.get(name) || []) callback(event);
  }

  createDataChannel(label, options) {
    this.channel = new FakeDataChannel(label, options);
    return this.channel;
  }

  async createOffer() {
    return { type: "offer", sdp: FakePeerConnection.offerSdp };
  }

  async setLocalDescription(description) {
    this.localDescription = description;
    this.iceGatheringState = "gathering";
    for (const candidate of FakePeerConnection.localCandidates) {
      this.emit("icecandidate", { candidate });
    }
    this.iceGatheringState = "complete";
    this.emit("icecandidate", { candidate: null });
    this.emit("icegatheringstatechange");
  }

  async setRemoteDescription(description) {
    this.remoteDescription = description;
    this.channel.open();
  }

  async addIceCandidate(candidate) {
    this.addedCandidates.push(candidate);
  }

  async getStats() {
    const pair = {
      id: "pair", type: "candidate-pair", state: "succeeded", localCandidateId: "local",
    };
    if (FakePeerConnection.statsShape === "pair") {
      pair.selected = true;
      pair.nominated = true;
    }
    const rows = [
      ["pair", pair],
      ["local", {
        id: "local", type: "local-candidate",
        candidateType: FakePeerConnection.selectedType,
      }],
    ];
    if (FakePeerConnection.statsShape === "transport") {
      rows.push(["transport", {
        id: "transport", type: "transport", selectedCandidatePairId: "pair",
      }]);
    }
    return new Map(rows);
  }

  close() {
    this.connectionState = "closed";
    if (this.channel) this.channel.close();
  }
}

FakePeerConnection.instances = [];
FakePeerConnection.selectedType = "relay";
FakePeerConnection.statsShape = "pair";
FakePeerConnection.offerSdp = [
  "v=0",
  "o=- 1 1 IN IP4 203.0.113.8",
  "c=IN IP4 192.168.1.4",
  "a=rtcp:9 IN IP4 10.0.0.4",
  "a=candidate:1 1 udp 1 192.168.1.4 5555 typ host",
  "a=end-of-candidates",
  "",
].join("\r\n");
FakePeerConnection.localCandidates = [];

function candidate(line, fields = {}) {
  return {
    candidate: line,
    sdpMid: "0",
    sdpMLineIndex: 0,
    usernameFragment: "ufrag",
    ...fields,
  };
}

function signalingChannel(config, answer) {
  const responses = [config, answer].map((value) => bytes(JSON.stringify(value)));
  return {
    sent: [],
    closed: false,
    async sendMessage(payload) { this.sent.push(JSON.parse(new TextDecoder().decode(payload))); },
    async recvMessage() { return responses.shift(); },
    close() { this.closed = true; },
  };
}

(async () => {
  check(A && typeof A.attemptWebRtcUpgrade === "function", "WebRTC adapter is not exported");

  const sanitized = A.sanitizeIceSdp(FakePeerConnection.offerSdp);
  check(!sanitized.includes("a=candidate:"), "candidate survived SDP sanitizing");
  check(!sanitized.includes("a=end-of-candidates"), "end marker survived SDP sanitizing");
  check(sanitized.includes("o=- 1 1 IN IP4 0.0.0.0"), "origin address was not neutralized");
  check(sanitized.includes("c=IN IP4 0.0.0.0"), "connection address was not neutralized");
  check(sanitized.includes("a=rtcp:9 IN IP4 0.0.0.0"), "RTCP address was not neutralized");

  let refused = false;
  try { A.assertAddressFreeIceSdp("v=0\r\nc=IN IP4 10.0.0.1\r\n"); } catch (_error) { refused = true; }
  check(refused, "remote SDP accepted a real connection address");

  const publicRelay = candidate(
    "candidate:1 1 udp 10 8.8.8.8 5000 typ relay raddr 1.1.1.1 rport 4000",
  );
  const scrubbedRelay = A.filterIceCandidate(publicRelay, "relay_only");
  check(scrubbedRelay.candidate.includes("raddr 0.0.0.0 rport 9"), "relay raddr leaked");
  check(A.filterIceCandidate(candidate(
    "candidate:2 1 udp 10 192.168.1.2 5000 typ host",
  ), "direct_allowed") === null, "literal host candidate leaked");
  check(A.filterIceCandidate(candidate(
    "candidate:3 1 udp 10 browser-name.local 5000 typ host",
  ), "direct_allowed") !== null, "mDNS host candidate was removed");
  check(A.filterIceCandidate(candidate(
    "candidate:3b 1 udp 10 100.64.1.2 5000 typ srflx raddr 192.168.1.2 rport 4000",
  ), "direct_allowed") === null, "non-global srflx should be omitted");
  const scrubbedSrflx = A.filterIceCandidate(candidate(
    "candidate:4 1 udp 10 8.8.8.8 5000 typ srflx raddr 192.168.1.2 rport 4000",
  ), "direct_allowed");
  check(scrubbedSrflx.candidate.includes("raddr 0.0.0.0 rport 9"),
    "private srflx related address leaked");
  const publicSrflx = A.filterIceCandidate(candidate(
    "candidate:5 1 udp 10 8.8.8.8 5000 typ srflx raddr 1.1.1.1 rport 4000",
  ), "direct_allowed");
  check(publicSrflx.candidate.includes("raddr 1.1.1.1 rport 4000"),
    "direct-allowed public related address was unnecessarily destroyed");

  const attemptId = "ab".repeat(16);
  const config = {
    v: 1,
    op: "ice.config",
    attempt_id: attemptId,
    policy: "relay_only",
    ice_servers: [
      { urls: ["stun:turn.auto.network:3478"] },
      {
        urls: [
          "turn:turn.auto.network:3478?transport=udp",
          "turn:turn.auto.network:3478?transport=tcp",
          "turns:turn.auto.network:443?transport=tcp",
        ],
        username: "future:user",
        credential: "secret",
        credentialType: "password",
      },
    ],
    expires_at: Math.floor(Date.now() / 1000) + 600,
  };
  const answer = {
    v: 1,
    op: "ice.answer",
    attempt_id: attemptId,
    sdp: "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\nc=IN IP4 0.0.0.0\r\n",
    candidates: [candidate(
      "candidate:9 1 udp 10 8.8.4.4 6000 typ relay raddr 0.0.0.0 rport 9",
    )],
  };
  const signaling = signalingChannel(config, answer);
  let authentications = 0;
  let proved = 0;
  const authenticatedDirect = { kind: "authenticated-direct" };
  const result = await A.attemptWebRtcUpgrade({
    openSignaling: async () => signaling,
    authenticateTransport: async (transport) => {
      authentications += 1;
      return transport === signaling ? signaling : authenticatedDirect;
    },
    proveApplication: async (channel) => {
      check(channel === authenticatedDirect, "application proof received the wrong channel");
      proved += 1;
    },
    RTCPeerConnectionImpl: FakePeerConnection,
    attemptId,
    nowSeconds: () => Math.floor(Date.now() / 1000),
  });

  const peer = FakePeerConnection.instances.at(-1);
  check(authentications === 2, "signaling and DataChannel were not freshly authenticated");
  check(proved === 1, "the direct path was selected without a real application exchange");
  check(signaling.sent[0].op === "ice.begin", "ICE did not begin on the fresh signaling channel");
  check(signaling.sent[1].op === "ice.offer", "ICE offer was not sent second");
  check(!signaling.sent[1].sdp.includes("a=candidate:"), "offer smuggled a candidate in SDP");
  check(peer.configuration.iceTransportPolicy === "relay", "relay-only was not enforced in the browser");
  check(peer.channel.label === "autonomy-v1" && peer.channel.options.ordered === true,
    "DataChannel label or ordering changed");
  check(peer.addedCandidates.length === 2 && peer.addedCandidates.at(-1) === null,
    "remote end-of-candidates was not applied");
  check(signaling.closed, "short-lived signaling channel stayed open");
  check(result.channel === authenticatedDirect && result.policy === "relay_only",
    "upgrade returned the wrong authenticated result");

  const directTransport = A.dataChannelTransport(peer.channel);
  peer.channel.emit("message", { data: new Uint8Array([0, 7, 8]).buffer });
  const incoming = await directTransport.recvBinary();
  check(incoming.length === 2 && incoming[0] === 7 && incoming[1] === 8,
    "DataChannel record kind was not removed");
  peer.channel.emit("message", { data: new Uint8Array([1, 9, 10]).buffer });
  const feed = await directTransport.recvFeed();
  check(feed.length === 2 && feed[0] === 9 && feed[1] === 10,
    "DataChannel feed kind was not routed to the feed queue");
  await directTransport.send(new Uint8Array([4, 5]));
  check(peer.channel.sent.at(-1)[0] === 4, "viewer record was incorrectly tagged on send");
  directTransport.close();

  // A failure after ICE but before the real application exchange closes both
  // transports and the peer; it never returns a half-authenticated path.
  const failedSignaling = signalingChannel(config, answer);
  FakePeerConnection.statsShape = "transport";
  let failed = false;
  try {
    await A.attemptWebRtcUpgrade({
      openSignaling: async () => failedSignaling,
      authenticateTransport: async (transport) => transport,
      proveApplication: async () => { throw new Error("application refused"); },
      RTCPeerConnectionImpl: FakePeerConnection,
      attemptId,
      nowSeconds: () => Math.floor(Date.now() / 1000),
    });
  } catch (_error) { failed = true; }
  const failedPeer = FakePeerConnection.instances.at(-1);
  check(failed, "failed application proof selected a direct path");
  check(failedSignaling.closed, "failed attempt leaked the signaling channel");
  check(failedPeer.connectionState === "closed", "failed attempt leaked the peer connection");

  console.log("PASS: browser WebRTC transport contract");
})().catch((error) => {
  console.error("FAIL:", error && error.stack || error);
  process.exit(1);
});
