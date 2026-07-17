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
| `marked.min.js` | marked | 15.0.12 | https://cdn.jsdelivr.net/npm/marked@15.0.12/marked.min.js |
| `xterm.min.js` | @xterm/xterm | 5.5.0 | https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js |
| `xterm.min.css` | @xterm/xterm | 5.5.0 | https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.min.css |
| `addon-fit.min.js` | @xterm/addon-fit | 0.11.0 | https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.11.0/lib/addon-fit.min.js |
| `addon-clipboard.min.js` | @xterm/addon-clipboard | 0.2.0 | https://cdn.jsdelivr.net/npm/@xterm/addon-clipboard@0.2.0/lib/addon-clipboard.min.js |
| `purify.min.js` | dompurify | 3.4.12 | https://cdn.jsdelivr.net/npm/dompurify@3.4.12/dist/purify.min.js |
| `alpine.min.js` | alpinejs | 3.15.12 | https://cdn.jsdelivr.net/npm/alpinejs@3.15.12/dist/cdn.min.js |
| `html2canvas.min.js` | html2canvas | 1.4.1 | https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js |
| `tailwind-browser.min.js` | @tailwindcss/browser | 4.3.3 | https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4.3.3 |

```
sha256:
3e7e7d7feb3e5d58cb6c804f68ab5c24cc7e5eb6270fd6e5cbb9124739217d0c  marked.min.js
4196e242ef1cf4c2adead8d97f4a772a69576076f70b095e004b4abbb049e7bf  xterm.min.js
f7f724aea2bb620a6482bfb8e4bdecfae1152b0c7facef55fbda61f3b6cfedb2  xterm.min.css
696bd2890cb91f96b6db0a83103d49088892ff440bf01d2da654c905cff7696c  addon-fit.min.js
0afd18873b17701ad0ca0f037a2d178fd5bf2f022de39705a58e1eccf0ed0a84  addon-clipboard.min.js
c45ba939765574f96cbf35ee9b6d89f73756a17921814425e74b82f7c54603ce  purify.min.js
57b37d7cae9a27d965fdae4adcc844245dfdc407e655aee85dcfff3a08036a3f  alpine.min.js
e87e550794322e574a1fda0c1549a3c70dae5a93d9113417a429016838eab8cb  html2canvas.min.js
6d8c473ef2f8ad63feafc0bd76502dda31501a6c135dc4c6173f6268cde595be  tailwind-browser.min.js
```

To update: download the new pinned URL into this directory, refresh the
table + hashes, and grep the repo for any remaining CDN hostname
(`test_no_cdn_dependencies.py` pins this).

Pre-existing vendored assets: `highlightjs/` (syntax highlighting),
`openpgp.min.js` (commit signing).
