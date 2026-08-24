# Mission Control (legacy `mission_control` plugin) — SUPERSEDED

**This app has been superseded by the `mission` plugin.** All structured
missions and the Multi-User Autonomy record migrated there and are marked
complete here. For everything mission-related — creating missions and
pillars, writing status/checkpoints/decisions/questions, chat, bead
linkage — read the current skill instead:

```
GET /api/plugins/mission/skill        # the authoritative manual
```

Its screens live at `/mission` (homepage) and `/mission/<mission_uuid>`;
its CLI is `graph mission <verb>`; its data is the `mission.*` settings
sets. Do not create new missions in this legacy app.

## What this plugin still owns

Only pre-cutover **freeform** missions that have not migrated (revisioned
HTML sites with visitor links). If you coordinate one, the minimal
surface is:

- `POST /api/missions/<id>/site` / `POST /api/pillars/<id>/site` with
  `{"html": "<complete document>", "note": "one-line change"}` — push a
  revision (publishes immediately).
- `GET /api/missions/<id>/questions` and
  `POST .../questions/<entry_id>/answer` with `{"answer": "..."}` —
  answer visitor questions. Poll rather than relying on CrossTalk relay.
- Send the bearer header on every call:
  `-H "Authorization: Bearer $CROSSTALK_TOKEN"`.
- Visitor links are minted by operator approval (kind `visitor_token`),
  never by a session.

Everything else this document used to describe (the full route surface,
the screen contract, presence, decision logs) is retired with the
migrated missions. Consult git history for the old text if a freeform
mission genuinely needs it.
