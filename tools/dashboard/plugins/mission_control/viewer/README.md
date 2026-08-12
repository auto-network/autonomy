# Mission viewer

The platform chrome for a Mission Control screen. Prepended by
`_resolve_mission` to the coordinator's own HTML, so the browser parses one
document: doctype, `<base href="about:srcdoc">`, this bootstrap, then their
page verbatim.

- `bootstrap.js` — source. Runs before any coordinator script.
- `chrome.css` — Tailwind input; `@source` points at `bootstrap.js`.
- `.build/bootstrap.js` — generated, gitignored. Built on demand, never
  committed (see commit 628572bd for what committing a build output cost).

Design of record: `graph://9ff7c9a9-e48`. Approved chrome:
Design Studio `d4c1d064-152d-4099-8dff-34ce4055c962`, revision `3609ffec`.

## Why the CSS is a string inside the shadow root

The chrome lives in a **closed** shadow root, so its styles must be inside that
root — a `<style>` in the document would not reach it, and a `<link>` cannot be
fetched at all: the viewer travels inside artifact bytes under a CSP that
permits no external script or stylesheet.

That also makes the isolation mutual. Coordinator CSS cannot reach the chrome
(measured: `.bar{display:none}` hides light-DOM chrome outright), and the
chrome's CSS cannot leak into their page.

## What runs first, and why it matters

`document.scripts.length === 1` at the bootstrap's own execution proves it ran
before any coordinator script. It opens the `MessageChannel` itself, keeps
`port1` in a closure and transfers `port2` out. **No global is exposed**:
coordinator scripts share this realm, and a public handle would let them issue
channel operations.
