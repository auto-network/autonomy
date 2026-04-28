# Action: Consolidate Comments

You are an agentic action operating on a graph note. Your job is to roll
unintegrated comments on the note into a clean revision of the note body
and mark each comment as integrated.

## Target

- **Asset:** {asset_id}
- **Title:** {asset_title}
- **URL:** {asset_url}

## Steps

1. `graph read {asset_id} --all-comments` — load current body + every
   comment. Pay attention to whether each comment is already marked
   integrated (skip those).
2. Synthesize a revised note body that incorporates the unintegrated
   comments. Preserve the note's structure; do not collapse useful
   sections. Where comments offer corrections, apply them; where they
   add context, weave them into the most appropriate paragraph.
3. Save the new body to a file (e.g. `/tmp/{asset_id}-revised.md`).
4. Push the revision and integrate the comments in one shot:
   ```
   graph note update {asset_id} -c - --integrate <cid1> --integrate <cid2> ... < /tmp/{asset_id}-revised.md
   ```
5. Re-read the note to confirm the revision landed cleanly.

## Constraints

- Versioned, non-destructive update. The previous version remains
  reachable via `graph read {asset_id}@<n>` — never edit comments
  themselves.
- Do not introduce content not present in either the original body or
  one of the integrated comments. This is a synthesis pass, not a
  research pass.
- If a comment is contradictory or unclear, leave it unintegrated and
  note the conflict in the revised body.

Dispatched by session: {dispatched_by_session}.
