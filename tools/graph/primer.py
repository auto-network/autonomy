"""Dynamic context primer generator.

Generates a tailored context primer for an agent about to work on a bead.
Queries the graph for everything relevant to the task and assembles
structured data that can be rendered for agents or the dashboard.

The graph is the source of truth. The primer is a disposable view.
If the graph changes, the next primer reflects it automatically.

Architecture:
    collect_primer_data() → dict      — pure data, no formatting
    format_for_agent(data) → str      — context markdown with follow-on commands
    format_for_dashboard(data) → dict — structured JSON for dashboard API
    generate_primer() → str           — backward-compat wrapper (collect + agent format)
"""

from __future__ import annotations
import json
import subprocess

from .db import GraphDB, DEFAULT_DB, _sanitize_fts_query


def _run_bd(args: list[str], timeout: int = 15) -> str:
    """Run a bd CLI command and return stdout."""
    try:
        result = subprocess.run(
            ["bd"] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _get_bead(bead_id: str) -> dict | None:
    """Return bead details including comments via dashboard DAO.

    Bypasses the `bd` CLI because `bd show --json` omits comments and
    `bd comments --json` has a UUID/int64 scan bug.
    """
    from tools.dashboard.dao import beads as dao_beads
    try:
        return dao_beads.get_bead(bead_id)
    except Exception:
        return None


# ── Data Collection ──────────────────────────────────────────────


def collect_primer_data(
    bead_id: str,
    db: GraphDB | None = None,
    include_pitfalls: bool = True,
    include_provenance: bool = True,
    include_related_beads: bool = True,
    max_context_chars: int = 15000,
) -> dict:
    """Collect all primer data for a bead as a structured dict.

    Returns pure data — no formatting, no instructions, no tool docs.
    Callers choose a formatter (format_for_agent, format_for_dashboard).
    """
    close_db = False
    if db is None:
        db = GraphDB(DEFAULT_DB)
        close_db = True

    result = {"bead_id": bead_id, "bead": None, "provenance": [],
              "related_notes": [], "pitfalls": [], "related_beads": []}

    # ── 1. Bead Details ──────────────────────────────────────
    bead = _get_bead(bead_id)
    if bead:
        result["bead"] = {
            "title": bead.get("title", bead_id),
            "priority": bead.get("priority", "?"),
            "status": bead.get("status", "?"),
            "description": bead.get("description", ""),
            "acceptance_criteria": bead.get("acceptance_criteria", ""),
            "design": bead.get("design", ""),
            "comments": bead.get("comments", []),
            "notes": bead.get("notes", ""),
        }

    # ── 1b. Merge retry context (from bead notes) ──────────
    result["merge_retry"] = None
    if bead and bead.get("notes", ""):
        notes_text = bead["notes"]
        marker = "MERGE_RETRY_CONTEXT"
        idx = notes_text.rfind(marker)  # use last occurrence (most recent retry)
        if idx >= 0:
            block = notes_text[idx + len(marker):]
            # Block ends at next note boundary or end of string
            retry = {}
            for line in block.splitlines():
                line = line.strip()
                if line.startswith("branch:"):
                    retry["branch"] = line[len("branch:"):].strip()
                elif line.startswith("commit:"):
                    retry["commit"] = line[len("commit:"):].strip()
                elif line.startswith("merge_error:"):
                    retry["merge_error"] = line[len("merge_error:"):].strip()
            if retry.get("branch") and retry.get("commit"):
                result["merge_retry"] = retry

    # ── 2. Provenance — original conversation turns ──────────
    if include_provenance:
        provenance_edges = db.conn.execute(
            """SELECT target_id, relation, metadata FROM edges
               WHERE source_id = ? AND source_type = 'bead'
               AND relation IN ('conceived_at', 'informed_by', 'discussed_at', 'refined_by')
               ORDER BY created_at""",
            (bead_id,),
        ).fetchall()

        chars_used = 0
        for edge in provenance_edges:
            meta = json.loads(edge["metadata"]) if edge["metadata"] else {}
            turns_range = meta.get("turns", {})
            note = meta.get("note", "")
            source_id = edge["target_id"]
            relation = edge["relation"]

            prov_entry = {
                "source_id": source_id,
                "relation": relation,
                "note": note,
                "turns": [],
            }

            if turns_range:
                from_turn = turns_range.get("from", 0)
                to_turn = turns_range.get("to", from_turn)
                entries = db.get_source_content(source_id)
                relevant = [e for e in entries
                            if e.get("turn_number") and from_turn <= e["turn_number"] <= to_turn]
                for entry in relevant:
                    role = "USER" if entry.get("entry_type") == "thought" else "ASSISTANT"
                    content = entry["content"]
                    # Never truncate USER messages — they are the primary spec.
                    # Only apply budget and character limits to ASSISTANT turns.
                    if role != "USER":
                        if chars_used + len(content) > max_context_chars // 3:
                            content = content[:500] + "\n... [truncated]"
                        content = content[:1000]
                    prov_entry["turns"].append({
                        "turn_number": entry["turn_number"],
                        "role": role,
                        "content": content,
                    })
                    chars_used += len(content)

            result["provenance"].append(prov_entry)

    # Collect provenance source IDs for dedup
    provenance_source_ids = {p["source_id"] for p in result["provenance"]}

    # Extract bead labels once for tag boosting
    bead_labels = set(bead.get("labels", [])) if bead else set()

    def _tag_score(note):
        meta = json.loads(note["metadata"]) if note["metadata"] else {}
        note_tags = set(meta.get("tags", []))
        return len(bead_labels & note_tags)

    # ── 3. Related notes ─────────────────────────────────────
    if bead and bead.get("title"):
        title_words = [w for w in bead["title"].split() if len(w) > 3]
        if title_words:
            query_terms = " ".join(title_words[:5])
            fts_q = _sanitize_fts_query(query_terms, or_mode=True)
            try:
                notes = db.conn.execute(
                    """SELECT s.id, t.content, s.metadata FROM sources s
                       JOIN thoughts t ON t.source_id = s.id
                       JOIN thoughts_fts fts ON fts.rowid = t.rowid
                       WHERE s.type = 'note'
                       AND s.metadata NOT LIKE '%pitfall%'
                       AND thoughts_fts MATCH ?
                       ORDER BY fts.rank LIMIT 5""",
                    (fts_q,),
                ).fetchall()
            except Exception:
                notes = []

            if not notes and len(title_words) > 1:
                fts_q = _sanitize_fts_query(" ".join(title_words[:3]), or_mode=True)
                try:
                    notes = db.conn.execute(
                        """SELECT s.id, t.content, s.metadata FROM sources s
                           JOIN thoughts t ON t.source_id = s.id
                           JOIN thoughts_fts fts ON fts.rowid = t.rowid
                           WHERE s.type = 'note'
                           AND s.metadata NOT LIKE '%pitfall%'
                           AND thoughts_fts MATCH ?
                           ORDER BY fts.rank LIMIT 5""",
                        (fts_q,),
                    ).fetchall()
                except Exception:
                    notes = []

            # Boost notes with tag overlap
            if bead_labels and notes:
                notes = sorted(notes, key=_tag_score, reverse=True)

            for note in (notes or []):
                if note["id"] in provenance_source_ids:
                    continue
                meta = json.loads(note["metadata"]) if note["metadata"] else {}
                result["related_notes"].append({
                    "source_id": note["id"],
                    "content": note["content"],
                    "tags": meta.get("tags", []),
                })

    # ── 4. Pitfalls (scoped to bead topics) ─────────────────
    if include_pitfalls and bead and bead.get("title"):
        title_words = [w for w in bead["title"].split() if len(w) > 3]
        if title_words:
            pit_q = _sanitize_fts_query(" ".join(title_words[:5]), or_mode=True)
            try:
                pitfalls = db.conn.execute(
                    """SELECT s.id, t.content, s.metadata FROM sources s
                       JOIN thoughts t ON t.source_id = s.id
                       JOIN thoughts_fts fts ON fts.rowid = t.rowid
                       WHERE s.type = 'note'
                       AND s.metadata LIKE '%pitfall%'
                       AND thoughts_fts MATCH ?
                       ORDER BY fts.rank LIMIT 5""",
                    (pit_q,),
                ).fetchall()
            except Exception:
                pitfalls = []

            if bead_labels and pitfalls:
                pitfalls = sorted(pitfalls, key=_tag_score, reverse=True)

            for p in (pitfalls or []):
                meta = json.loads(p["metadata"]) if p["metadata"] else {}
                result["pitfalls"].append({
                    "source_id": p["id"],
                    "content": p["content"],
                    "tags": meta.get("tags", []),
                })

    # ── 5. Semantically related beads (via bd find-duplicates) ──
    if include_related_beads:
        dup_out = _run_bd(
            ["find-duplicates", "--json", "--threshold", "0.3", "--limit", "50"],
            timeout=30,
        )
        if dup_out:
            try:
                dup_data = json.loads(dup_out)
                pairs = dup_data.get("pairs", [])
                for pair in pairs:
                    other_id = None
                    if pair.get("issue_a_id") == bead_id:
                        other_id = pair.get("issue_b_id")
                        other_title = pair.get("issue_b_title", "?")
                    elif pair.get("issue_b_id") == bead_id:
                        other_id = pair.get("issue_a_id")
                        other_title = pair.get("issue_a_title", "?")
                    if other_id:
                        other_bead = _get_bead(other_id)
                        result["related_beads"].append({
                            "bead_id": other_id,
                            "title": other_title,
                            "priority": other_bead.get("priority", "?") if other_bead else "?",
                            "status": other_bead.get("status", "?") if other_bead else "?",
                            "similarity": round(pair.get("similarity", 0), 3),
                        })
                # Sort by similarity descending, keep top 8
                result["related_beads"].sort(
                    key=lambda x: x.get("similarity", 0), reverse=True,
                )
                result["related_beads"] = result["related_beads"][:8]
            except (json.JSONDecodeError, KeyError):
                pass

    if close_db:
        db.close()

    return result


# ── Formatters ───────────────────────────────────────────────


def format_for_agent(data: dict) -> str:
    """Render primer data as agent-friendly markdown.

    Pure context — no tool instructions (those live in tool_guidelines.md).
    Includes follow-on graph commands so the agent can dig deeper.
    """
    bead_id = data["bead_id"]
    bead = data.get("bead")
    sections = []

    # ── Header ───────────────────────────────────────────────
    if bead:
        sections.append(f"# Task: {bead['title']}")
        sections.append(f"**Bead:** {bead_id}  **Priority:** P{bead['priority']}  **Status:** {bead['status']}")
        # ── Merge retry banner (before description) ──────────
        merge_retry = data.get("merge_retry")
        if merge_retry:
            branch = merge_retry.get("branch", "?")
            commit = merge_retry.get("commit", "?")
            merge_error = merge_retry.get("merge_error", "(no details)")
            sections.append(
                f"\n## MERGE RETRY — Previous Work Available\n"
                f"\nThis bead was previously completed but the merge to master failed due to conflicts.\n"
                f"\n**Previous branch:** {branch}"
                f"\n**Previous commit:** {commit}"
                f"\n**Merge error:**\n```\n{merge_error}\n```\n"
                f"\n### Recovery Strategy\n"
                f"\n1. `git cherry-pick {commit}` — apply previous work onto current master"
                f"\n2. Resolve any conflicts (usually trivial — adjacent edits, import lines)"
                f"\n3. Verify the result compiles/works"
                f"\n4. Commit and write decision.json as normal\n"
                f"\nDo NOT re-implement from scratch. The previous work is complete and correct"
                f" — it just needs conflict resolution."
            )
        if bead["description"]:
            sections.append(f"\n## Description\n{bead['description']}")
        if bead["acceptance_criteria"]:
            sections.append(f"\n## Acceptance Criteria\n{bead['acceptance_criteria']}")
        if bead["design"]:
            sections.append(f"\n## Design Notes\n{bead['design']}")
        comments = bead.get("comments", [])
        if comments:
            sections.append("\n## Comments")
            for c in comments:
                author = c.get("author", "?") if isinstance(c, dict) else "?"
                ts = c.get("created_at", "") if isinstance(c, dict) else ""
                text = c.get("text", "") if isinstance(c, dict) else str(c)
                sections.append(f"\n**{author}** ({ts}):\n{text}")
    else:
        sections.append(f"# Task: {bead_id}")
        sections.append("(Could not fetch bead details from bd)")

    # ── Provenance ───────────────────────────────────────────
    if data["provenance"]:
        sections.append("\n## Background — Original Discussions")
        for prov in data["provenance"]:
            if prov["note"]:
                sections.append(f"\n**{prov['relation']}:** {prov['note']}")
            for turn in prov["turns"]:
                sections.append(f"\n> **Turn {turn['turn_number']} — {turn['role']}:**\n> {turn['content']}")
            # Follow-on command for agent to read more context
            if prov["turns"]:
                first_turn = prov["turns"][0]["turn_number"]
                sections.append(f"\n_Read more:_ `graph context {prov['source_id']} {first_turn} --window 5`")

    # ── Related notes ────────────────────────────────────────
    if data["related_notes"]:
        sections.append("\n## Related Notes")
        for note in data["related_notes"]:
            content = note["content"]
            if len(content) > 300:
                content = content[:300] + "..."
            sections.append(f"- {content}")
            sections.append(f"  _Full text:_ `graph read {note['source_id']}`")

    # ── Pitfalls ─────────────────────────────────────────────
    if data["pitfalls"]:
        sections.append("\n## Known Pitfalls")
        for p in data["pitfalls"]:
            content = p["content"]
            if len(content) > 200:
                content = content[:200] + "..."
            sections.append(f"- {content}")

    # ── Related beads ────────────────────────────────────────
    if data["related_beads"]:
        sections.append("\n## Similar Beads")
        for rb in data["related_beads"]:
            icon = "done" if rb["status"] == "closed" else "open"
            sim = f" ({rb['similarity']:.0%})" if rb.get("similarity") else ""
            bid = rb.get("bead_id", rb.get("id", "?"))
            sections.append(f"- [{icon}] {bid}: {rb['title']} (P{rb['priority']}){sim}")
            sections.append(f"  _Details:_ `bd show {bid}`")

    return "\n".join(sections)


def format_for_dashboard(data: dict) -> dict:
    """Return structured data for the dashboard /api/primer/{id} endpoint.

    Returns a dict (serialized as JSON) with each section carrying full IDs
    so the frontend can render clickable links to /source/{src_id} and
    /bead/{bead_id}. No agent instructions, no CLI commands.
    """
    result = {
        "bead_id": data["bead_id"],
        "bead": data.get("bead"),
        "provenance": [],
        "related_notes": [],
        "pitfalls": [],
        "related_beads": [],
    }

    # ── Provenance — with content previews ────────────────────
    for prov in data.get("provenance", []):
        entry = {
            "source_id": prov["source_id"],
            "relation": prov["relation"],
            "note": prov.get("note", ""),
            "turns": [],
        }
        for turn in prov.get("turns", []):
            entry["turns"].append({
                "turn_number": turn["turn_number"],
                "role": turn["role"],
                "content_preview": turn["content"][:500] + ("..." if len(turn["content"]) > 500 else ""),
            })
        result["provenance"].append(entry)

    # ── Related notes — with content previews ─────────────────
    for note in data.get("related_notes", []):
        content = note["content"]
        result["related_notes"].append({
            "source_id": note["source_id"],
            "content_preview": content[:500] + ("..." if len(content) > 500 else ""),
            "tags": note.get("tags", []),
        })

    # ── Pitfalls — with content previews ──────────────────────
    for p in data.get("pitfalls", []):
        content = p["content"]
        result["pitfalls"].append({
            "source_id": p["source_id"],
            "content_preview": content[:300] + ("..." if len(content) > 300 else ""),
            "tags": p.get("tags", []),
        })

    # ── Related beads — with similarity scores ────────────────
    for rb in data.get("related_beads", []):
        result["related_beads"].append({
            "bead_id": rb.get("bead_id", rb.get("id", "?")),
            "title": rb.get("title", "?"),
            "priority": rb.get("priority", "?"),
            "status": rb.get("status", "?"),
            "similarity": rb.get("similarity"),
        })

    return result


# ── Backward-compatible wrappers ─────────────────────────────


def generate_primer(
    bead_id: str,
    db: GraphDB | None = None,
    include_tools: bool = True,
    include_pitfalls: bool = True,
    include_provenance: bool = True,
    include_related_beads: bool = True,
    max_context_chars: int = 15000,
) -> str:
    """Generate a context primer as markdown (backward-compatible).

    Calls collect_primer_data() + format_for_agent().
    The include_tools parameter is accepted but ignored — tool instructions
    now live exclusively in agents/shared/tool_guidelines.md.
    """
    data = collect_primer_data(
        bead_id, db=db,
        include_pitfalls=include_pitfalls,
        include_provenance=include_provenance,
        include_related_beads=include_related_beads,
        max_context_chars=max_context_chars,
    )
    return format_for_agent(data)


def primer_for_bead(bead_id: str) -> str:
    """Convenience function — generate primer and return as string."""
    return generate_primer(bead_id)
