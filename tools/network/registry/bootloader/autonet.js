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
        || !["note", "design", "present"].includes(header.kind)) {
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

  // ---- page shell -----------------------------------------------------------

  const state = {
    phase: "init", transport: null, bodyLength: 0,
    bodySha256: null, artifactTitle: null, artifactHeight: null,
    error: null, errorKind: null,
  };

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

  async function renderArtifact(header, body) {
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

    const viewerBytes = body.slice(
      artifact.viewer.offset, artifact.viewer.offset + artifact.viewer.length
    );
    const decoder = new TextDecoder("utf-8", { fatal: true });
    // WebKit can reject blob: HTML navigation in an HTTPS sandboxed iframe,
    // leaving the note viewer unable to emit its ready message. srcdoc keeps
    // the same sandboxed opaque origin without depending on blob navigation.
    frame.srcdoc = decoder.decode(viewerBytes);

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
      await renderArtifact(header, body);
    } catch (err) {
      state.error = String(err && err.message || err);
      showError("content");
    }
  }

  return {
    state, boot, canonicalJson, verifyChain, attemptEndpoints,
    attemptDirectEndpoint, performHandshake, openSocket, fetchArtifact,
    validateArtifact, renderArtifact,
    assembleJoinContext, deliverJoinContext,
    withTimeout,
  };
})();

window.autonet = autonet;
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => autonet.boot());
} else {
  autonet.boot();
}
