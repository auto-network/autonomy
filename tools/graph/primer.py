"""Dynamic context primer generator.

Generates a tailored context primer for an agent about to work on a bead.
Queries the graph for everything relevant to the task and assembles a
markdown document the agent receives as its prompt context.

The graph is the source of truth. The primer is a disposable view.
If the graph changes, the next primer reflects it automatically.
"""

from __future__ import annotations
import json
import subprocess
from pathlib import Path

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


def generate_primer(
    bead_id: str,
    db: GraphDB | None = None,
    include_tools: bool = True,
    include_pitfalls: bool = True,
    include_provenance: bool = True,
    include_related_beads: bool = True,
    max_context_chars: int = 15000,
) -> str:
    """Generate a context primer for an agent about to work on a bead.

    Pulls from:
    1. Bead description + acceptance criteria (bd show)
    2. Conceived_at provenance turns (the original conversation)
    3. Related graph notes tagged with relevant topics
    4. Pitfall notes for tools the agent will use
    5. Recent related beads in the same epic
    6. Tool documentation from graph
    """
    close_db = False
    if db is None:
        db = GraphDB(DEFAULT_DB)
        close_db = True

    sections = []

    # ── 1. Bead Details ──────────────────────────────────────
    bead = _get_bead(bead_id)
    if bead:
        sections.append(f"# Task: {bead.get('title', bead_id)}")
        sections.append(f"**Bead:** {bead_id}  **Priority:** P{bead.get('priority', '?')}  **Status:** {bead.get('status', '?')}")
        if bead.get("description"):
            sections.append(f"\n## Description\n{bead['description']}")
        if bead.get("acceptance_criteria"):
            sections.append(f"\n## Acceptance Criteria\n{bead['acceptance_criteria']}")
        if bead.get("design"):
            sections.append(f"\n## Design Notes\n{bead['design']}")
    else:
        sections.append(f"# Task: {bead_id}")
        sections.append("(Could not fetch bead details from bd)")

    # ── 2. Provenance — original conversation turns ──────────
    if include_provenance:
        provenance_edges = db.conn.execute(
            """SELECT target_id, relation, metadata FROM edges
               WHERE source_id = ? AND source_type = 'bead'
               AND relation IN ('conceived_at', 'informed_by', 'discussed_at', 'refined_by')
               ORDER BY created_at""",
            (bead_id,),
        ).fetchall()

        if provenance_edges:
            sections.append("\n## Background — Original Discussions")
            chars_used = 0
            for edge in provenance_edges:
                meta = json.loads(edge["metadata"]) if edge["metadata"] else {}
                turns = meta.get("turns", {})
                note = meta.get("note", "")
                source_id = edge["target_id"]
                relation = edge["relation"]

                if note:
                    sections.append(f"\n**{relation}:** {note}")

                if turns:
                    from_turn = turns.get("from", 0)
                    to_turn = turns.get("to", from_turn)
                    entries = db.get_source_content(source_id)
                    relevant = [e for e in entries
                                if e.get("turn_number") and from_turn <= e["turn_number"] <= to_turn]
                    for entry in relevant:
                        role = "USER" if entry.get("entry_type") == "thought" else "ASSISTANT"
                        content = entry["content"]
                        if chars_used + len(content) > max_context_chars // 3:
                            content = content[:500] + "\n... [truncated]"
                        sections.append(f"\n> **Turn {entry['turn_number']} — {role}:**\n> {content[:1000]}")
                        chars_used += len(content)

    # ── 3. Related notes ─────────────────────────────────────
    if bead and bead.get("title"):
        # Extract key terms from title for searching
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

            # Fallback: try OR-style with individual terms
            if not notes and len(title_words) > 1:
                from .db import _sanitize_fts_query
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

            if notes:
                sections.append("\n## Related Notes")
                for note in notes:
                    content = note["content"]
                    if len(content) > 300:
                        content = content[:300] + "…"
                    sections.append(f"- {content}")

    # ── 4. Pitfalls ──────────────────────────────────────────
    if include_pitfalls:
        try:
            pitfalls = db.conn.execute(
                """SELECT t.content FROM sources s
                   JOIN thoughts t ON t.source_id = s.id
                   WHERE s.type = 'note'
                   AND s.metadata LIKE '%pitfall%'
                   ORDER BY s.created_at DESC LIMIT 10""",
            ).fetchall()

            if pitfalls:
                sections.append("\n## Known Pitfalls")
                for p in pitfalls:
                    content = p["content"]
                    if len(content) > 200:
                        content = content[:200] + "…"
                    sections.append(f"- {content}")
        except Exception:
            pass

    # ── 5. Related beads in same epic ────────────────────────
    if include_related_beads:
        # Get parent epic
        parent_out = _run_bd(["dep", "list", bead_id, "--json"])
        if parent_out:
            try:
                deps = json.loads(parent_out)
                siblings_shown = 0
                for dep in deps:
                    if dep.get("type") == "parent-child" and dep.get("direction") == "parent":
                        # Get siblings
                        sibling_out = _run_bd(["dep", "list", dep["target"], "--json"])
                        if sibling_out:
                            sibling_deps = json.loads(sibling_out)
                            siblings = [s for s in sibling_deps
                                        if s.get("type") == "parent-child"
                                        and s.get("direction") == "child"
                                        and s.get("source") != bead_id]
                            if siblings:
                                sections.append("\n## Related Beads (Same Epic)")
                                for s in siblings[:8]:
                                    sib_bead = _get_bead(s["source"])
                                    if sib_bead:
                                        status = "✓" if sib_bead.get("status") == "closed" else "○"
                                        sections.append(
                                            f"- {status} {s['source']}: {sib_bead.get('title', '?')} (P{sib_bead.get('priority', '?')})"
                                        )
                                        siblings_shown += 1
                        break
            except (json.JSONDecodeError, KeyError):
                pass

    # ── 6. Tool docs ─────────────────────────────────────────
    if include_tools:
        sections.append("\n## Available Tools")
        sections.append("""
### Knowledge Graph (`graph`)
- `graph search "query"` — full-text search (use `--or` for OR mode)
- `graph read <src_id>` — read full source content
- `graph context <src_id> <turn>` — show turns around a hit
- `graph sources --project X --type Y` — list sources
- `graph note "text" --tags x,y` — drop a trail marker
- `graph link <bead> <src> -r relation -t turns` — create provenance edge
- `graph agent-runs --list` — show subagent traces
- `graph attention --last N` — show recent human input

### Beads (`bd`)
- `bd ready` — show unblocked work
- `bd show <id>` — bead details
- `bd update <id> --notes "progress"` — update progress
- `bd close <id> --reason "done"` — close when complete

### Workflow
- After completing work: write experience_report.md with tool feedback
- Drop trail markers for pitfalls discovered: `graph note "..." --tags pitfall`
- Link your work to the bead: `graph link <bead> <source> -r implemented_by`
""")

    if close_db:
        db.close()

    return "\n".join(sections)


def primer_for_bead(bead_id: str) -> str:
    """Convenience function — generate primer and return as string."""
    return generate_primer(bead_id)
