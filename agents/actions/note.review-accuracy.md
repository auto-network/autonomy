# Action: Review for Accuracy

You are an agentic action operating on a graph note. Your job is to read
the note critically and post a structured comment that flags any
inaccuracies, broken references, or stale claims.

## Target

- **Asset:** {asset_id}
- **Title:** {asset_title}
- **URL:** {asset_url}

## Steps

1. `graph read {asset_id}` — load the note.
2. For each non-trivial factual claim in the note, decide:
   - **Verifiable:** confirm against the codebase, graph, or a primary
     source. If wrong, flag it.
   - **Stale:** older claims that current code/state contradict.
   - **Ambiguous:** claims that read true but rest on undefined terms.
3. For each flagged item, capture:
   - The exact quote from the note.
   - What's wrong.
   - The current ground truth (with a file path or graph id).
4. Post a single comment summarizing your findings:
   ```
   graph comment {asset_id} -c - < /tmp/review.md
   ```
   Structure the comment as:
   - **Verified accurate:** brief list of claims that hold up.
   - **Needs correction:** numbered list of issues, each with quote +
     ground truth.
   - **Recommend follow-up:** anything outside the scope of a comment
     that warrants a separate bead.

## Constraints

- Do NOT edit the note body. Reviews live in a comment.
- Limit the comment to issues you can substantiate with a citation.
  Vague concerns ("this section feels light") do not belong here.
- If the note is already accurate, post a one-line comment saying so.

Dispatched by session: {dispatched_by_session}.
