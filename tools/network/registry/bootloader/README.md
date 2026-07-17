# bootloader — auto.network `/l/{token}` viewer (B3)

The browser end of a share link (spec `graph://a17c8657-939` §5.3). The
registry serves this static page for every `/l/{token}`; it fetches the
grant envelope, opens the relay channel, runs the **viewer side** of the
B2 E2E handshake (this is the WebCrypto counterpart of
`tools/network/relaykit/channel.py`), and renders the artifact. Bead
`auto-5mvzf`.

## Files

- **`bootloader.html`** — the shell. A fixed byte string, served
  identically for live/expired/revoked/invented tokens (only the HTTP
  status differs, mirroring the envelope endpoint's liveness). No
  framework, no build step, no org/target/token identifiers in the
  bytes — the URL is a pure network pointer (§5.3).
- **`autonet.js`** — the channel client: canonical JSON (byte-identical
  to `idkit.canonical_json`), delegation-chain verification pinned to the
  envelope's `root_pub` (I5), X25519 + HKDF-SHA256 + AES-256-GCM record
  layer, chunk reassembly, the §5.4 `endpoints[]` direct-connect seam,
  and sandboxed rendering.

## Flow

1. `GET /v1/links/{token}/envelope` → `{org, root_pub, endpoints, …}`.
   The root pub is fetched over HTTPS *before* any channel bytes — it is
   the trust anchor for step 4.
2. `endpoints[]` seam (§5.4): try each announced endpoint first (empty in
   v1 → straight to relay). A working endpoint must still pass the same
   handshake — the pin does not change.
3. Open `WS /v1/links/{token}/channel`; send `CLIENT_HELLO`.
4. Receive `SERVER_HELLO`; verify its `tunnel:serve` cert chain to the
   pinned `root_pub` and the signature over `(org, token, client_eph,
   server_eph)`. A relay substituting either ECDH key or the cert fails
   here — **I5 on the client**.
5. Derive keys, `fetch` the artifact (channel protocol v1: request
   `{op:"fetch"}`, response = JSON header line + body), render.

## Rendering & the CSP decision

HTML artifacts load into `<iframe sandbox="allow-scripts">` via a blob:
URL — an **opaque origin**: no same-origin access, no top-navigation, no
forms/popups. That sandbox, not CSP, is the isolation boundary between
the untrusted artifact and this origin.

Target artifacts (Present decks, microsites) are interactive and must run
their own scripts. Because blob: iframes inherit the embedder's CSP in
Chromium, the shell's `script-src` admits `'unsafe-inline' blob:` so the
artifact's scripts run. This does not weaken the shell: the shell is a
fixed static byte string with no inline script and no interpolated data,
so it has no injection surface. `connect-src 'self'` still bars the
opaque-origin artifact from exfiltrating (an opaque origin never matches
`'self'`) — it renders, but cannot phone home.

Non-HTML content types get a minimal text viewer or a download link.

The artifact signals readiness by `postMessage({autonet_title})` upward —
the only cooperative channel across the sandbox boundary, and how the
acceptance harness asserts the inner document title.

## Tests

- `tools/network/registry/tests/test_bootloader.py` — served-bytes
  anti-enumeration (no identifiers, byte-identical across token states,
  status mirrors liveness, CSP/headers).
- `tools/network/registry/tests/test_bootloader_canonical.py` — pins
  `autonet.js` canonical JSON to `idkit.canonical_json` byte-for-byte
  (via Node).
- `tools/network/relaykit/tests/bootloader_harness.py` — the full
  headless-browser acceptance (1.55 MB+ HTML renders, sha + inner title
  asserted; error-page anti-enumeration; endpoints[] seam). Opt-in:
  `AUTONET_BROWSER_TEST=1 pytest .../test_bootloader_browser.py`, or run
  the harness module directly.
