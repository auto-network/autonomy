---
name: capture
description: Capture a pivotal moment — refresh graph, find the turn, create or update beads with provenance, drop a trail marker note
user_invocable: true
---

The user has identified something important in the conversation that needs to be captured in the knowledge graph with full provenance. This could be a new insight, a design principle, a correction, a pitfall discovery, or a feature idea.

Follow these steps IN ORDER:

1. **Refresh the graph** to capture the latest turns:
   ```bash
   graph sessions --all
   ```

2. **Find the pivotal turn(s)** — search for the key phrase the user said:
   ```bash
   graph search "key phrase from the user" --or --limit 3
   ```
   Note the source ID and turn number(s).

3. **Decide: new bead or update existing?**
   - If this is a new feature/task/insight: create a bead with provenance
   - If this refines an existing bead: update the bead and add a link

4. **For a new bead** — use `graph bead` to create with provenance in one step:
   ```bash
   graph bead "Title describing the insight" -p <priority> \
     -d "Full description with context" \
     --source <source_id> --turns <turn_number_or_range> \
     --note "What the user said and why it matters"
   ```

5. **For an existing bead** — update description and add provenance link:
   ```bash
   bd update <bead_id> -d "Updated description reflecting new understanding"
   graph link <bead_id> <source_id> -r <relation> -t <turns> -n "context"
   ```
   Relations: `conceived_at`, `informed_by`, `discussed_at`, `refined_by`

6. **Drop a trail marker note** with the principle/insight for searchability:
   ```bash
   graph note "PRINCIPLE/PITFALL/DISCOVERY: description" \
     --project autonomy --tags <relevant,tags>
   ```

7. **If this changes how other beads should work**, update those beads too and link them to the same turns.

8. **Report what you captured** — show the user the bead ID, source turn, and links created.

Always include the user's exact words in the provenance note — their phrasing IS the source of truth.
