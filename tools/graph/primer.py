"""Dynamic context primer generator.

Generates a tailored context primer for an agent about to work on a bead.
Queries the graph for everything relevant to the task and assembles
structured data that can be rendered for agents or the dashboard.

The graph is the source of truth. The primer is a disposable view.
If the graph changes, the next primer reflects it automatically.

Architecture:
    collect_primer_data() → dict    — pure data, no formatting
    format_for_agent(data) → str    — context markdown with follow-on commands
    format_for_dashboard(data) → str — human-friendly markdown (no agent noise)
    generate_primer() → str          — backward-compat wrapper (collect + agent format)
"""

from __future__ import annotations
import json
import subprocess

from .db import GraphDB, DEFAULT_DB


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
    """Get bead details from bd show --json."""
    out = _run_bd(["show", bead_id, "--json"])
    if not out:
        return None
    try:
        data = json.loads(out)
        if isinstance(data, list) and data:
            return data[0]
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
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
        }

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
                    if chars_used + len(content) > max_context_chars // 3:
                        content = content[:500] + "\n... [truncated]"
                    prov_entry["turns"].append({
                        "turn_number": entry["turn_number"],
                        "role": role,
                        "content": content[:1000],
                    })
                    chars_used += len(content)

            result["provenance"].append(prov_entry)

    # ── 3. Related notes ─────────────────────────────────────
    if bead and bead.get("title"):
        title_words = [w for w in bead["title"].split() if len(w) > 3]
        if title_words:
            query_terms = " ".join(title_words[:5])
            from .db import _sanitize_fts_query
            fts_q = _sanitize_fts_query(query_terms, or_mode=True)
            try:
                notes = db.conn.execute(
                    """SELECT s.id, t.content, s.metadata FROM sources s
                       JOIN thoughts t ON t.source_id = s.id
                       JOIN thoughts_fts fts ON fts.rowid = t.rowid
                       WHERE s.type = 'note'
                       AND thoughts_fts MATCH ?
                       ORDER BY s.created_at DESC LIMIT 5""",
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
                           AND thoughts_fts MATCH ?
                           ORDER BY s.created_at DESC LIMIT 5""",
                        (fts_q,),
                    ).fetchall()
                except Exception:
                    notes = []

            for note in (notes or []):
                meta = json.loads(note["metadata"]) if note["metadata"] else {}
                result["related_notes"].append({
                    "source_id": note["id"],
                    "content": note["content"],
                    "tags": meta.get("tags", []),
                })

    # ── 4. Pitfalls ──────────────────────────────────────────
    if include_pitfalls:
        try:
            pitfalls = db.conn.execute(
                """SELECT s.id, t.content FROM sources s
                   JOIN thoughts t ON t.source_id = s.id
                   WHERE s.type = 'note'
                   AND s.metadata LIKE '%pitfall%'
                   ORDER BY s.created_at DESC LIMIT 10""",
            ).fetchall()

            for p in (pitfalls or []):
                result["pitfalls"].append({
                    "source_id": p["id"],
                    "content": p["content"],
                })
        except Exception:
            pass

    # ── 5. Related beads in same epic ────────────────────────
    if include_related_beads:
        parent_out = _run_bd(["dep", "list", bead_id, "--json"])
        if parent_out:
            try:
                deps = json.loads(parent_out)
                for dep in deps:
                    if dep.get("type") == "parent-child" and dep.get("direction") == "parent":
                        sibling_out = _run_bd(["dep", "list", dep["target"], "--json"])
                        if sibling_out:
                            sibling_deps = json.loads(sibling_out)
                            siblings = [s for s in sibling_deps
                                        if s.get("type") == "parent-child"
                                        and s.get("direction") == "child"
                                        and s.get("source") != bead_id]
                            for s in siblings[:8]:
                                sib_bead = _get_bead(s["source"])
                                if sib_bead:
                                    result["related_beads"].append({
                                        "id": s["source"],
                                        "title": sib_bead.get("title", "?"),
                                        "priority": sib_bead.get("priority", "?"),
                                        "status": sib_bead.get("status", "?"),
                                    })
                        break
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
        if bead["description"]:
            sections.append(f"\n## Description\n{bead['description']}")
        if bead["acceptance_criteria"]:
            sections.append(f"\n## Acceptance Criteria\n{bead['acceptance_criteria']}")
        if bead["design"]:
            sections.append(f"\n## Design Notes\n{bead['design']}")
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
        sections.append("\n## Related Beads (Same Epic)")
        for rb in data["related_beads"]:
            icon = "done" if rb["status"] == "closed" else "open"
            sections.append(f"- [{icon}] {rb['id']}: {rb['title']} (P{rb['priority']})")
        sections.append(f"\n_View details:_ `bd show <id>`")

    return "\n".join(sections)


def format_for_dashboard(data: dict) -> str:
    """Render primer data as human-friendly markdown for the dashboard.

    No agent instructions, no CLI commands. Uses readable formatting
    that the dashboard can render as HTML.
    """
    bead_id = data["bead_id"]
    bead = data.get("bead")
    sections = []

    # ── Header ───────────────────────────────────────────────
    if bead:
        sections.append(f"# {bead['title']}")
        sections.append(f"**Priority:** P{bead['priority']}  **Status:** {bead['status']}")
        if bead["description"]:
            sections.append(f"\n## Description\n{bead['description']}")
        if bead["acceptance_criteria"]:
            sections.append(f"\n## Acceptance Criteria\n{bead['acceptance_criteria']}")
        if bead["design"]:
            sections.append(f"\n## Design Notes\n{bead['design']}")
    else:
        sections.append(f"# {bead_id}")
        sections.append("(Could not fetch bead details)")

    # ── Provenance ───────────────────────────────────────────
    if data["provenance"]:
        sections.append("\n## Background — Original Discussions")
        for prov in data["provenance"]:
            if prov["note"]:
                sections.append(f"\n**{prov['relation']}:** {prov['note']}")
            for turn in prov["turns"]:
                sections.append(f"\n> **Turn {turn['turn_number']} — {turn['role']}:**\n> {turn['content']}")

    # ── Related notes ────────────────────────────────────────
    if data["related_notes"]:
        sections.append("\n## Related Notes")
        for note in data["related_notes"]:
            content = note["content"]
            if len(content) > 500:
                content = content[:500] + "..."
            sections.append(f"- {content}")

    # ── Pitfalls ─────────────────────────────────────────────
    if data["pitfalls"]:
        sections.append("\n## Known Pitfalls")
        for p in data["pitfalls"]:
            content = p["content"]
            if len(content) > 300:
                content = content[:300] + "..."
            sections.append(f"- {content}")

    # ── Related beads ────────────────────────────────────────
    if data["related_beads"]:
        sections.append("\n## Related Beads")
        for rb in data["related_beads"]:
            icon = "done" if rb["status"] == "closed" else "open"
            sections.append(f"- [{icon}] {rb['id']}: {rb['title']} (P{rb['priority']})")

    return "\n".join(sections)


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
