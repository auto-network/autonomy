# Present — publishing decks from an agent session

Present (`/presentations`, `/present`) is the dashboard's deck library and viewer.
Publishing has **two independent stores**: the Design Studio design store (revisions +
variants, `agents/design_db.py`) and the Present **library** (a graph Settings set,
`dashboard.presentation.deck#1`). A design renders by direct URL without ever being in
the library. The library is what the operator browses.

Dashboard base URL: `https://localhost:8080` on host-network sessions,
`https://host.docker.internal:8080` from bridge-network containers (`curl -sk`).

## 1. Create a design

Direct API (handler: `api_design_create`, `tools/dashboard/server.py`):

```bash
curl -sk https://host.docker.internal:8080/api/design \
  -X POST -H 'Content-Type: application/json' \
  -d '{
    "title": "My Deck",
    "description": "Shown as the deck subtitle in the library",
    "variants": [{"id": "main", "html": "<full self-contained HTML>"}],
    "creator_session_id": "'$AUTONOMY_SESSION'"
  }'
# → 201 {"id": "<uuid>"}   — this id is BOTH the revision id and (for a new design) the stable design_id
```

Or the file watcher (`graph ui-design`, impl: `cmd_ui_design` in `tools/graph/cli.py`):

```bash
mkdir -p /tmp/deck && $EDITOR /tmp/deck/main.html   # one .html file per variant; filename stem = variant id
graph ui-design "My Deck" /tmp/deck --description "subtitle" \
  --api https://host.docker.internal:8080            # default --api is https://localhost:8080 — override on bridge networks
```

`graph ui-design` prints the revision id, design id, and URL, then **enters a foreground
watch loop** (1 s poll; every file save auto-POSTs a new revision; `screenshot.png` is
symlinked into the directory when the browser captures one; Ctrl-C to stop). From an
agent Bash tool, run it in the background or with a timeout — it does not exit on its own.
Append to an existing design with `--design <design_id>`.
**`graph ui-design` does NOT publish to the library** — step 3 is still required once.

## 2. Verify it renders

`/present/<design_id>` renders immediately after creation — library membership is not
required for direct-URL viewing. `GET /api/design/<id>` returns
`202 {"status":"pending","id":...}` — "pending" is a Design-Studio ranking state and is
**unrelated** to rendering or library visibility.

## 3. Publish to the library (the step everyone misses)

```bash
curl -sk -X POST https://host.docker.internal:8080/api/presentations/deck/<design_id>/shown
# → {"ok": true, "deck": {...}}
```

Handler: `mark_shown` in `tools/dashboard/plugins/presentations/entrypoints/api.py`.
It upserts one member into the graph Settings set `dashboard.presentation.deck`
(schema `PresentationDeckV1`, `entrypoints/schemas.py`), **keyed by the stable design_id**
— the `key` field you see on `GET /api/presentations/decks` rows is exactly that set-member
key. Idempotent: re-POSTing only refreshes `last_shown_at`. Required **once per deck**,
not once per revision.

## 4. Revise

```bash
curl -sk https://host.docker.internal:8080/api/design -X POST -H 'Content-Type: application/json' \
  -d '{"title": "...", "design_id": "<stable design_id>", "variants": [{"id":"main","html":"..."}], "creator_session_id": "'$AUTONOMY_SESSION'"}'
# → 201 {"id": "<new revision id>"}
```

`create_design` (`agents/design_db.py`) appends with `revision_seq = MAX+1`.
**You do NOT need to re-POST `/shown`.** The stored library record deliberately drops
`latest_revision_id` (`_deck_record_payload`, api.py); every library read re-hydrates
name/subtitle/slide count/latest revision from the design store
(`_hydrate_deck_record` → `_get_design_by_revision_or_design_id`), so the library always
shows the newest revision automatically and never duplicates the deck.
(Verified live 2026-07-14 against deck `3c7e290b-509e-488c-b04f-ed16569efbdf`.)

## 5. Remove / hide a deck

**There is no delete or hide route in the plugin API.** The full route surface
(`routes` list at the bottom of `entrypoints/api.py`) is:

- `GET  /api/presentations/decks` — the library
- `GET  /api/presentations/deck/{design_id}` — deck + full design + owner presence
- `POST /api/presentations/deck/{design_id}/shown` — publish/refresh library record

Removal is a Settings operation on the deck set (host-side graph write):

```bash
graph set members dashboard.presentation.deck --org autonomy       # list; KEY column = design_id
graph set remove  dashboard.presentation.deck <design_id> --org autonomy   # hard-delete the library record
# non-destructive alternative: graph set exclude dashboard.presentation.deck <design_id> --org autonomy
```

Note: `POST /api/design/{id}/dismiss` (`api_design_dismiss`, `tools/dashboard/server.py`)
only marks *pending Design-Studio revisions* dismissed — it does **not** remove a deck
from the Present library.

## Slide markers — multi-slide decks vs single-page apps

Two detectors, keep them consistent:

- **Server** (`_detect_slide_count`, `entrypoints/api.py`): counts every `<section>` /
  `<article>` start tag, every element with a `data-slide` attribute, and every element
  with class `slide` or `present-slide` (min 1) → `slide_count` / `slide_ids` metadata.
- **Viewer runtime** (`runtimeScript` in `page.js`): selects
  `[data-slide],[data-present-slide],.slide,.present-slide,section,article` anywhere
  under the scroll root (fallback: meaningful direct children of body; else the whole
  body is one slide). Each match becomes a `min-height:100svh` scroll-snap slide.

Therefore: for a **multi-slide deck**, use one top-level `<section>` per slide.
For a **single-page app/microsite**, avoid `<section>`/`<article>`/`.slide` markers
*anywhere* in the markup (the selector matches descendants, not just top-level children)
or the viewer will chop your page into snap-scrolled slides.

## Iframe environment + the init gotcha

The viewer does not load your HTML as a URL. `injectIframe` (`page.js`) `doc.write()`s a
**rebuilt document** (`iframeDocument`): your `<head>` content is extracted and re-injected,
your `<body>` content is wrapped in `<main id="present-scroll-root">`, and the viewer adds
Tailwind (browser CDN) + Alpine CDN, forces `html,body{overflow:hidden}`, dark background,
and scroll-snap CSS.

Consequences:
- One-shot top-level init can be discarded. Make init **idempotent and re-entrant**
  (guard on e.g. `document.body.dataset.appInit`), and invoke it at parse time, on
  `DOMContentLoaded`, on `load`, AND from a short retry loop (~20 × 500 ms).
- Ship a single self-contained HTML file — no external assets of your own.
- Arrow keys / space / scrubbing are handled by the viewer; your document receives
  `present:goto` postMessages and may expose `window.__presentGoToSlide`.
