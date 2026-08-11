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
    if (!hasOnlyKeys(header, ["v", "status", "kind", "viewer", "content", "branding"])
        || header.v !== 1 || header.status !== "ok"
        || !["note", "design", "present", "mission"].includes(header.kind)) {
      throw new Error("invalid artifact header");
    }

    const ranges = [validateSlice(header.viewer, body.length, "viewer", false)];
    let content = null;
    if (header.kind === "note") {
      if (!Object.prototype.hasOwnProperty.call(header, "content")
          || !hasOnlyKeys(header.content, ["title", "markdown", "parts", "attachments"])
          || typeof header.content.title !== "string"
          || codePointLength(header.content.title) > MAX_TITLE_CHARS
          || !Array.isArray(header.content.parts)) {
        throw new Error("invalid note content");
      }
      const markdown = validateSlice(
        header.content.markdown, body.length, "markdown"
      );
      ranges.push(markdown);
      const refs = new Set();
      const parts = header.content.parts.map((part) => {
        if (!hasOnlyKeys(part, ["ref", "mime", "offset", "length"])
            || typeof part.ref !== "string" || !part.ref
            || refs.has(part.ref) || typeof part.mime !== "string" || !part.mime) {
          throw new Error("invalid artifact part");
        }
        refs.add(part.ref);
        const range = validateSlice(
          { offset: part.offset, length: part.length }, body.length, "part"
        );
        ranges.push(range);
        return { ref: part.ref, mime: part.mime, ...range };
      });
      let attachments = [];
      if (Object.prototype.hasOwnProperty.call(header.content, "attachments")) {
        if (!Array.isArray(header.content.attachments)) {
          throw new Error("invalid note attachments");
        }
        const manifestRefs = new Set();
        attachments = header.content.attachments.map((entry) => {
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
      content = { title: header.content.title, markdown, parts, attachments };
    } else if (Object.prototype.hasOwnProperty.call(header, "content")) {
      throw new Error("content forbidden for design");
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
      content,
      branding,
    };
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

  // ── mission viewer bridge (auto-t2lz1) ──────────────────────────────
  //
  // A mission's page is authored against the dashboard's own origin --
  // fetch("/api/missions/<id>/questions"), links to
  // "/missions/<id>/pillars/<pid>". Over the relay the artifact runs in a
  // sandboxed srcdoc iframe whose base URL is the relay's, where none of
  // those paths exist: every one is a 404 and every click fails silently.
  //
  // The shim below is PREPENDED to the artifact HTML before it becomes
  // srcdoc, because a sandboxed frame is an opaque origin the parent can
  // never inject into after load. It lives here rather than in the
  // server's resolver so the coordinator's own bytes are still served
  // byte-for-byte, and so the interception exists only in the relay
  // viewer -- the same page keeps working unchanged on the dashboard.
  //
  // Inside the frame it overrides fetch() and intercepts pillar
  // navigation, turning each into a postMessage the parent answers by
  // issuing the matching read/write channel op. Pillar navigation becomes
  // an in-place document swap, never a page load: one link per mission,
  // navigation stays in-page.

  const MISSION_SHIM = `<script>(function () {
  var pending = {}, nextId = 1;
  function ask(op, body) {
    return new Promise(function (resolve) {
      var id = String(nextId++);
      pending[id] = resolve;
      parent.postMessage({ v: 1, op: "mc-request", id: id, mcOp: op, body: body }, "*");
    });
  }
  window.addEventListener("message", function (event) {
    if (event.source !== parent || !event.data || event.data.v !== 1) return;
    if (event.data.op !== "mc-response") return;
    var resolve = pending[event.data.id];
    if (!resolve) return;
    delete pending[event.data.id];
    resolve(event.data.result);
  });
  function reply(payload) {
    return new Response(JSON.stringify(payload === null ? {} : payload), {
      status: payload === null ? 502 : 200,
      headers: { "Content-Type": "application/json" },
    });
  }
  var realFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    var url = typeof input === "string" ? input : (input && input.url) || "";
    var method = ((init && init.method) || (input && input.method) || "GET").toUpperCase();
    var m;
    if ((m = url.match(/\\/api\\/(?:missions|pillars)\\/([^\\/?#]+)\\/questions\\/([^\\/?#]+)\\/reopen/))) {
      var rb = init && init.body ? JSON.parse(init.body) : {};
      return ask("write", { kind: "reopen", entry_id: m[2], followup: rb.followup }).then(reply);
    }
    if ((m = url.match(/\\/api\\/(missions|pillars)\\/([^\\/?#]+)\\/questions/))) {
      var isPillar = m[1] === "pillars";
      if (method === "POST") {
        var qb = init && init.body ? JSON.parse(init.body) : {};
        return ask("write", {
          kind: "question", question: qb.question, anchor: qb.anchor,
          pillar_id: isPillar ? m[2] : undefined,
        }).then(reply);
      }
      return ask("read", {
        kind: "questions", pillar_id: isPillar ? m[2] : undefined,
      }).then(reply);
    }
    if (url.indexOf("/pillars") !== -1 && url.indexOf("/api/missions/") !== -1) {
      return ask("read", { kind: "pillars" }).then(reply);
    }
    if (url.indexOf("dashboard.surface.presence") !== -1) {
      return ask("read", { kind: "presence" }).then(function (r) {
        // Shape-compatible with the Settings endpoint the page expects.
        return reply({ members: (r && r.presence || []).map(function (p) {
          return { key: p.participant_id, payload: p };
        }) });
      });
    }
    return realFetch(input, init);
  };
  // This script's own source, so it can be re-injected into whatever
  // document replaces this one -- a pillar page needs the same fetch
  // interception and the same way back.
  var SELF = document.currentScript && document.currentScript.textContent;
  var MISSION_HTML = null;
  document.addEventListener("DOMContentLoaded", function () {
    MISSION_HTML = document.documentElement.outerHTML;
  });
  function swap(html) {
    document.open();
    document.write(SELF ? "<scr" + "ipt>" + SELF + "</scr" + "ipt>" + html : html);
    document.close();
  }
  function openPillar(pillarId) {
    ask("read", { kind: "pillar_site", pillar_id: pillarId }).then(function (r) {
      if (r && r.html) swap(r.html);
    });
  }
  window.__mcOpenPillar = openPillar;
  document.addEventListener("click", function (event) {
    var el = event.target;
    while (el && el.tagName !== "A") el = el.parentElement;
    var href = el && el.getAttribute("href");
    if (!href) return;
    // A srcdoc document has NO base URL of its own, so it inherits the
    // parent's -- which means even a bare "#section" resolves to
    // relay.auto.network/l/<token>#section, a DIFFERENT document, and
    // navigates the frame off the artifact for good. Every in-page anchor
    // in the mission (53 of them in the OSS Insights binder: its whole
    // table of contents) breaks this way, not just pillar links. Scroll
    // instead of letting the browser navigate.
    if (href.charAt(0) === "#") {
      event.preventDefault();
      var id = href.slice(1);
      if (!id) { window.scrollTo(0, 0); return; }
      var target = document.getElementById(id)
        || document.getElementsByName(id)[0];
      if (target && target.scrollIntoView) {
        target.scrollIntoView({ behavior: "smooth", block: "start" });
      }
      return;
    }
    var pillar = href.match(/\\/missions\\/[^\\/]+\\/pillars\\/([^\\/?#]+)/);
    if (pillar) { event.preventDefault(); openPillar(pillar[1]); return; }
    // A link back to the mission itself: restore the artifact we already
    // hold rather than fetching anything.
    if (/\\/missions\\/[^\\/?#]+\\/?$/.test(href)) {
      event.preventDefault();
      if (MISSION_HTML) swap(MISSION_HTML);
      return;
    }
    // Any other same-site path would navigate the frame off the artifact
    // into a 404 on the relay's origin. Refuse it rather than destroy the
    // page; an absolute external link is left alone.
    if (href.charAt(0) === "/") event.preventDefault();
  }, true);
  // The page also navigates by assignment (window.location.href = ...),
  // which a sandbox without allow-top-navigation blocks outright. Give it
  // a settable shim that routes to the same in-place swap.
  try {
    var realAssign = window.location.assign.bind(window.location);
    Object.defineProperty(window, "location", {
      configurable: true,
      get: function () { return window.__mcLocation; },
      set: function (value) { window.__mcLocation.href = value; },
    });
    window.__mcLocation = {
      get href() { return document.baseURI; },
      set href(value) {
        var m = String(value).match(/\\/missions\\/[^\\/]+\\/pillars\\/([^\\/?#]+)/);
        if (m) { openPillar(m[1]); return; }
        realAssign(value);
      },
      assign: function (v) { this.href = v; },
      replace: function (v) { this.href = v; },
    };
  } catch (e) { /* a browser refusing the redefinition keeps link clicks */ }
})();<\/script>`;

  function missionShimmed(html) {
    return MISSION_SHIM + html;
  }

  class MissionBridge {
    /** Answers the shim's requests by issuing channel ops. The frame is
     * an opaque origin, so every inbound message is checked against the
     * captured window before it is trusted -- the same discipline the
     * note viewer's own message handling uses. */
    constructor({ frame, channel }) {
      this.childWindow = frame.contentWindow;
      this.channel = channel;
      this.queue = Promise.resolve();
      this.onMessage = this.onMessage.bind(this);
      window.addEventListener("message", this.onMessage);
    }

    dispose() {
      window.removeEventListener("message", this.onMessage);
    }

    onMessage(event) {
      if (event.source !== this.childWindow || !event.data) return;
      const msg = event.data;
      if (msg.v !== 1 || msg.op !== "mc-request") return;
      if (msg.mcOp !== "read" && msg.mcOp !== "write") return;
      // Serialize: one request/response exchange at a time on a channel.
      this.queue = this.queue.then(() => this.exchange(msg));
    }

    async exchange(msg) {
      let result = null;
      try {
        await this.channel.sendMessage(te.encode(canonicalJson({
          v: 1, op: msg.mcOp, body: msg.body && typeof msg.body === "object" ? msg.body : {},
        })));
        const raw = await this.channel.recvMessage();
        const line = new TextDecoder().decode(raw).split("\n")[0];
        const parsed = JSON.parse(line);
        if (parsed && parsed.status === "ok") result = parsed;
      } catch (err) {
        result = null;  // a failed exchange answers null, never hangs the page
      }
      this.childWindow.postMessage(
        { v: 1, op: "mc-response", id: msg.id, result }, "*",
      );
    }
  }

  let missionBridge = null;

  async function renderArtifact(header, body, attachmentContext = null) {
    const artifact = validateArtifact(header, body);
    state.bodyLength = body.length;
    renderBrand(artifact.branding, body);
    const frame = document.getElementById("artifact-frame");
    const capturedWindow = frame.contentWindow;
    let readySeen = false;
    let resolveReady;
    const ready = new Promise((resolve) => { resolveReady = resolve; });

    window.addEventListener("message", (event) => {
      if (event.source !== capturedWindow || !event.data || event.data.v !== 1) return;
      if (event.data.op === "ready" && artifact.kind === "note" && !readySeen) {
        readySeen = true;
        resolveReady();
      } else if (event.data.op === "title" && typeof event.data.title === "string") {
        state.artifactTitle = truncateCodePoints(event.data.title, MAX_TITLE_CHARS);
        setStatus(state.artifactTitle);
      } else if (event.data.op === "height" && Number.isSafeInteger(event.data.height)) {
        state.artifactHeight = Math.max(0, Math.min(event.data.height, 1000000));
      }
    });
    if (attachmentController) {
      attachmentController.dispose();
      attachmentController = null;
    }
    if (missionBridge) {
      missionBridge.dispose();
      missionBridge = null;
    }
    if (artifact.kind === "note" && attachmentContext) {
      attachmentController = new AttachmentController({
        frame,
        channel: attachmentContext.channel,
        token: attachmentContext.token,
        noteId: attachmentContext.noteId,
        manifest: artifact.content.attachments,
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
    if (artifact.kind === "mission" && attachmentContext) {
      // The bridge must exist before the document runs, or a shim request
      // fired on load would find nobody listening.
      missionBridge = new MissionBridge({
        frame, channel: attachmentContext.channel,
      });
      frame.srcdoc = missionShimmed(viewerHtml);
    } else {
      frame.srcdoc = viewerHtml;
    }

    if (artifact.kind === "note") {
      await withTimeout(ready, VIEWER_READY_TIMEOUT_MS, "viewer ready");
      const md = artifact.content.markdown;
      const parts = artifact.content.parts.map((part) => {
        const bytes = body.slice(part.offset, part.end).buffer;
        return { ref: part.ref, mime: part.mime, bytes };
      });
      const transfer = parts.map((part) => part.bytes);
      capturedWindow.postMessage({
        v: 1, op: "content", title: artifact.content.title,
        markdown: decoder.decode(body.slice(md.offset, md.end)), parts,
        attachments: artifact.content.attachments,
      }, "*", transfer);
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
    const destination = "/network/join?" + query.toString() +
      "#t=" + encodeURIComponent(context.token);
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
        channel, token, noteId: envelope.target_uuid,
      });
    } catch (err) {
      state.error = String(err && err.message || err);
      showError("content");
    }
  }

  return {
    state, boot, canonicalJson, verifyChain, attemptEndpoints,
    attemptDirectEndpoint, performHandshake, openSocket, fetchArtifact,
    validateArtifact, renderArtifact,
    MISSION_SHIM, missionShimmed, MissionBridge,
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
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => autonet.boot());
} else {
  autonet.boot();
}
