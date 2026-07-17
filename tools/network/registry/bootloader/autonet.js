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
"use strict";

const autonet = (() => {
  const CERT_DOMAIN = "autonomy.idkit.cert.v1\n";
  const HANDSHAKE_DOMAIN = "autonomy.network.channel.handshake.v1\n";
  const KEYS_INFO = "autonomy.network.channel.keys.v1";
  const DIR_C2S = "c2s\x00";
  const DIR_S2C = "s2c\x00";
  const CHUNK_SIZE = 128 * 1024;
  const MAX_MESSAGE_SIZE = 64 * 1024 * 1024;
  const MAX_CHAIN_DEPTH = 16;
  const HANDSHAKE_VERSION = 1;

  const te = new TextEncoder();

  // ---- canonical JSON (byte-compatible with idkit canonical_json) --------

  function canonicalJson(value) {
    const parts = [];
    encodeValue(value, parts);
    return parts.join("");
  }

  function encodeValue(value, parts) {
    if (value === null) { parts.push("null"); return; }
    const kind = typeof value;
    if (kind === "boolean") { parts.push(value ? "true" : "false"); return; }
    if (kind === "number") {
      if (!Number.isInteger(value)) throw new Error("floats are not canonical");
      parts.push(String(value));
      return;
    }
    if (kind === "string") { parts.push(encodeString(value)); return; }
    if (Array.isArray(value)) {
      parts.push("[");
      value.forEach((item, i) => { if (i) parts.push(","); encodeValue(item, parts); });
      parts.push("]");
      return;
    }
    if (kind === "object") {
      parts.push("{");
      Object.keys(value).sort().forEach((key, i) => {
        if (i) parts.push(",");
        parts.push(encodeString(key), ":");
        encodeValue(value[key], parts);
      });
      parts.push("}");
      return;
    }
    throw new Error("type not allowed in canonical JSON: " + kind);
  }

  const SHORT_ESCAPES = { 8: "\\b", 9: "\\t", 10: "\\n", 12: "\\f", 13: "\\r", 34: '\\"', 92: "\\\\" };

  function encodeString(str) {
    // Matches Python json.dumps(ensure_ascii=True): shorthand escapes,
    // \u00XX for other control chars, \uXXXX per UTF-16 unit for >0x7E.
    let out = '"';
    for (let i = 0; i < str.length; i++) {
      const code = str.charCodeAt(i);
      if (SHORT_ESCAPES[code]) out += SHORT_ESCAPES[code];
      else if (code < 0x20 || code > 0x7e) out += "\\u" + code.toString(16).padStart(4, "0");
      else out += str[i];
    }
    return out + '"';
  }

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

  function concatBytes(...arrays) {
    const total = arrays.reduce((n, a) => n + a.length, 0);
    const out = new Uint8Array(total);
    let offset = 0;
    for (const a of arrays) { out.set(a, offset); offset += a.length; }
    return out;
  }

  function seqBytes(seq) {
    const out = new Uint8Array(8);
    new DataView(out.buffer).setBigUint64(0, BigInt(seq));
    return out;
  }

  // ---- delegation chain verification (I5 pin) ------------------------------

  async function ed25519Verify(pubHex, sigHex, dataBytes) {
    const key = await crypto.subtle.importKey(
      "raw", hexToBytes(pubHex, 64, "public key"), "Ed25519", false, ["verify"]);
    return crypto.subtle.verify("Ed25519", key, hexToBytes(sigHex, 128, "signature"), dataBytes);
  }

  function certPayloadDict(cert) {
    const payload = {
      v: cert.v, child_pub: cert.child_pub, scope: cert.scope, org: cert.org,
      subject: cert.subject, not_before: cert.not_before, not_after: cert.not_after,
    };
    if (cert.target_types !== undefined) payload.target_types = cert.target_types;
    if (cert.parent_cert !== undefined) payload.parent_cert = certToDict(cert.parent_cert);
    return payload;
  }

  function certToDict(cert) {
    const dict = certPayloadDict(cert);
    dict.sig = cert.sig;
    return dict;
  }

  function chainOf(cert) {
    const chain = [];
    for (let cursor = cert; cursor; cursor = cursor.parent_cert) {
      chain.push(cursor);
      if (chain.length > MAX_CHAIN_DEPTH) throw new Error("chain exceeds depth cap");
    }
    chain.reverse();
    return chain;
  }

  function isStrictSubset(child, parent) {
    const parentSet = new Set(parent);
    return child.every((s) => parentSet.has(s)) && child.length < parentSet.size;
  }

  /* Verify a cert chain against rootPubHex, mirroring idkit verify_chain:
   * per hop — signature over CERT_DOMAIN||payload against the parent key,
   * org match, time validity, strict scope/window/target_types narrowing.
   * Returns the leaf cert. Throws on any failure. */
  async function verifyChain(certWire, rootPubHex, org, requiredScope, now) {
    const cert = JSON.parse(certWire);
    // Anti-malleability: exactly one accepted byte form (idkit from_json).
    if (canonicalJson(cert) !== certWire) throw new Error("cert is not in canonical wire form");

    const chain = chainOf(cert);
    let signerPub = rootPubHex;
    let parent = null;
    for (const hop of chain) {
      if (hop.v !== 1) throw new Error("unsupported cert version");
      const payload = te.encode(CERT_DOMAIN + canonicalJson(certPayloadDict(hop)));
      if (!(await ed25519Verify(signerPub, hop.sig, payload))) {
        throw new Error("hop signature does not verify against its parent key");
      }
      if (hop.org !== org) throw new Error("cert org mismatch");
      if (now < hop.not_before || now > hop.not_after) throw new Error("cert hop outside validity window");
      if (parent) {
        if (!isStrictSubset(hop.scope, parent.scope)) throw new Error("scope escalation");
        if (hop.not_before < parent.not_before || hop.not_after >= parent.not_after) {
          throw new Error("validity window not nested");
        }
        if (parent.target_types !== undefined) {
          if (hop.target_types === undefined ||
              !hop.target_types.every((t) => parent.target_types.includes(t))) {
            throw new Error("target_types escalation");
          }
        }
      }
      signerPub = hop.child_pub;
      parent = hop;
    }
    const leaf = chain[chain.length - 1];
    if (requiredScope && !leaf.scope.includes(requiredScope)) {
      throw new Error("leaf lacks required scope " + requiredScope);
    }
    return leaf;
  }

  // ---- E2E handshake + record layer (mirrors relaykit/channel.py) ----------

  async function performHandshake(ws, { org, token, rootPub }) {
    const eph = await crypto.subtle.generateKey("X25519", false, ["deriveBits"]);
    const clientEph = bytesToHex(await crypto.subtle.exportKey("raw", eph.publicKey));
    ws.send(te.encode(canonicalJson({ v: HANDSHAKE_VERSION, eph_pub: clientEph })));

    const helloBytes = await ws.recvBinary();
    const hello = JSON.parse(new TextDecoder().decode(helloBytes));
    if (!hello || hello.v !== HANDSHAKE_VERSION || typeof hello.eph_pub !== "string" ||
        typeof hello.cert !== "string" || typeof hello.sig !== "string") {
      throw new Error("malformed SERVER_HELLO");
    }
    const now = Math.floor(Date.now() / 1000);
    const leaf = await verifyChain(hello.cert, rootPub, org, "tunnel:serve", now);
    const signedPayload = te.encode(HANDSHAKE_DOMAIN + canonicalJson({
      v: HANDSHAKE_VERSION, org, token, client_eph: clientEph, server_eph: hello.eph_pub,
    }));
    if (!(await ed25519Verify(leaf.child_pub, hello.sig, signedPayload))) {
      throw new Error("SERVER_HELLO signature does not verify (relay MITM?)");
    }

    const transcript = new Uint8Array(await crypto.subtle.digest("SHA-256",
      te.encode(HANDSHAKE_DOMAIN + canonicalJson({
        v: HANDSHAKE_VERSION, org, token, client_eph: clientEph,
        server_eph: hello.eph_pub, cert: hello.cert,
      }))));

    const serverKey = await crypto.subtle.importKey(
      "raw", hexToBytes(hello.eph_pub, 64, "server eph"), "X25519", false, []);
    const shared = await crypto.subtle.deriveBits({ name: "X25519", public: serverKey }, eph.privateKey, 256);
    const hkdfKey = await crypto.subtle.importKey("raw", shared, "HKDF", false, ["deriveBits"]);
    const okm = new Uint8Array(await crypto.subtle.deriveBits(
      { name: "HKDF", hash: "SHA-256", salt: transcript, info: te.encode(KEYS_INFO) }, hkdfKey, 512));

    const sendKey = await crypto.subtle.importKey("raw", okm.slice(0, 32), "AES-GCM", false, ["encrypt"]);
    const recvKey = await crypto.subtle.importKey("raw", okm.slice(32), "AES-GCM", false, ["decrypt"]);
    return new SecureChannel(ws, sendKey, recvKey, transcript);
  }

  class SecureChannel {
    constructor(ws, sendKey, recvKey, transcript) {
      this.ws = ws;
      this.sendKey = sendKey;
      this.recvKey = recvKey;
      this.transcript = transcript;
      this.sendSeq = 0;
      this.recvSeq = 0;
      this.sendDir = te.encode(DIR_C2S);
      this.recvDir = te.encode(DIR_S2C);
    }

    async sendMessage(bytes) {
      for (let offset = 0; ; offset += CHUNK_SIZE) {
        const chunk = bytes.slice(offset, offset + CHUNK_SIZE);
        const final = offset + CHUNK_SIZE >= bytes.length;
        const seq = seqBytes(this.sendSeq++);
        const plaintext = concatBytes(new Uint8Array([final ? 1 : 0]), chunk);
        const ciphertext = new Uint8Array(await crypto.subtle.encrypt(
          { name: "AES-GCM", iv: concatBytes(this.sendDir, seq),
            additionalData: concatBytes(this.transcript, this.sendDir, seq) },
          this.sendKey, plaintext));
        this.ws.send(concatBytes(seq, ciphertext));
        if (final) return;
      }
    }

    async recvMessage() {
      const parts = [];
      let size = 0;
      for (;;) {
        const record = await this.ws.recvBinary();
        const seq = record.slice(0, 8);
        if (new DataView(seq.buffer, seq.byteOffset).getBigUint64(0) !== BigInt(this.recvSeq)) {
          throw new Error("record out of sequence");
        }
        const plaintext = new Uint8Array(await crypto.subtle.decrypt(
          { name: "AES-GCM", iv: concatBytes(this.recvDir, seq),
            additionalData: concatBytes(this.transcript, this.recvDir, seq) },
          this.recvKey, record.slice(8)));
        this.recvSeq++;
        size += plaintext.length - 1;
        if (size > MAX_MESSAGE_SIZE) throw new Error("message exceeds maximum size");
        parts.push(plaintext.slice(1));
        if (plaintext[0] & 1) return concatBytes(...parts);
      }
    }
  }

  // ---- transport -----------------------------------------------------------

  /* WebSocket wrapped with an async binary receive queue. */
  function openSocket(url) {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(url);
      ws.binaryType = "arraybuffer";
      const queue = [];
      const waiters = [];
      let closed = null;
      const fail = (err) => {
        closed = err;
        while (waiters.length) waiters.shift().reject(err);
      };
      ws.onopen = () => resolve({
        send: (bytes) => ws.send(bytes),
        close: () => ws.close(),
        recvBinary: () => {
          if (queue.length) return Promise.resolve(queue.shift());
          if (closed) return Promise.reject(closed);
          return new Promise((res, rej) => waiters.push({ resolve: res, reject: rej }));
        },
      });
      ws.onmessage = (event) => {
        if (!(event.data instanceof ArrayBuffer)) return;
        const bytes = new Uint8Array(event.data);
        if (waiters.length) waiters.shift().resolve(bytes);
        else queue.push(bytes);
      };
      ws.onerror = () => { fail(new Error("websocket error")); reject(new Error("websocket error")); };
      ws.onclose = (event) => fail(new Error("websocket closed (" + event.code + ")"));
    });
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
  // response : JSON header line + "\n" + body bytes
  //            header: {v:1, status, content_type}

  async function fetchArtifact(channel) {
    await channel.sendMessage(te.encode(canonicalJson({ v: 1, op: "fetch" })));
    const response = await channel.recvMessage();
    const newline = response.indexOf(10);
    if (newline < 0) throw new Error("malformed artifact response");
    const header = JSON.parse(new TextDecoder().decode(response.slice(0, newline)));
    return { header, body: response.slice(newline + 1) };
  }

  // ---- page shell -----------------------------------------------------------

  const state = {
    phase: "init", transport: null, contentType: null, bodyLength: 0,
    bodySha256: null, artifactTitle: null, error: null,
  };

  function show(id) {
    for (const section of document.querySelectorAll("main > section")) {
      section.hidden = section.id !== id;
    }
  }

  function setStatus(text) {
    const el = document.getElementById("status-line");
    if (el) el.textContent = text;
  }

  function showError() {
    // ONE error view for every failure mode: unknown, expired, revoked,
    // dead binding, dashboard offline, handshake refused. Indistinguishable
    // by design (§5.3) — nothing here may depend on WHY it failed.
    state.phase = "error";
    show("error-view");
  }

  function render(header, body) {
    state.contentType = header.content_type || "application/octet-stream";
    state.bodyLength = body.length;
    const type = state.contentType.split(";")[0].trim();
    if (type === "text/html") {
      const frame = document.getElementById("artifact-frame");
      frame.src = URL.createObjectURL(new Blob([body], { type: "text/html" }));
      show("frame-view");
    } else if (type.startsWith("text/")) {
      document.getElementById("text-view-pre").textContent = new TextDecoder().decode(body);
      show("text-view");
    } else {
      const link = document.getElementById("download-link");
      link.href = URL.createObjectURL(new Blob([body], { type }));
      link.download = "artifact";
      show("download-view");
    }
    state.phase = "rendered";
  }

  async function boot() {
    window.addEventListener("message", (event) => {
      // Cooperative title signaling from the sandboxed artifact.
      if (event.data && typeof event.data.autonet_title === "string") {
        state.artifactTitle = event.data.autonet_title;
        setStatus(event.data.autonet_title);
      }
    });

    const token = location.pathname.split("/").pop();
    if (!/^[0-9a-f]{32}$/.test(token)) return showError();

    try {
      state.phase = "envelope";
      setStatus("resolving…");
      const response = await fetch("/v1/links/" + token + "/envelope");
      if (!response.ok) return showError();
      const envelope = await response.json();

      state.phase = "connecting";
      setStatus("connecting…");
      let transport = await attemptEndpoints(envelope.endpoints, attemptDirectEndpoint);
      if (transport) {
        state.transport = "direct";
      } else {
        const scheme = location.protocol === "https:" ? "wss" : "ws";
        transport = await openSocket(
          scheme + "://" + location.host + "/v1/links/" + token + "/channel");
        state.transport = "relay";
      }

      state.phase = "handshake";
      setStatus("securing…");
      const channel = await performHandshake(transport, {
        org: envelope.org, token, rootPub: envelope.root_pub,
      });

      state.phase = "fetching";
      setStatus("loading…");
      const { header, body } = await fetchArtifact(channel);
      if (header.status !== 200) return showError();
      state.bodySha256 = bytesToHex(await crypto.subtle.digest("SHA-256", body));
      render(header, body);
    } catch (err) {
      state.error = String(err && err.message || err);
      showError();
    }
  }

  return {
    state, boot, canonicalJson, verifyChain, attemptEndpoints,
    attemptDirectEndpoint, performHandshake, openSocket, fetchArtifact,
  };
})();

window.autonet = autonet;
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => autonet.boot());
} else {
  autonet.boot();
}
