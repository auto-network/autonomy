# Action: Update Title & Summary

You are an agentic action operating on a graph note. Read the note's
content and update its metadata columns — title, short_description,
keywords — so search and card previews reflect what the note actually
says.

## Target

- **Asset:** {asset_id}
- **Title now:** {asset_title}
- **Short description now:** {asset_short_description}
- **URL:** {asset_url}

## Steps

1. Read the note: `graph read {asset_id}`.

2. If the existing title and short_description are already accurate and
   no useful keywords are missing, write
   `/workspace/output/decision.json`:

   ```json
   {{
     "status": "DONE",
     "reason": "no change needed"
   }}
   ```

   Then exit.

3. Otherwise, compose the new metadata:

   - **title** — 5–12 words, ≤ 140 chars, plain text (no markdown),
     title case. Names what the note is, not what you think of it.
   - **short_description** — one or two sentences, ≤ 200 chars, prose.
   - **keywords** — comma-separated synonyms / aliases / related terms
     a searcher might use that aren't already in the title or
     description (3–10 entries).
   - **proposed_tags** (optional) — pick from the taxonomy below; omit
     if none of the existing tags apply.

4. Apply the metadata change in **one** command. Pass only the flags
   whose values are changing — omitted flags preserve the existing
   column value:

   ```
   graph note update {asset_id} \
     --title "<your new title>" \
     --short-description "<your one or two sentences>" \
     --keywords "<your,comma,separated,terms>"
   ```

   This updates the title / short_description / keywords columns
   directly. No body change. No version bump.

5. Verify: `graph read {asset_id}` — the new title and
   short_description should be visible.

6. Write `/workspace/output/decision.json`:

   ```json
   {{
     "status": "DONE",
     "reason": "<one sentence: what changed and why>"
   }}
   ```

   If something blocked the update (note body unreadable, command
   failed, etc.), write instead:

   ```json
   {{
     "status": "BLOCKED",
     "reason": "<one sentence: what blocked you>"
   }}
   ```

## Existing tag taxonomy

{tag_list}

Dispatched by session: {dispatched_by_session}.
