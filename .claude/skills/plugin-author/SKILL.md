---
name: plugin-author
description: Author a complete dashboard plugin end to end — scaffold, settings schemas, API routes, dynamic CLI, embedded agent skill, jsdom tests, enablement and cutover. Use when creating or substantially extending a dashboard plugin, wiring plugin.yaml entrypoints, or registering plugin-owned settings sets.
---

# Authoring a Dashboard Plugin

A plugin is **a directory + a YAML manifest + a Setting row**. The
substrate owns discovery, validation, auth, routing, asset serving,
settings reconciliation, CLI mounting, and skill embedding — you write
the manifest and the modules it names, nothing else. The `mission`
plugin (`tools/dashboard/plugins/mission/`) is the worked example for
every step here; read it alongside this skill.

Before defining a new plugin, schema, route family, store, or shared UI
mechanism, run the `prior-art-analysis` skill. Record which existing plugin or
substrate seam will be consumed or extended and the exact delta that remains;
do not mint a parallel plugin-private primitive because the existing mechanism
was named for another product.

Work the steps in order. Each one is verifiable before the next.

## 1. Scaffold, dormant

```
tools/dashboard/plugins/<id>/
  plugin.yaml   __init__.py   page.html   page.js
  entrypoints/__init__.py
```

- Directory name = plugin id, **Python-import-friendly** (no hyphens):
  the loader imports `entrypoints.*` strings with importlib.
- `default_enabled: false` while building. Two reasons: nothing shows
  in the dashboard until you opt in, and a new default-enabled plugin
  breaks the substrate's dormant-baseline test.
- Required manifest fields: `id`, `api_version: 1`, `org`, `paths`
  (page URLs), `assets` (template/script), `nav.label`,
  `frontend.alpine_root`.

Verify before writing any code:

```python
from tools.dashboard.plugin_api import loader
[p.manifest for p in loader.discover() if p.manifest.id == "<id>"]
loader.load_all()      # resolves every entrypoint; raises on bad specs
```

## 2. Settings sets

**Read the three key notes first**: How to Write a Setting
(`graph://d72e25ec-a41` — the procedure: SCOPE → CARDINALITY → KEY →
PAYLOAD → EDGES → PUBLICATION → READ), the mechanism note it links, and
the scope rubric. Then:

- Schemas live in `entrypoints/schemas.py`, one class per set, declared
  with `@home`, `@publication_band`, and a cardinality decorator; list
  each class under `entrypoints.schemas` in the manifest.
- Namespace the set ids by the plugin (`<id>.registry`, `<id>.item`) —
  never `dashboard.*`.
- **Key segments are never repeated as payload fields** (merge-gated).
  Readers recover them from the member key; composite keys are
  parent-prefixed (`<mission>:<pillar>:<item>`) so children of a parent
  are a key-prefix selection, and keys stay globally unique.
- The substrate enforces declarations (required, enums, undeclared
  fields, element shapes) on every write. Override `validate()` ONLY
  for cross-field rules a declaration cannot express.
- Trails: the store upserts rows whole and keeps no versions. If a row
  needs history, declare explicit event arrays (`history`, `work`) and
  append at the write path.

Verify: `validate_payload(set_id, rev, payload)` round-trips your
vocabulary, refuses your cross-field violations, refuses undeclared
fields.

## 3. API routes

`entrypoints.api: <module>:routes` — a plain list of Starlette Routes
with absolute paths (`/api/<id>/...`). The substrate wraps them in
**default-deny auth**; never add your own.

- **Org scope enforcement is YOUR job, and it is the security
  boundary.** Plugin code runs in the dashboard process and can open
  any org's database — nothing below your handler stops a cross-org
  read. The middleware-established principal is the only legitimate
  input, and there are exactly three legs:
  1. **Org-bound session** (`principal.org_bound`): serve exactly
     `organization_scope_from_request(request)` — the middleware pins
     it from the bearer and ignores conflicting headers. Resolve a
     resource's owning org by probing **only** that org, so another
     org's resource ids return the same 404 as nonexistent ones (no
     existence oracle).
  2. **Global authority** (`principal.global_authority` — operator
     cookie, local host session): aggregate across
     `cross_org.list_org_slugs()`. Never let this caller's empty org
     selection resolve to the personal database.
  3. **Anything else**: empty scope. Fail closed — no fallback to
     personal, no default org.
  Never read an org from a path, query, or body — those are inputs to
  the identity middleware, not sources of authority. Pin the three
  legs with tests (the mission plugin's `test_org_isolation.py` is the
  template). Keep settings reads `peers=[]` and org-content sets
  banded `raw..curated` so federation isn't a fourth door.
- Attribution: `principal_from_request(request).subject` — identity is
  stamped at the boundary, never taken from the payload.
- Serve screens as **complete documents with data baked in** (a pure
  settings→bytes compose function). Baking keeps rendering testable,
  spares N fetches, and leaves the door open for relay sealing later.
  **You own the whole document shell**: doctype, charset, and the
  viewport meta (`width=device-width, initial-scale=1`) — things a
  platform composer used to add invisibly. Without the viewport meta,
  phones lay out at ~980px and shrink everything; pin the shell with a
  test, and look at the screen on a real phone before calling it done.
- Your own templates are **trusted**: same-origin fetch from them is
  fine. (Untrusted author-HTML surfaces are the opposite regime —
  platform-chrome mediation; know which one you are serving.)
- Domain refusals: raise a dedicated exception in the write layer, map
  it to 400 with the message; handler faults must never leak traces.
- Storage precedes delivery: persist first, then relay (CrossTalk via
  `tools.dashboard.crosstalk_delivery`) best-effort.

## 3a. Design the surfaces in Design Studio — as the real thing

The correct workflow, proven across the mission viewer's entire
design arc: **design exactly what the final product will be** — the
studio file IS the production template, not a mockup (the studio
prime directive: production UI verbatim, no non-production chrome).

Set up the live loop once, then never "publish" manually again:

1. Put the real template in a working directory and run
   `graph ui-design "Title" <dir>` — the watcher auto-posts a revision
   on every file save.
2. Add a local rebuild loop if the template needs composing (fixture
   data baked in) so a save → composed preview → revision, hands-free.
3. The operator reviews the live design page (often by dictation) and
   you edit files; every save appears on their screen in seconds. No
   confirm step, no export step, ever.
4. When the design is called done, the file ships **verbatim** as the
   plugin's template — the mission viewer moved from studio to
   production as a copy, byte-for-byte, because it was never anything
   but the production file.

Fixture data must be schema-shaped from day one: the studio fixtures
became the migration draft AND the integration-test scenarios. Design
with the data model you intend to ship.

## 3b. The page fragment: containment and continuity

- **Documents render inside the shell, never as navigations.** If your
  plugin serves complete documents, the fragment embeds them in a
  same-origin iframe. `window.location` to your document tears down
  the dashboard SPA — and shell services live there: the voice/
  dictation layer died on every mission open until this was fixed.
  Cross the frame boundary with postMessage seams, not navigation.
- **URLs name the page; they never grow history.** Use
  `history.replaceState` for every internal step, not `pushState` —
  the operator ruled pushState "way too heavy": the back button must
  stay a pure exit from the plugin, while the address bar always shows
  a copyable name for the current page (deep-linkable on load). For
  sub-page state, have the embedded document postMessage its position
  up (`{type:"<app>:where", ...}`) and mirror it into the hash.
- **The fragment speaks your documents' design language.** A scoped
  style block carrying your palette/type tokens, your drawn SVG icons,
  your control idioms — a generic-Tailwind list page next to a
  carefully designed document reads broken. Phone discipline applies
  to fragments too: auto-fit single-line titles between a ceiling and
  a floor, one-line metas in the icon vocabulary instead of word
  labels, and look at a real phone before calling it done.

### Full-frame seating and the shell toolbar

- **Full frame:** the shell mounts fragments inside `<main id="content"
  class="pt-6 px-6">`. A plugin that owns its whole surface escapes
  that padding on its ROOT element: `margin:-1.5rem -1.5rem 0;
  height:calc(100% + 1.5rem)`. Without this your page floats in a wide
  border no inner CSS can remove.
- **The toolbar is claimable — do not rebuild it.** The shell ships
  `#app-topbar-slot` beside the global search input, hidden until a
  page claims it: add `app-topbar-active` to `<header>` (hides search
  + page-title, shows the slot) and teleport your controls in with
  `<template x-teleport="#app-topbar-slot">`. The SPA router's
  `resetTopbar()` removes the class on every navigation — no cleanup
  code needed. The org dropdown idiom to copy lives in the worktrees
  page (`pages/worktrees.html`, `worktrees-org-select`); orgs come
  from `/api/orgs` (identity payload carries name/color/initial).

### Loading interstitials that tell the truth

A document that takes >300ms to compose gets a pre-rendered overlay
(visible in the same frame as the tap) with a progress bar carrying a
real signal — never a spinner. The whole pattern fits one round trip:
the screen endpoint takes `?progress=1` and returns a
`StreamingResponse` that yields HTML-comment stage markers
(`<!--app:42|Loading items — 12 of 78 settings-->`) between compose
stages, then `<!--app:doc:<bytes>-->`, then the document; the client's
`getReader()` loop parses markers into bar % + an italic sub-status,
uses the doc marker for byte progress, strips everything before
`<!doctype` and mounts via `srcdoc`. Upfront counts come from
`graph_ops.count_set_rows(set_id, org=..., prefix=...)` — a COUNT(*)
built for exactly this. Mission Control is the reference
implementation (`compose.render_stages`, `page.js loadScreen`).

## 3c. Attribution and identity

Acts are attributed by IDENTITY; presentation resolves at render.
Store an agent's session name, or a member's **org-scoped persona
public key** (`autonomy.network.persona` — per-org, unlinkable; the
personal root key never appears in org data). Never store a resolved
display name: screens resolve keys through the org member directory
(`autonomy.org.member-profile`), so a rename re-labels history; render
unknown keys truncated, never raw hex. A local personal-name fallback
is render-only — never persisted into an org row.

**Storage shape is a signing decision.** If rows should someday carry
per-writer signed identity (design `graph://21a0da9e-1c2`), model one
row per act (`@append_only_log`), not an aggregate array — the
envelope signs rows, and the cross-member merge is the substrate's
per-signer slot resolution, free.

## 4. Dynamic CLI

`entrypoints.cli: <module>:register` — `register(subparsers)`, the same
shape as built-in graph command modules. The substrate
(`tools/graph/plugin_cli.py`) mounts it **only while the plugin is
enabled** for its org; an optional `cli_workspaces` allow-list in the
Setting payload gates further by `$AUTONOMY_WORKSPACE`.

- Keep the module import-light (urllib, argparse): it loads on every
  `graph` invocation once enabled.
- Sessions hold no org database — every verb is one authenticated HTTP
  call against your plugin's routes (`GRAPH_API` + bearer). Add the GET
  routes the CLI needs; do not read settings from the CLI.
- A verb collision or a faulting register is contained per plugin (a
  stderr note), but don't rely on it: one verb, one owner. If you are
  replacing a statically registered command, unhook the static one in
  the same commit that enables the plugin.

Verify both ways: mounted with `payload_reader=lambda o: {"<id>":
{"enabled": True}}`, absent with `{}`.

## 5. Embedded agent skill

`skill: SKILL.md` in the manifest. It is delivered into every session's
workspace primer **while the plugin is enabled** and served at
`GET /api/plugins/<id>/skill`. Write it as role playbooks around your
domain's doctrine — what each kind of agent does with your verbs — not
as a route listing (put the route table at the end).

## 6. Tests (agent-test only)

Three layers, in `tools/dashboard/plugins/<id>/tests/`:

1. **Schema tests** — your cross-field rules plus one probe proving the
   substrate's declaration enforcement is active.
2. **Pure-function tests** — compose/write paths against an in-memory
   store whose `add_setting` runs the REAL `validate_payload`, so a
   write your schema would refuse fails in the test too.
3. **jsdom integration** — the platform convention: `.cjs` scripts
   (self-contained node, exit 0 + print PASS) driven by a parametrized
   python runner that skips when node/jsdom is missing. Compose a real
   document from scenario payloads and assert on the rendered DOM.

Run everything through `agent-test`; never pytest directly.

## 7. Enablement and cutover

State lives in the `dashboard.plugin` Setting (keyed by plugin id, in
the plugin's own org): `{"enabled": true|false, "cli_workspaces":
[...]}`. No row → manifest `default_enabled` → underscore-dir
convention → on.

Cutover from a legacy surface is one commit: flip `default_enabled`
(or write the Setting), unhook any static CLI registration you are
replacing, and leave the legacy plugin untouched until its own
deletion commit. Plugin-declared settings reconcile on enable
(`plugin_api/settings.py`); removal has an uninstall path.

## 8. What is NOT free yet

Relay serving (guest links over the encrypted channel) is not
manifest-driven today: it takes one read handler, one write handler,
and an event-topics consumer, hand-registered in `link_serving`'s
dispatch tables. The security core (one attributed funnel per
direction) is deliberate and stays; the registration becoming a
manifest stanza is `auto-ydvz0`. Until then, ship direct-access-only
and keep documents whole so sealing can come later.

## Test with both credentials — the org-scope trap

`organization_scope_from_request` returns the bearer's org for an
org-bound session, but for a dashboard operator with no explicit org
selection it returns **None — and None resolves to the personal
database**, so every read comes back empty while your own bearer-based
tests pass. Routes serving org-homed data must handle the
global-authority caller deliberately: resolve the resource's **owning
org** (try each org from `cross_org.list_org_slugs()`), aggregate
lists across orgs, and route writes into the owning org — never
personal. Verify every route twice: once with a session bearer, once
through the operator's browser cookie. The mission plugin shipped,
demoed green on bearer auth, and showed the operator an empty list —
this exact trap, found by the first real user.

## Researching before building

When the operator says "we designed this," search for the DESIGN
DECISION — the Signpost Index and MADR notes — before searching for
the artifact. Tonight's directory hunt burned an hour proving a
nonexistence that the design note stated in one line ("the member
directory is a consumer built against this"). `graph set find` can
only surface registered schemas; concepts that are designed but
unbuilt live in notes.

## Known pitfalls (all hit before, all avoidable)

- Hyphenated plugin dirs break entrypoint imports.
- `entrypoints.actions` is a bare module path (import side-effect
  registration), unlike `api`/`cli` which are `module:attr`.
- Jinja templates resolve through a custom multi-directory loader —
  don't pass a list to `Jinja2Templates(directory=...)`.
- A new default-enabled plugin breaks the dormant-baseline substrate
  test.
- Writing settings from a container writes a container-local copy —
  verify writes by reading back through the dashboard, from another
  process.
- Container CLIs can't read org settings locally (no org database in
  the container) — the substrate's plugin CLI mounting handles this
  with an HTTP fallback to `/api/plugins`; anything else your CLI
  reads must go through your plugin's authenticated routes, never
  local settings reads.
