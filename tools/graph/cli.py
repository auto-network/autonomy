#!/usr/bin/env python3
"""CLI for the Autonomy Knowledge Graph."""

from __future__ import annotations
import argparse
import json
import os
import sys
import textwrap
from pathlib import Path

from .db import GraphDB, DEFAULT_DB


# ── Scoped Access ────────────────────────────────────────────
# GRAPH_SCOPE env var transparently restricts all queries to a project.
# GRAPH_DB env var overrides the database path.
# Agents never see these — they just run `graph search "foo"`.

def _get_scope() -> str | None:
    """Get the active scope from env. Returns project name or None."""
    return os.environ.get("GRAPH_SCOPE")


def _get_db_path() -> Path:
    """Get DB path from env or default."""
    env_db = os.environ.get("GRAPH_DB")
    return Path(env_db) if env_db else DEFAULT_DB


def _apply_scope(args) -> None:
    """If GRAPH_SCOPE is set and args has a project field, enforce it."""
    scope = _get_scope()
    if scope:
        # Override project on any command that supports it
        if hasattr(args, 'project') and args.project is None:
            args.project = scope
from .ingest import (
    ingest_conversation, ingest_musing, ingest_directory,
    ingest_claude_code_session, ingest_claude_code_project, ingest_all_claude_code,
    ingest_status_file, ingest_status_dir, ingest_git_commits,
    ingest_doc_file, ingest_docs_dir,
)
from .watch import watch_sessions
from .playbooks import get_catalog, get_playbook_status, save_playbook


def cmd_ingest(args):
    """Ingest files or directories into the graph."""
    db = GraphDB(args.db)
    path = Path(args.path)

    if path.is_dir():
        # Check if this looks like a Claude Code project dir (contains .jsonl files)
        jsonl_files = list(path.glob("*.jsonl"))
        if jsonl_files:
            results = ingest_claude_code_project(db, path, force=args.force)
        else:
            results = ingest_directory(db, path, force=args.force)
        for r in results:
            status = r["status"]
            f = r.get("file", "?")
            if status == "ingested":
                title = r.get("title", "")
                if title:
                    title = f" — {title[:60]}"
                print(f"  + {Path(f).name}: {r.get('thoughts', 0)} thoughts, "
                      f"{r.get('derivations', 0)} derivations, {r.get('entities', 0)} entities{title}")
            else:
                print(f"  ~ {Path(f).name}: {status} ({r.get('reason', '')})")
    elif path.is_file():
        if path.suffix == ".jsonl":
            result = ingest_claude_code_session(db, path, force=args.force)
        else:
            text = path.read_text()
            if "## Turn " in text:
                result = ingest_conversation(db, path, force=args.force)
            else:
                result = ingest_musing(db, path, force=args.force)
        print(json.dumps(result, indent=2))
    else:
        print(f"Error: {path} not found", file=sys.stderr)
        sys.exit(1)

    db.close()


def cmd_search(args):
    """Full-text search across the graph."""
    db = GraphDB(args.db)
    results = db.search(args.query, limit=args.limit, project=getattr(args, 'project', None))

    if not results:
        print("No results found.")
        db.close()
        return

    width = args.width
    for r in results:
        rtype = r["result_type"][0].upper()  # T or D
        source = r.get("source_title", "?")
        project = r.get("project", "")
        turn = r.get("turn_number", "?")
        sid = r.get("source_id", "?")[:12]
        content = r["content"]
        # Show more content — wrap to width, indent continuation lines
        if len(content) > width:
            content = content[:width] + "…"
        lines = content.replace("\n", "\n    ").rstrip()
        proj_tag = f" [{project}]" if project else ""
        print(f"  [{rtype}] {source[:50]} t{turn} (src:{sid}){proj_tag}")
        print(f"    {lines}")
        print()

    db.close()


def cmd_read(args):
    """Read full content of a source by ID or title search."""
    db = GraphDB(args.db)

    # Try as source ID first (or prefix)
    source = db.get_source(args.source)
    if not source:
        # Try prefix match
        row = db.conn.execute(
            "SELECT * FROM sources WHERE id LIKE ? LIMIT 1", (f"{args.source}%",)
        ).fetchone()
        if row:
            source = dict(row)

    if not source:
        # Try title search
        sources = db.find_sources(args.source, limit=5)
        # Scope filter
        scope = _get_scope()
        if scope:
            sources = [s for s in sources if s.get("project") == scope]
        if not sources:
            print(f"No source found matching '{args.source}'")
            db.close()
            return
        if len(sources) > 1 and not args.first:
            print(f"Multiple sources match '{args.source}':")
            for s in sources:
                proj = f" [{s['project']}]" if s.get('project') else ""
                print(f"  {s['id'][:12]}  {s['type']:10s}  {s.get('title', '?')[:60]}{proj}")
            print(f"\nUse the source ID to read a specific one, or --first to read the top match.")
            db.close()
            return
        source = sources[0]

    # Print source header
    proj = f" [{source['project']}]" if source.get('project') else ""
    print(f"Source: {source['id'][:12]}  {source['type']}{proj}")
    print(f"Title:  {source.get('title', '?')}")
    print(f"Date:   {source.get('created_at', '?')[:10]}")
    print(f"{'─' * 72}")

    # Print all content
    entries = db.get_source_content(source["id"])
    for e in entries:
        role = e.get("role", "?")
        turn = e.get("turn_number", "?")
        etype = e.get("entry_type", "?")
        label = "USER" if etype == "thought" else f"ASSISTANT ({role})"
        content = e["content"]

        if args.max_chars and len(content) > args.max_chars:
            content = content[:args.max_chars] + f"\n... [{len(content) - args.max_chars} chars truncated]"

        print(f"\n## Turn {turn} — {label}")
        print(content)

    db.close()


def cmd_sources(args):
    """List sources with optional filters."""
    db = GraphDB(args.db)
    sources = db.list_sources(project=args.project, source_type=args.type, limit=args.limit)

    if not sources:
        print("No sources found.")
        db.close()
        return

    for s in sources:
        proj = f" [{s['project']}]" if s.get('project') else ""
        date = (s.get("created_at") or "")[:10]
        print(f"  {s['id'][:12]}  {s['type']:10s}  {date}  {s.get('title', '?')[:55]}{proj}")

    db.close()


def cmd_context(args):
    """Show turns around a specific turn in a source — useful for expanding search hits."""
    db = GraphDB(args.db)

    # Find the source
    source = db.get_source(args.source)
    if not source:
        row = db.conn.execute(
            "SELECT * FROM sources WHERE id LIKE ? LIMIT 1", (f"{args.source}%",)
        ).fetchone()
        if row:
            source = dict(row)

    if not source:
        print(f"Source not found: {args.source}")
        db.close()
        return

    entries = db.get_source_content(source["id"])
    target_turn = args.turn
    window = args.window

    # Filter to turns within the window
    relevant = [e for e in entries if abs((e.get("turn_number") or 0) - target_turn) <= window]

    proj = f" [{source['project']}]" if source.get('project') else ""
    print(f"Source: {source.get('title', '?')[:60]}{proj}")
    print(f"Showing turns {target_turn - window}–{target_turn + window}")
    print(f"{'─' * 72}")

    for e in relevant:
        turn = e.get("turn_number", "?")
        etype = e.get("entry_type", "?")
        label = "USER" if etype == "thought" else "ASSISTANT"
        marker = " ◀" if turn == target_turn else ""
        content = e["content"]
        if args.max_chars and len(content) > args.max_chars:
            content = content[:args.max_chars] + f"\n... [{len(content) - args.max_chars} chars truncated]"
        print(f"\n## Turn {turn} — {label}{marker}")
        print(content)

    db.close()


def cmd_entities(args):
    """List or search entities."""
    db = GraphDB(args.db)

    if args.query:
        entities = db.search_entities(args.query, limit=args.limit)
    else:
        entities = db.list_entities(entity_type=args.type, limit=args.limit)

    if not entities:
        print("No entities found.")
        db.close()
        return

    for e in entities:
        mention_count = db.conn.execute(
            "SELECT SUM(count) as total FROM entity_mentions WHERE entity_id = ?",
            (e["id"],)
        ).fetchone()
        mentions = mention_count["total"] or 0
        print(f"  {e['name']:40s}  [{e['type']:12s}]  {mentions:3d} mentions")

    db.close()


def cmd_stats(args):
    """Show database statistics."""
    db = GraphDB(args.db)
    stats = db.stats()
    print("Knowledge Graph Stats:")
    for table, count in stats.items():
        print(f"  {table:20s}  {count:6d}")
    db.close()


def cmd_tree(args):
    """Show the knowledge hierarchy tree."""
    db = GraphDB(args.db)
    nodes = db.get_tree(args.root, depth=args.depth)

    if not nodes:
        print("No hierarchy nodes found. Use 'seed' to create the initial structure.")
        db.close()
        return

    for n in nodes:
        depth = n.get("_depth", 0)
        indent = "  " * depth
        status_icon = {"active": "●", "planned": "○", "in_progress": "◐", "completed": "✓", "deprecated": "✗"}.get(n["status"], "?")
        print(f"  {indent}{status_icon} [{n['type']}] {n['title']}")
        if n.get("description") and args.verbose:
            desc = textwrap.shorten(n["description"], width=80 - len(indent) * 2, placeholder="…")
            print(f"  {indent}  {desc}")

    db.close()


def cmd_seed(args):
    """Seed the knowledge hierarchy with the Autonomy vision structure."""
    db = GraphDB(args.db)

    # Check if already seeded
    existing = db.get_children(None)
    if existing and not args.force:
        print("Hierarchy already seeded. Use --force to re-seed.")
        db.close()
        return

    from .seed import seed_hierarchy
    count = seed_hierarchy(db)
    print(f"Seeded {count} nodes into the knowledge hierarchy.")
    db.close()


def cmd_sessions(args):
    """Ingest Claude Code sessions."""
    db = GraphDB(args.db)

    if args.all:
        results = ingest_all_claude_code(db, force=args.force)
    elif args.project:
        results = ingest_claude_code_project(db, Path(args.project), force=args.force)
    else:
        # Default: current project
        results = ingest_claude_code_project(db, force=args.force)

    ingested = [r for r in results if r["status"] == "ingested"]
    updated = [r for r in results if r["status"] == "updated"]
    skipped = [r for r in results if r["status"] == "skipped"]

    for r in ingested:
        title = r.get("title", "")
        if title:
            title = f" — {title[:50]}"
        tokens = r.get("tokens", 0)
        tok_str = f"  {tokens:,} tokens" if tokens else ""
        print(f"  + {r.get('session_id', '?')[:12]}: "
              f"{r.get('thoughts', 0)} thoughts, {r.get('derivations', 0)} derivations{tok_str}{title}")

    for r in updated:
        print(f"  ~ {r.get('session_id', '?')[:12]}: "
              f"+{r.get('new_thoughts', 0)} thoughts, +{r.get('new_derivations', 0)} derivations "
              f"(turns {r.get('from_turn', '?')}-{r.get('to_turn', '?')})")

    if skipped:
        print(f"  ({len(skipped)} sessions already up to date)")

    print(f"\nTotal: {len(ingested)} new, {len(updated)} updated, {len(skipped)} skipped")
    db.close()


def cmd_docs_ingest(args):
    """Ingest documentation files."""
    db = GraphDB(args.db)
    path = Path(args.path)

    if path.is_file():
        result = ingest_doc_file(db, path, project=args.project, force=args.force)
        print(json.dumps(result, indent=2))
    elif path.is_dir():
        results = ingest_docs_dir(db, path, project=args.project, force=args.force)
        ingested = [r for r in results if r["status"] == "ingested"]
        skipped = [r for r in results if r["status"] == "skipped"]

        for r in ingested:
            print(f"  + {r.get('title', '?')[:60]}: {r.get('thoughts', 0)} sections")

        if skipped:
            print(f"  ({len(skipped)} already ingested)")
        print(f"\nTotal: {len(ingested)} ingested, {len(skipped)} skipped")
    else:
        print(f"Error: {path} not found", file=sys.stderr)
        sys.exit(1)

    db.close()


def cmd_status_ingest(args):
    """Ingest status files from a directory."""
    db = GraphDB(args.db)
    path = Path(args.path)

    if path.is_file():
        result = ingest_status_file(db, path, project=args.project, authorship=args.authorship, force=args.force)
        print(json.dumps(result, indent=2))
    elif path.is_dir():
        results = ingest_status_dir(db, path, project=args.project, authorship=args.authorship, force=args.force)
        ingested = [r for r in results if r["status"] == "ingested"]
        skipped = [r for r in results if r["status"] == "skipped"]

        for r in ingested:
            cat = r.get("category", "?")
            print(f"  + [{cat}] {r.get('title', '?')[:60]}: {r.get('thoughts', 0)} sections")

        if skipped:
            print(f"  ({len(skipped)} already ingested)")
        print(f"\nTotal: {len(ingested)} ingested, {len(skipped)} skipped")
    else:
        print(f"Error: {path} not found", file=sys.stderr)
        sys.exit(1)

    db.close()


def cmd_git_ingest(args):
    """Ingest git commit history."""
    db = GraphDB(args.db)
    result = ingest_git_commits(
        db, args.repo, project=args.project,
        since=args.since, force=args.force,
    )
    if result["status"] == "skipped":
        print(f"  Skipped: {result.get('reason', '?')}")
    else:
        print(f"  {result['status']}: {result.get('commits', 0)} commits from {result.get('repo', '?')}")
    db.close()


def cmd_playbooks(args):
    """Show playbook catalog with status."""
    db = GraphDB(args.db)
    statuses = get_playbook_status(db)

    if args.priority:
        statuses = [s for s in statuses if s["priority"] == args.priority]
    if args.audience:
        statuses = [s for s in statuses if s["audience"] == args.audience]
    if args.missing:
        statuses = [s for s in statuses if s["status"] == "missing"]

    # Group by priority
    by_priority = {}
    for s in statuses:
        p = s["priority"]
        if p not in by_priority:
            by_priority[p] = []
        by_priority[p].append(s)

    for priority in ["P0", "P1", "P2", "P3"]:
        if priority not in by_priority:
            continue
        items = by_priority[priority]
        print(f"\n  {priority} — {'CRITICAL' if priority == 'P0' else 'IMPORTANT' if priority == 'P1' else 'REFERENCE' if priority == 'P2' else 'OPTIONAL'}")
        print(f"  {'─' * 70}")
        for s in items:
            icon = "✓" if s["status"] == "current" else "✗"
            age = ""
            if s.get("generated_at"):
                age = f"  (generated {s['generated_at'][:10]})"
            aud = f"[{s['audience']}]"
            print(f"  {icon} {s['title']:50s} {aud:12s}{age}")
            if s["status"] == "missing":
                print(f"      {s['description'][:70]}")

    total = len(statuses)
    missing = sum(1 for s in statuses if s["status"] == "missing")
    current = total - missing
    print(f"\n  {current}/{total} playbooks current, {missing} missing")
    db.close()


def cmd_projects(args):
    """List projects and their source counts by type."""
    db = GraphDB(args.db)

    # All sources grouped by project and type
    rows = db.conn.execute(
        """SELECT project, type, COUNT(*) as count,
                  MIN(created_at) as first, MAX(created_at) as last
           FROM sources
           WHERE project IS NOT NULL
           GROUP BY project, type
           ORDER BY project, type"""
    ).fetchall()

    if not rows:
        print("No projects found.")
        db.close()
        return

    # Group by project
    projects = {}
    for r in rows:
        p = r["project"]
        if p not in projects:
            projects[p] = {"types": {}, "total": 0, "last": ""}
        projects[p]["types"][r["type"]] = r["count"]
        projects[p]["total"] += r["count"]
        if r["last"] and r["last"] > projects[p]["last"]:
            projects[p]["last"] = r["last"]

    print(f"  {'Project':30s}  {'Total':>6s}  {'Sessions':>8s}  {'Status':>7s}  {'Git':>5s}  {'Other':>6s}  {'Last':>12s}")
    print(f"  {'─' * 30}  {'─' * 6}  {'─' * 8}  {'─' * 7}  {'─' * 5}  {'─' * 6}  {'─' * 12}")
    for name, info in sorted(projects.items(), key=lambda x: -x[1]["total"]):
        display = name
        if display.startswith("-home-jeremy-"):
            display = display[13:] or "(home)"
        if display.startswith("workspace-"):
            display = display[10:]
        sessions = info["types"].get("session", 0)
        status = info["types"].get("status", 0)
        git = info["types"].get("git-log", 0)
        other = info["total"] - sessions - status - git
        last = info["last"][:10] if info["last"] else "—"
        print(f"  {display:30s}  {info['total']:6d}  {sessions:8d}  {status:7d}  {git:5d}  {other:6d}  {last:>12s}")

    db.close()


def cmd_watch(args):
    """Watch sessions live and ingest incrementally."""
    watch_sessions(
        db_path=args.db,
        interval=args.interval,
        project_filter=args.project,
        verbose=args.verbose,
    )


def cmd_related(args):
    """Find content related to a search term."""
    db = GraphDB(args.db)

    # Find entity
    entities = db.search_entities(args.term)
    if not entities:
        print(f"No entity found matching '{args.term}'")
        db.close()
        return

    entity = entities[0]
    print(f"Entity: {entity['name']} [{entity['type']}]\n")

    # Find thoughts mentioning this entity
    thoughts = db.entity_thoughts(entity["id"])
    if thoughts:
        print(f"Referenced in {len(thoughts)} thought(s):")
        for t in thoughts[:10]:
            snippet = textwrap.shorten(t["content"], width=100, placeholder="…")
            print(f"  [{t['platform']}/{t['source_title']}] turn {t.get('turn_number', '?')}")
            print(f"    {snippet}")
            print()

    db.close()


def main():
    parser = argparse.ArgumentParser(
        prog="autonomy-graph",
        description="Autonomy Knowledge Graph CLI",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Database path")

    sub = parser.add_subparsers(dest="command", required=True)

    # ingest
    p = sub.add_parser("ingest", help="Ingest files or directories")
    p.add_argument("path", help="File or directory to ingest")
    p.add_argument("--force", action="store_true", help="Re-ingest existing sources")
    p.set_defaults(func=cmd_ingest)

    # search
    p = sub.add_parser("search", help="Full-text search")
    p.add_argument("query", help="Search query")
    p.add_argument("--project", "-p", help="Filter by project name")
    p.add_argument("--limit", type=int, default=10, help="Max results")
    p.add_argument("--width", "-w", type=int, default=500, help="Max chars per result (default 500)")
    p.set_defaults(func=cmd_search)

    # read
    p = sub.add_parser("read", help="Read full content of a source")
    p.add_argument("source", help="Source ID (or prefix) or title search term")
    p.add_argument("--first", action="store_true", help="Read first match if multiple")
    p.add_argument("--max-chars", type=int, default=0, help="Max chars per turn (0=unlimited)")
    p.set_defaults(func=cmd_read)

    # sources
    p = sub.add_parser("sources", help="List sources")
    p.add_argument("--project", "-p", help="Filter by project")
    p.add_argument("--type", "-t", help="Filter by type (session, status, git-log, etc.)")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    p.set_defaults(func=cmd_sources)

    # context
    p = sub.add_parser("context", help="Show turns around a search hit")
    p.add_argument("source", help="Source ID or prefix")
    p.add_argument("turn", type=int, help="Turn number to center on")
    p.add_argument("--window", type=int, default=3, help="Turns before/after (default 3)")
    p.add_argument("--max-chars", type=int, default=0, help="Max chars per turn (0=unlimited)")
    p.set_defaults(func=cmd_context)

    # entities
    p = sub.add_parser("entities", help="List or search entities")
    p.add_argument("--query", "-q", help="Filter by name")
    p.add_argument("--type", "-t", help="Filter by entity type")
    p.add_argument("--limit", type=int, default=50, help="Max results")
    p.set_defaults(func=cmd_entities)

    # related
    p = sub.add_parser("related", help="Find content related to a term")
    p.add_argument("term", help="Entity name to find")
    p.set_defaults(func=cmd_related)

    # stats
    p = sub.add_parser("stats", help="Database statistics")
    p.set_defaults(func=cmd_stats)

    # tree
    p = sub.add_parser("tree", help="Show knowledge hierarchy")
    p.add_argument("--root", help="Root node ID (default: show all)")
    p.add_argument("--depth", type=int, default=10, help="Max depth")
    p.add_argument("--verbose", "-v", action="store_true", help="Show descriptions")
    p.set_defaults(func=cmd_tree)

    # sessions
    p = sub.add_parser("sessions", help="Ingest Claude Code sessions")
    p.add_argument("--all", action="store_true", help="Ingest all projects, not just current")
    p.add_argument("--project", help="Specific project path")
    p.add_argument("--force", action="store_true", help="Re-ingest existing sessions")
    p.set_defaults(func=cmd_sessions)

    # seed
    p = sub.add_parser("seed", help="Seed the knowledge hierarchy")
    p.add_argument("--force", action="store_true", help="Re-seed (deletes existing)")
    p.set_defaults(func=cmd_seed)

    # docs-ingest
    p = sub.add_parser("docs-ingest", help="Ingest documentation files (TOOL.md, README.md, etc.)")
    p.add_argument("path", help="File or directory to scan")
    p.add_argument("--project", "-p", help="Project name to tag with")
    p.add_argument("--force", action="store_true", help="Re-ingest existing files")
    p.set_defaults(func=cmd_docs_ingest)

    # status-ingest
    p = sub.add_parser("status-ingest", help="Ingest status files from a directory")
    p.add_argument("path", help="File or directory to ingest")
    p.add_argument("--project", "-p", help="Project name to tag these with")
    p.add_argument("--authorship", "-a", default="mixed",
                   choices=["human", "agent", "mixed"],
                   help="Provenance: human, agent, or mixed (default: mixed)")
    p.add_argument("--force", action="store_true", help="Re-ingest existing files")
    p.set_defaults(func=cmd_status_ingest)

    # git-ingest
    p = sub.add_parser("git-ingest", help="Ingest git commit history")
    p.add_argument("repo", help="Path to git repository")
    p.add_argument("--project", "-p", help="Project name to tag with")
    p.add_argument("--since", help="Only commits after this date (e.g. 2025-10-01)")
    p.add_argument("--force", action="store_true", help="Re-ingest from scratch")
    p.set_defaults(func=cmd_git_ingest)

    # projects
    p = sub.add_parser("projects", help="List projects and session counts")
    p.set_defaults(func=cmd_projects)

    # playbooks
    p = sub.add_parser("playbooks", help="Show playbook catalog and status")
    p.add_argument("--priority", choices=["P0", "P1", "P2", "P3"], help="Filter by priority")
    p.add_argument("--audience", help="Filter by audience (agent, architect, operator, developer, researcher)")
    p.add_argument("--missing", action="store_true", help="Show only missing playbooks")
    p.set_defaults(func=cmd_playbooks)

    # watch
    p = sub.add_parser("watch", help="Live-watch sessions and ingest incrementally")
    p.add_argument("--interval", type=float, default=5.0, help="Poll interval in seconds")
    p.add_argument("--project", "-p", help="Filter to projects matching this substring")
    p.add_argument("--verbose", "-v", action="store_true", help="Show skip events too")
    p.set_defaults(func=cmd_watch)

    args = parser.parse_args()

    # Apply scope from environment
    _apply_scope(args)

    # Show scope banner if active
    scope = _get_scope()
    if scope and args.command in ("search", "sources", "read", "context", "entities", "related"):
        print(f"  [scope: {scope}]", file=sys.stderr)

    args.func(args)


if __name__ == "__main__":
    main()
