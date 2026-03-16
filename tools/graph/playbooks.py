"""Playbook catalog and librarian for the Autonomy Knowledge Graph.

Playbooks are operational guides derived from session logs and docs.
They serve specific audiences (agents, humans, architects) with
distilled, actionable knowledge from the ground truth.

The librarian's job:
1. Maintain a catalog of playbooks (what exists, what's stale)
2. Prioritize which playbooks need updating
3. Generate/regenerate playbooks from source material

Playbooks are stored as nodes (type='playbook') in the hierarchy,
with their content as a thought linked to a 'playbook' source.
"""

from __future__ import annotations
import json
from pathlib import Path

from .db import GraphDB, DEFAULT_DB
from .models import Source, Thought, Node, new_id, now_iso


# ── Playbook Catalog ─────────────────────────────────────────

# Priority tiers:
#   P0 = critical for agent operation (must always be current)
#   P1 = important for productivity (update weekly)
#   P2 = useful reference (update when source changes)
#   P3 = nice to have (update opportunistically)

PLAYBOOK_CATALOG = [
    # ── Tool Operations (P0 — agents need these to function) ──
    {
        "id": "pb-scraper",
        "title": "Scraper: Launch, Auth, Extract, Convert",
        "audience": "agent",
        "priority": "P0",
        "description": "How to launch the stealth browser, verify auth, extract conversations from ChatGPT/Claude.ai, convert to markdown",
        "source_queries": ["scraper launch browser", "chatgpt extract DOM", "convert markdown"],
        "source_projects": ["autonomy"],
    },
    {
        "id": "pb-graph-cli",
        "title": "Knowledge Graph CLI Reference",
        "audience": "agent",
        "priority": "P0",
        "description": "All graph CLI commands, flags, examples. Search, read, ingest, watch, scope.",
        "source_queries": ["graph search", "graph read", "graph ingest"],
        "source_projects": ["autonomy"],
    },
    {
        "id": "pb-session-ingestion",
        "title": "Session Ingestion Pipeline",
        "audience": "agent",
        "priority": "P0",
        "description": "How Claude Code sessions are captured, parsed, incrementally ingested. Hooks, file sizes, dedup.",
        "source_queries": ["session ingestion JSONL", "incremental ingest", "SessionStart hook"],
        "source_projects": ["autonomy"],
    },

    # ── Agent Patterns (P1 — learned patterns for agent design) ──
    {
        "id": "pb-container-arch",
        "title": "Containerized Agent Architecture",
        "audience": "architect",
        "priority": "P1",
        "description": "DinD, isolation, port conflicts, baked images, overlay volumes, startup sequence",
        "source_queries": ["containerized agent docker", "DinD agent isolation"],
        "source_projects": ["jira", "enterprise-ng"],
    },
    {
        "id": "pb-agent-failures",
        "title": "Agent Failure Modes & Mitigations",
        "audience": "architect",
        "priority": "P1",
        "description": "Destructive actions, stop-instruction ignoring, wrong-repo analysis, premature completion claims",
        "source_queries": ["agent failure destructive", "stop instruction", "runtime verification"],
        "source_projects": ["jira", "enterprise-ng"],
    },
    {
        "id": "pb-sprint-orchestration",
        "title": "Sprint Agent Orchestration",
        "audience": "operator",
        "priority": "P1",
        "description": "Architect→Planner→Runner→Reviewer→Consolidator pipeline, decision files, iteration limits",
        "source_queries": ["sprint orchestration", "runner reviewer consolidator"],
        "source_projects": ["jira"],
    },
    {
        "id": "pb-prompt-engineering",
        "title": "Agent Prompt Engineering Lessons",
        "audience": "architect",
        "priority": "P1",
        "description": "Phase 0 validation, progressive spec decomposition, rules files, targeted refinement",
        "source_queries": ["Phase 0 scope validation", "prompt engineering agent", "rules files"],
        "source_projects": ["jira"],
    },

    # ── Architecture (P1 — design intent) ──
    {
        "id": "pb-sovereignty",
        "title": "Autonomy Sovereignty Model",
        "audience": "architect",
        "priority": "P1",
        "description": "Sovereignty line, above/below split, 99/1 insight, constitutional center",
        "source_queries": ["sovereignty line", "constitutional center"],
        "source_projects": ["autonomy"],
    },
    {
        "id": "pb-autonomy-arch",
        "title": "Autonomy Network Architecture",
        "audience": "architect",
        "priority": "P1",
        "description": "Core→Runtime→Modules→Surface→Infra stack, Alice, Signpost, Channels",
        "source_queries": ["Autonomy Core Runtime", "Autonomy architecture stack"],
        "source_projects": ["autonomy"],
    },

    # ── Enterprise Codebase (P2 — reference for enterprise work) ──
    {
        "id": "pb-vuln-scanning",
        "title": "Vulnerability Scanning Pipeline",
        "audience": "developer",
        "priority": "P2",
        "description": "CVSS encoding, provider-based keying, vuln_metadata_loader, _best_cvss_subquery",
        "source_queries": ["CVSS encoding", "vuln_metadata_loader", "provider-based keying"],
        "source_projects": ["enterprise-ng"],
    },
    {
        "id": "pb-job-framework",
        "title": "Enterprise Job Framework",
        "audience": "developer",
        "priority": "P2",
        "description": "Job lifecycle, sidecar pattern, SQL maintenance, worker orchestration",
        "source_queries": ["job framework sidecar", "job lifecycle"],
        "source_projects": ["enterprise-ng", "jira"],
    },

    # ── Research & Third-Party (P3 — external references) ──
    {
        "id": "pb-beads",
        "title": "Beads: Distributed Graph Issue Tracker",
        "audience": "researcher",
        "priority": "P3",
        "description": "Steve Yegge's Beads, SQLite+JSONL, hash IDs, bd ready, GasTown multi-agent",
        "source_queries": ["beads Steve Yegge", "GasTown agent"],
        "source_projects": ["autonomy"],
    },
    {
        "id": "pb-crdt",
        "title": "CRDTs, Automerge, Peritext, Loro",
        "audience": "researcher",
        "priority": "P3",
        "description": "Conflict-free data types for sovereign collaborative editing",
        "source_queries": ["CRDT Automerge", "Peritext Loro"],
        "source_projects": ["autonomy"],
    },
]


def get_catalog() -> list[dict]:
    """Return the full playbook catalog."""
    return PLAYBOOK_CATALOG


def get_playbook_status(db: GraphDB) -> list[dict]:
    """Check which playbooks exist, are stale, or missing."""
    statuses = []
    for pb in PLAYBOOK_CATALOG:
        # Check if playbook source exists
        source_key = f"playbook:{pb['id']}"
        existing = db.get_source_by_path(source_key)

        if existing:
            meta = json.loads(existing["metadata"]) if existing["metadata"] else {}
            generated_at = meta.get("generated_at", existing.get("ingested_at", ""))
            statuses.append({
                **pb,
                "status": "current",
                "source_id": existing["id"],
                "generated_at": generated_at,
            })
        else:
            statuses.append({
                **pb,
                "status": "missing",
                "source_id": None,
                "generated_at": None,
            })

    return statuses


def save_playbook(db: GraphDB, playbook_id: str, content: str, generated_by: str = "librarian") -> dict:
    """Save or update a playbook's content in the graph."""
    catalog_entry = next((pb for pb in PLAYBOOK_CATALOG if pb["id"] == playbook_id), None)
    if not catalog_entry:
        return {"status": "error", "reason": f"Unknown playbook: {playbook_id}"}

    source_key = f"playbook:{playbook_id}"
    existing = db.get_source_by_path(source_key)
    if existing:
        db.delete_source(existing["id"])

    source = Source(
        type="playbook",
        platform="generated",
        project=catalog_entry.get("source_projects", [None])[0],
        title=catalog_entry["title"],
        file_path=source_key,
        metadata={
            "playbook_id": playbook_id,
            "audience": catalog_entry["audience"],
            "priority": catalog_entry["priority"],
            "generated_by": generated_by,
            "generated_at": now_iso(),
        },
    )
    db.insert_source(source)

    # Store as a single thought (the distilled playbook)
    t = Thought(
        source_id=source.id,
        content=content,
        role="user",
        turn_number=1,
    )
    db.insert_thought(t)
    db.commit()

    return {
        "status": "saved",
        "source_id": source.id,
        "playbook_id": playbook_id,
        "title": catalog_entry["title"],
        "content_length": len(content),
    }
