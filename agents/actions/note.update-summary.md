# Action: Update Title & Summary

You are an agentic action operating on a graph note. Your job is to read the
note and produce updated metadata (title, short_description, keywords, and
proposed tags) so search and card previews accurately reflect the content.

## Target

- **Asset:** {asset_id}
- **Title now:** {asset_title}
- **Short description now:** {asset_short_description}
- **URL:** {asset_url}

## Steps

1. `graph read {asset_id}` — load the full note content.
2. Decide whether the existing metadata is still accurate. If title +
   short_description are correct AND no useful keywords are missing, write
   `experience_report.md` with a one-line "no change needed" and exit DONE.
3. Otherwise, produce a JSON object with all four fields:

   ```json
   {{
     "title": "<concise title, 4–10 words, no leading # marker>",
     "short_description": "<one or two sentences, ≤ 200 chars>",
     "keywords": ["<synonym>", "<alias>", "<related-term>", ...],
     "proposed_tags": ["<existing-tag-from-taxonomy>", ...]
   }}
   ```

   Rules:
   - **title** — human-readable, no markdown, ≤ 80 chars. Capitalize
     appropriately. Do not editorialize.
   - **short_description** — prose, complete sentences, NO list bullets.
   - **keywords** — 3–10 alternative search terms (synonyms, abbreviations,
     common typos, related concepts that don't appear in the title or
     description). DO NOT repeat words already in the title.
   - **proposed_tags** — ONLY tags drawn from the existing taxonomy
     provided below. Never invent new tag names. Empty list if no existing
     tag applies.

4. Apply the changes via the graph CLI:

   ```
   graph note update {asset_id} \
     --short-description "<one or two sentences>" \
     --keywords "<comma,separated,terms>"
   ```

   The title is derived automatically from a leading `# heading` line in
   the note body, so emit a `# <new title>` line at the top of the
   updated content if the existing one needs to change. (If the CLI
   surface lacks one of these flags in your container, fall back to the
   `/api/graph/note/update` JSON endpoint with the matching field name.)

5. Verify the update by re-reading the note.

## Existing tag taxonomy

{tag_list}

## Constraints

- Do NOT rewrite the body of the note. Only touch title +
  short_description + keywords (+ optional proposed_tags metadata).
- Keep the title information-dense; do not editorialize.
- If the note's content is empty, missing, or unreadable, write a BLOCKED
  decision and stop.

Dispatched by session: {dispatched_by_session}.
