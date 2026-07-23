# bootloader — relay.auto.network `/l/{token}` viewer

The browser end of an anonymous relay share. `registry.auto.network` remains
the control endpoint; `relay.auto.network` serves this fixed shell for every
`/l/{token}`. It fetches the grant envelope, opens the relay
channel, authenticates the serving connector, validates the artifact wire
format, and renders the artifact in a sandboxed iframe.

## Files

- **`bootloader.html`** — fixed shell bytes with no org, target, or token
  interpolation.
- **`autonet.js`** — the channel client: canonical JSON (byte-identical
  to `idkit.canonical_json`), delegation-chain verification pinned to the
  envelope's `root_pub`, X25519 + HKDF-SHA256 + AES-256-GCM records,
  chunk reassembly, strict artifact validation, and sandboxed rendering.

The shell and script are deployed as one protocol unit. The script response is
`Cache-Control: no-store`, the shell carries an asset-version query to evict
the former five-minute cache immediately, and deployments must activate the
shell and script together. There is no compatibility fallback.

## Flow

1. `GET /v1/links/{token}/envelope` → `{org, root_pub, endpoints, …}`.
   The root pub is fetched over HTTPS *before* any channel bytes — it is
   the trust anchor for step 4.
2. Try each announced `endpoints[]` entry, then fall back to the relay.
   Every transport must pass the same authenticated handshake.
3. Open `WS /v1/links/{token}/channel`; send `CLIENT_HELLO`.
4. Receive `SERVER_HELLO`; verify its `tunnel:serve` cert chain to the
   pinned `root_pub` and the signature over `(org, token, client_eph,
   server_eph)`. A relay substituting either ECDH key or the cert fails
   here — **I5 on the client**.
5. Derive keys and send `{v:1,op:"fetch"}`. The response is a JSON header
   line followed by a part-addressed body.
6. Require `status:"ok"`, an allowed discriminated union, safe in-bounds
   non-overlapping slices, unique part refs, a non-empty viewer, authenticated
   bounded org branding, and a body no larger than 48 MiB.
7. Load the viewer bytes. Notes wait for one authenticated `ready` message,
   then receive Markdown and image parts once. Designs and Present artifacts
   execute their existing HTML unchanged.

The static shell remains identical for every link and shows `auto.network`
while resolving. After an authenticated artifact validates, the header replaces
that text with the serving org's favicon. Local dashboard icons travel as
bounded artifact parts; HTTPS icons load under the page's no-referrer policy;
an org without an icon uses its established color/initial identity.

## Rendering & the CSP decision

Artifacts load through `srcdoc` into an iframe with
`sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"`.
The frame has an opaque origin with no same-origin parent access, top
navigation, or forms. External links may open in a new browsing context.

The embedded document inherits the shell CSP. `script-src` admits inline and
blob scripts so existing Present and Design viewers remain executable.
`connect-src` and `img-src` admit public-network resources. The response's
`Referrer-Policy: no-referrer` and the viewer's meta policy prevent bearer
URLs from being sent as Referer headers. `frame-ancestors 'none'` prevents
third-party framing of share pages.

The parent accepts `ready`, `title`, and `height` messages only from the
captured artifact window. A 10-second ready timeout prevents a malformed
note viewer from hanging indefinitely. Note Markdown is rendered with
vendored Marked, sanitized with vendored DOMPurify, then highlighted with
vendored highlight.js. `cid:` images resolve only to serialized owning-note
parts; missing parts become local placeholders. HTTP(S) links open with
`noopener noreferrer`; graph links remain inert text.

## Tests

- `tools/network/registry/tests/test_bootloader.py` — static shell bytes,
  observable failure states, CSP, response headers, and sandbox capabilities.
- `tools/network/registry/tests/test_bootloader_canonical.py` — canonical
  JSON parity with Python.
- `tools/network/registry/tests/test_bootloader_artifact.py` — strict union,
  range, overlap, ref, and size validation.
- `tools/dashboard/tests/relay_viewer/test_note_viewer.py` — deterministic,
  content-free generated viewer and safe render ordering.
- `tools/network/relaykit/tests/bootloader_harness.py` — full authenticated
  headless-browser acceptance for rich notes, controlled remote images,
  sanitation, link behavior, source-authenticated messaging, executable
  designs, and truthful invalid/disconnected failure views. Opt-in:
  `AUTONET_BROWSER_TEST=1 pytest .../test_bootloader_browser.py`, or run
  the harness module directly.
