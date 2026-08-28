# Vendored UI libraries

Served from the deployment itself (`/static/vendor/…`) so a fresh install
is fully self-contained: no CDN in the load path, works offline, and the
CSP contains no third-party script/style origins (sovereign-distribution
stance, DEPLOY.md). Do not reintroduce CDN URLs in templates or plugins —
vendor here instead.

Pinned versions, sources, and sha256 (downloaded 2026-07-17; the marked
pin matches what the previously used unversioned CDN URL actually served):

| File | Package | Version | Source URL |
|---|---|---|---|
| `marked-15.0.12.min.js` | marked | 15.0.12 | https://cdn.jsdelivr.net/npm/marked@15.0.12/marked-15.0.12.min.js |
| `xterm-5.5.0.min.js` | @xterm/xterm | 5.5.0 | https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm-5.5.0.min.js |
| `xterm-5.5.0.min.css` | @xterm/xterm | 5.5.0 | https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm-5.5.0.min.css |
| `addon-fit-0.11.0.min.js` | @xterm/addon-fit | 0.11.0 | https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.11.0/lib/addon-fit-0.11.0.min.js |
| `addon-clipboard-0.2.0.min.js` | @xterm/addon-clipboard | 0.2.0 | https://cdn.jsdelivr.net/npm/@xterm/addon-clipboard@0.2.0/lib/addon-clipboard-0.2.0.min.js |
| `purify-3.4.12.min.js` | dompurify | 3.4.12 | https://cdn.jsdelivr.net/npm/dompurify@3.4.12/dist/purify-3.4.12.min.js |
| `alpine-3.15.12.min.js` | alpinejs | 3.15.12 | https://cdn.jsdelivr.net/npm/alpinejs@3.15.12/dist/cdn.min.js |
| `html2canvas-1.4.1.min.js` | html2canvas | 1.4.1 | https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas-1.4.1.min.js |
| `tailwind-browser-4.3.3.min.js` | @tailwindcss/browser | 4.3.3 | https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4.3.3 |
| `hpke-x25519-chacha20poly1305-1.8.0.mjs` | @hpke/core + @hpke/dhkem-x25519 + @hpke/chacha20poly1305 + @hpke/common | 1.9.0 / 1.8.0 / 1.8.0 / 1.10.1 | https://www.npmjs.com/package/@hpke/core/v/1.9.0 |
| `openpgp-6.3.1.min.js` | openpgp | 6.3.1 | https://cdn.jsdelivr.net/npm/openpgp@6.3.1/dist/openpgp.min.js |
| `highlightjs/highlight-11.11.1.min.js` | @highlightjs/cdn-assets | 11.11.1 | https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.11.1/highlight.min.js |
| `highlightjs/github-dark-11.11.1.min.css` | @highlightjs/cdn-assets | 11.11.1 | https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.11.1/styles/github-dark.min.css |

```
sha256:
3e7e7d7feb3e5d58cb6c804f68ab5c24cc7e5eb6270fd6e5cbb9124739217d0c  marked-15.0.12.min.js
4196e242ef1cf4c2adead8d97f4a772a69576076f70b095e004b4abbb049e7bf  xterm-5.5.0.min.js
f7f724aea2bb620a6482bfb8e4bdecfae1152b0c7facef55fbda61f3b6cfedb2  xterm-5.5.0.min.css
696bd2890cb91f96b6db0a83103d49088892ff440bf01d2da654c905cff7696c  addon-fit-0.11.0.min.js
0afd18873b17701ad0ca0f037a2d178fd5bf2f022de39705a58e1eccf0ed0a84  addon-clipboard-0.2.0.min.js
c45ba939765574f96cbf35ee9b6d89f73756a17921814425e74b82f7c54603ce  purify-3.4.12.min.js
57b37d7cae9a27d965fdae4adcc844245dfdc407e655aee85dcfff3a08036a3f  alpine-3.15.12.min.js
e87e550794322e574a1fda0c1549a3c70dae5a93d9113417a429016838eab8cb  html2canvas-1.4.1.min.js
6d8c473ef2f8ad63feafc0bd76502dda31501a6c135dc4c6173f6268cde595be  tailwind-browser-4.3.3.min.js
621ad61d026f526711ad0842b03ab92d735a96c1f173a62005f196bb4ecac37d  hpke-x25519-chacha20poly1305-1.8.0.mjs
9736f49e81790af972029cd8416a8f9e5be7c4bddfb041676ab93fcad8332f5e  openpgp-6.3.1.min.js
c4a399dd6f488bc97a3546e3476747b3e714c99c57b9473154c6fb8d259b9381  highlightjs/highlight-11.11.1.min.js
9f208d022102b1d0c7aebfecd8e42ca7997d5de636649d2b31ea63093d809019  highlightjs/github-dark-11.11.1.min.css
```

To update: download the new pinned URL into this directory, refresh the
table + hashes, and grep the repo for any remaining CDN hostname
(`test_no_cdn_dependencies.py` pins this).

Every vendored file carries its version in its name, so a request for one
version can never be answered with another and the URL changes whenever the
file does. That is what lets a browser keep them: `/static/` grants a long
lifetime only to a request that names a version.

`highlightjs/` (syntax highlighting) and `openpgp-6.3.1.min.js` (commit
signing) predate this table and were listed here in prose, with no version,
source or checksum recorded, until they were added above.

## jsQR 1.4.0
Pure-JS QR decoder for the recovery-code "scan" verify path. MIT (Cosmo Wolfe),
version-pinned, sha256 bc40c8a15196236b2314db0856f72ca0b49980cd5413b8c852a7349f5fee0859.
Lazy-loaded only when the camera scanner opens.

## qrcode-generator 1.4.4
Compact pure-JS QR encoder (Kazuhiko Arase) for rendering the recovery code as
a printable SVG QR. MIT, version-pinned, sha256
18ae399f81182bc9de916e9c77b195df20cc58d6f2d55a62b085a299f1bf1780.

## jspdf-2.5.2.min.js
- Version: 2.5.2 (pinned) — MIT (see jspdf-2.5.2.min.js.LICENSE)
- Source: https://cdn.jsdelivr.net/npm/jspdf@2.5.2/dist/jspdf.umd.min.js
- sha256: 85ba2cc3ff858a20fa49fe6e457bec863ea40b55a9f3725e58a940e62f6f61a4
- Purpose: builds the recovery-code sheet as an in-memory PDF so the installed
  PWA can print via the iOS share sheet (window.print() is a no-op there).
- Loading: lazy — fetched only when the recovery enrollment wizard starts;
  referenced by no page template.
