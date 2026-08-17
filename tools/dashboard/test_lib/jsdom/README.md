# Browser-free (jsdom) dashboard UI tests

Runs the dashboard's client JS + vendored Alpine inside **jsdom** (DOM-in-Node)
so data → render → interact → assert-DOM tests need no real browser. Faster and
not subject to the browser-boot/timing flakiness of the agent-browser path.

## Scope — what moves here, what does not
- **Category B (moves here):** assertions about *rendered structure and logic* —
  "N sessions → N picker buttons", "filter query hides non-matching rows",
  "clicking connect writes localStorage", event handlers, reactive text.
- **Category C (stays on a real browser):** anything reading **layout** —
  `getBoundingClientRect`, offsets, computed positioning, CSS overlap. jsdom has
  **no layout engine**; those return zeros. Do not migrate them.

## Why this is feasible here
The dashboard's static JS is **classic scripts** (0 ES imports/exports; components
register on `alpine:init`, helpers hang off `window`). So a test just injects the
script text into a jsdom document — no bundler, no module-graph resolution — the
same way `alpine_smoke.cjs` injects `static/vendor/alpine-3.15.12.min.js`.

## The validated recipe (proven by alpine_smoke.cjs)
1. Build a jsdom document from the page's HTML (for a full page, fetch the
   server-rendered fragment from the same mock server the browser harness starts;
   for a unit-level check, hand-write the minimal `x-data`/`x-for` markup).
2. Mock the data source: override `window.fetch` (and/or `XMLHttpRequest`) to
   return the fixture JSON the component would `GET` (e.g. `/api/dao/active_sessions`).
3. Inject `static/vendor/alpine-<v>.min.js` then the page's classic script(s)
   (e.g. `lib/session-store.js`), dispatch `DOMContentLoaded` so `alpine:init`
   fires and the store seeds from the mocked fetch.
4. Await a microtask/short tick, then assert on `document.querySelectorAll(...)`.

`alpine_smoke.cjs` exercises steps 1/3/4 (reactivity + `x-for` + input events);
the only piece a full-page migration adds is step 2 (fetch mock) + loading the
real component script and its server-rendered fragment.

## Toolchain
- `jsdom` is installed globally in the agent image with
  `NODE_PATH=/usr/lib/node_modules` (see `agents/Dockerfile`). Scripts are
  **CommonJS** (`require`) because `NODE_PATH` resolves globals for `require`
  but not for ESM `import`.
- `tests/test_jsdom_smoke.py` runs `alpine_smoke.cjs`; it **skips** when
  node/jsdom is absent (before the image is rebuilt) and passes once present.
