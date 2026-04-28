# Action: Update Title & Summary

You are an agentic action operating on a graph note. Your job is to read the
note and update its title and short_description so they accurately reflect
the current content.

## Target

- **Asset:** {asset_id}
- **Title now:** {asset_title}
- **Short description now:** {asset_short_description}
- **URL:** {asset_url}

## Steps

1. `graph read {asset_id}` — load the full note content.
2. Decide whether the existing title and short_description are still
   accurate. If they are, write `experience_report.md` with a one-line
   "no change needed" and exit DONE.
3. If they need updating:
   - Pick a concise, descriptive title (≤ 80 chars).
   - Write a one-sentence short_description that captures the note's
     current focus.
4. Apply the changes via the graph CLI:
   - `graph note update {asset_id} --title "<new title>" --short-description "<one sentence>"`
   (use whichever flags the CLI exposes; if uncertain, fall back to the
   `/api/graph/source/{{id}}/title` and `.../short-description` write
   endpoints).
5. Verify the update by re-reading the note.

## Constraints

- Do NOT rewrite the body of the note. Only touch title + short_description.
- Keep the title information-dense; do not editorialize.
- If the note's content is empty, missing, or unreadable, write a BLOCKED
  decision and stop.

Dispatched by session: {dispatched_by_session}.
