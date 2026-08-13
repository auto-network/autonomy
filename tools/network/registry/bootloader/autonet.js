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
  const ATTACHMENT_CHUNK_SIZE = 1024 * 1024;
  const ATTACHMENT_WINDOW_SIZE = 8 * ATTACHMENT_CHUNK_SIZE;
  const ATTACHMENT_LAST_IN_WINDOW = 0x01;
  const ATTACHMENT_EOF = 0x02;
  const MAX_MESSAGE_SIZE = 64 * 1024 * 1024;
  const MAX_ARTIFACT_BYTES = 48 * 1024 * 1024;
  const MAX_TITLE_CHARS = 500;
  const MAX_ATTACHMENT_NAME_CHARS = 255;
  const MAX_CHAIN_DEPTH = 16;
  const HANDSHAKE_VERSION = 1;
  // Bound establishing a live channel (connect + handshake). The relay may
  // ACCEPT a viewer socket and never send SERVER_HELLO — it holds the
  // connection open when no serving tunnel is dialed in for the org — and
  // recvBinary() waits forever, so without this the page hangs on a spinner
  // instead of showing the honest offline error. The body transfer that
  // follows is NOT bounded here: a large artifact may legitimately take time.
  const CONNECT_TIMEOUT_MS = 10000;
  const VIEWER_READY_TIMEOUT_MS = 10000;
  const STREAM_FINAL = 0x01;
  const MSG_END = 0x02;
  const KNOWN_RECORD_FLAGS = STREAM_FINAL | MSG_END;
  const ATTACHMENT_CURSOR_DOMAIN = "autonomy.attachment.cursor.v1";
  const ATTACHMENT_ERROR_CODES = new Set([
    "not_found", "not_authorized", "oversize", "out_of_range",
    "unavailable", "internal",
  ]);

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

    close() {
      if (this.ws && typeof this.ws.close === "function") this.ws.close();
    }

    async recvRecord() {
      try {
        const record = await this.ws.recvBinary();
        if (!(record instanceof Uint8Array) || record.length < 8 + 1 + 16) {
          throw new Error("record is too short");
        }
        const seq = record.slice(0, 8);
        if (new DataView(seq.buffer, seq.byteOffset, 8).getBigUint64(0) !== BigInt(this.recvSeq)) {
          throw new Error("record out of sequence");
        }
        const plaintext = new Uint8Array(await crypto.subtle.decrypt(
          { name: "AES-GCM", iv: concatBytes(this.recvDir, seq),
            additionalData: concatBytes(this.transcript, this.recvDir, seq) },
          this.recvKey, record.slice(8)));
        this.recvSeq++;
        if (plaintext.length < 1) throw new Error("record plaintext is empty");
        const flags = plaintext[0];
        const chunk = plaintext.slice(1);
        if (flags & ~KNOWN_RECORD_FLAGS) throw new Error("record has unknown flags");
        if (chunk.length > CHUNK_SIZE) throw new Error("record chunk exceeds maximum size");
        return { flags, chunk };
      } catch (err) {
        // Authentication, sequence, and record-framing failures poison the
        // stream. Never leave unread records available to a later exchange.
        this.close();
        throw err;
      }
    }

    async *recvMessageStream() {
      let parts = [];
      let size = 0;
      for (;;) {
        const { flags, chunk } = await this.recvRecord();
        size += chunk.length;
        if (size > MAX_MESSAGE_SIZE) throw new Error("message exceeds maximum size");
        parts.push(chunk);
        // STREAM_FINAL is the deployed v1 final bit, so it also terminates
        // the current message for compatibility with one-shot peers.
        const messageEnd = Boolean(flags & (MSG_END | STREAM_FINAL));
        if (messageEnd) {
          yield concatBytes(...parts);
          parts = [];
          size = 0;
        }
        if (flags & STREAM_FINAL) return;
      }
    }

    async recvMessage() {
      let message = null;
      for await (const candidate of this.recvMessageStream()) {
        if (message !== null) {
          throw new Error("one-shot response carried multiple messages");
        }
        message = candidate;
      }
      if (message === null) throw new Error("response exchange ended without a message");
      return message;
    }
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

  /* WebSocket wrapped with async receive queues -- records and feed frames. */
  function openSocket(url) {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(url);
      ws.binaryType = "arraybuffer";
      const queue = [];
      const waiters = [];
      const feedQueue = [];
      const feedWaiters = [];
      let closed = null;
      const fail = (err) => {
        closed = err;
        while (waiters.length) waiters.shift().reject(err);
        while (feedWaiters.length) feedWaiters.shift().reject(err);
      };
      const take = (q, w) => {
        if (q.length) return Promise.resolve(q.shift());
        if (closed) return Promise.reject(closed);
        return new Promise((res, rej) => w.push({ resolve: res, reject: rej }));
      };
      ws.onopen = () => resolve({
        send: (bytes) => ws.send(bytes),
        close: () => ws.close(),
        recvBinary: () => take(queue, waiters),
        /* Sealed feed frame; opens with the stream key from `subscribe`,
         * NOT this channel's key. */
        recvFeed: () => take(feedQueue, feedWaiters),
      });
      ws.onmessage = (event) => {
        if (!(event.data instanceof ArrayBuffer)) return;
        const tagged = new Uint8Array(event.data);
        if (!tagged.length) return;
        const payload = tagged.subarray(1);
        if (tagged[0] === VIEWER_KIND_FEED) {
          if (feedWaiters.length) feedWaiters.shift().resolve(payload);
          else feedQueue.push(payload);
          return;
        }
        if (tagged[0] !== VIEWER_KIND_RECORD) return;   // unknown kind: ignore
        if (waiters.length) waiters.shift().resolve(payload);
        else queue.push(payload);
      };
      ws.onerror = () => {
        const error = typedError("disconnected", "websocket error");
        fail(error);
        reject(error);
      };
      ws.onclose = (event) => fail(typedError(
        "disconnected", "websocket closed (" + event.code + ")"
      ));
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
          const body = await this.exchange({
            v: 1, op: message.op, body: message.body,
          });
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
      const encoder = new TextEncoder();
      await this.channel.sendMessage(encoder.encode(JSON.stringify(request)));
      const raw = await this.channel.recvMessage();
      const text = new TextDecoder("utf-8", { fatal: true }).decode(raw);
      const newline = text.indexOf("\n");
      return JSON.parse(newline === -1 ? text : text.slice(0, newline));
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
    state, boot, canonicalJson, verifyChain, attemptEndpoints,
    attemptDirectEndpoint, performHandshake, openSocket, fetchArtifact,
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
