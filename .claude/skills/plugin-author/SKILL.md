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

- Org scope: `organization_scope_from_request(request)` — the trusted
  value. Never read an org from a path, query, or body.
- Attribution: `principal_from_request(request).subject` — identity is
  stamped at the boundary, never taken from the payload.
- Serve screens as **complete documents with data baked in** (a pure
  settings→bytes compose function). Baking keeps rendering testable,
  spares N fetches, and leaves the door open for relay sealing later.
- Your own templates are **trusted**: same-origin fetch from them is
  fine. (Untrusted author-HTML surfaces are the opposite regime —
  platform-chrome mediation; know which one you are serving.)
- Domain refusals: raise a dedicated exception in the write layer, map
  it to 400 with the message; handler faults must never leak traces.
- Storage precedes delivery: persist first, then relay (CrossTalk via
  `tools.dashboard.crosstalk_delivery`) best-effort.

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
