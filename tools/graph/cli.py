#!/usr/bin/env python3
"""CLI for the Autonomy Knowledge Graph."""

from __future__ import annotations
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import time
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
    """Get DB path with resolution order: (a) GRAPH_DB env, (b) ./data/graph.db, (c) legacy parents[2] fallback."""
    env_db = os.environ.get("GRAPH_DB")
    if env_db:
        return Path(env_db)
    cwd_db = Path.cwd() / "data" / "graph.db"
    if cwd_db.exists():
        return cwd_db
    return DEFAULT_DB


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
from .agent_runs import ingest_all_agent_runs, discover_subagent_traces, parse_agent_trace
from .primer import generate_primer, collect_primer_data, format_for_agent, format_for_dashboard


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
    results = db.search(args.query, limit=args.limit, project=getattr(args, 'project', None), or_mode=getattr(args, 'or_mode', False))

    if args.json:
        import json as _json
        # Truncate content for JSON output
        for r in results:
            if len(r.get("content", "")) > args.width:
                r["content"] = r["content"][:args.width] + "…"
        print(_json.dumps(results, default=str))
        db.close()
        return

    if not results:
        print("No results found.")
        db.close()
        return

    width = args.width
    for r in results:
        rtype = r["result_type"][0].upper()  # T or D
        source = r.get("source_title") or "?"
        project = r.get("project", "")
        turn = r.get("turn_number", "?")
        sid = r.get("source_id", "?")[:12]
        content = r["content"]
        if len(content) > width:
            content = content[:width] + "…"
        lines = content.replace("\n", "\n    ").rstrip()
        proj_tag = f" [{project}]" if project else ""
        print(f"  [{rtype}] {source[:50]} t{turn} (src:{sid}){proj_tag}")
        print(f"    {lines}")
        print()

    db.close()


def _resolve_source(db, source_arg, first=False):
    """Resolve a source by ID, prefix, or title search. Returns dict or None."""
    source = db.get_source(source_arg)
    if not source:
        sources = db.find_sources(source_arg, limit=5)
        scope = _get_scope()
        if scope:
            sources = [s for s in sources if s.get("project") == scope]
        if not sources:
            return None
        if len(sources) > 1 and not first:
            return sources  # Return list for disambiguation
        source = sources[0]
    return source


def cmd_read(args):
    """Read full content of a source by ID or title search."""
    db = GraphDB(args.db)
    import json as _json

    result = _resolve_source(db, args.source, first=args.first)
    if result is None:
        print(f"No source found matching '{args.source}'")
        db.close()
        return
    if isinstance(result, list):
        print(f"Multiple sources match '{args.source}':")
        for s in result:
            proj = f" [{s['project']}]" if s.get('project') else ""
            print(f"  {s['id'][:12]}  {s['type']:10s}  {s.get('title', '?')[:60]}{proj}")
        print(f"\nUse the source ID to read a specific one, or --first to read the top match.")
        db.close()
        return
    source = result

    entries = db.get_source_content(source["id"])

    # Get edges for this source
    edges = db.conn.execute(
        """SELECT * FROM edges
           WHERE source_id = ? OR target_id = ?""",
        (source["id"], source["id"]),
    ).fetchall()

    if args.json:
        # Structured JSON output
        entry_list = []
        for e in entries:
            entry = {
                "id": e.get("id"),
                "entry_type": e.get("entry_type"),
                "role": e.get("role"),
                "turn_number": e.get("turn_number"),
                "content": e["content"],
                "message_id": e.get("message_id"),
                "metadata": e.get("metadata"),
            }
            if args.max_chars and len(entry["content"]) > args.max_chars:
                entry["content"] = entry["content"][:args.max_chars] + "…"
                entry["truncated"] = True
            entry_list.append(entry)

        edge_list = [dict(e) for e in edges]

        output = {
            "source": {
                "id": source["id"],
                "type": source.get("type"),
                "title": source.get("title"),
                "project": source.get("project"),
                "platform": source.get("platform"),
                "created_at": source.get("created_at"),
                "metadata": source.get("metadata"),
            },
            "entries": entry_list,
            "edges": edge_list,
        }
        print(_json.dumps(output, default=str))
        db.close()
        return

    # Text output (existing behavior)
    proj = f" [{source['project']}]" if source.get('project') else ""
    print(f"Source: {source['id'][:12]}  {source['type']}{proj}")
    print(f"Title:  {source.get('title', '?')}")
    print(f"Date:   {source.get('created_at', '?')[:10]}")
    print(f"{'─' * 72}")

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


def cmd_bead(args):
    """Create a bead with provenance — links to the source conversation turns that inspired it."""
    import subprocess
    db = GraphDB(args.db)
    from .models import Edge

    # First, refresh the graph to capture latest turns
    subprocess.run(["graph", "sessions", "--all"], capture_output=True, timeout=30)

    # Build bd create command — always set readiness:idea as pipeline entry point
    cmd = ["bd", "create", args.title, "-p", str(args.priority), "-l", "readiness:idea"]
    if args.desc:
        cmd += ["-d", args.desc]
    if args.type:
        cmd += ["-t", args.type]

    # Run bd create and capture output
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        print(f"bd create failed: {result.stderr}")
        db.close()
        return

    # Parse bead ID from output (format: "✓ Created issue: auto-xxx — Title")
    import re
    match = re.search(r"Created issue: (\S+)", result.stdout)
    if not match:
        print(result.stdout)
        db.close()
        return
    bead_id = match.group(1)

    print(result.stdout.strip())

    # If source + turns provided, create the conceived_at link
    if args.source and args.turns:
        source = db.get_source(args.source)
        if source:
            turn_range = {}
            parts = args.turns.split("-")
            if len(parts) == 2:
                turn_range = {"from": int(parts[0]), "to": int(parts[1])}
            elif len(parts) == 1:
                turn_range = {"from": int(parts[0]), "to": int(parts[0])}

            metadata = {"turns": turn_range}
            if args.note:
                metadata["note"] = args.note

            db.insert_edge(Edge(
                source_id=bead_id,
                source_type="bead",
                target_id=source["id"],
                target_type="source",
                relation="conceived_at",
                metadata=metadata,
            ))
            db.commit()
            turns_str = f" turns {args.turns}" if args.turns else ""
            print(f"  ✓ linked: {bead_id} —[conceived_at]→ {source['id'][:12]}{turns_str}")

    db.close()


def cmd_link(args):
    """Create a provenance edge between a bead and session turns."""
    db = GraphDB(args.db)
    from .models import Edge

    # Parse turn range
    turn_range = None
    if args.turns:
        parts = args.turns.split("-")
        if len(parts) == 2:
            turn_range = {"from": int(parts[0]), "to": int(parts[1])}
        elif len(parts) == 1:
            turn_range = {"from": int(parts[0]), "to": int(parts[0])}

    # Find source by ID prefix or session UUID
    source = db.get_source(args.source)
    if not source:
        print(f"Source not found: {args.source}")
        db.close()
        return

    metadata = {}
    if turn_range:
        metadata["turns"] = turn_range
    if args.note:
        metadata["note"] = args.note

    db.insert_edge(Edge(
        source_id=args.bead,
        source_type="bead",
        target_id=source["id"],
        target_type="source",
        relation=args.relation,
        metadata=metadata,
    ))
    db.commit()

    turns_str = f" turns {args.turns}" if args.turns else ""
    print(f"  ✓ {args.bead} —[{args.relation}]→ {source['id'][:12]}{turns_str}")
    db.close()


def cmd_attention(args):
    """Show human input from sessions, chronologically. Fast query for sovereign content."""
    db = GraphDB(args.db)
    import json as _json
    from pathlib import Path

    session_file = args.session
    if not session_file:
        # Find the most recent session for the current project
        projects_dir = Path.home() / ".claude" / "projects" / "-home-jeremy-workspace-autonomy"
        jsonl_files = sorted(projects_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
        if jsonl_files:
            session_file = str(jsonl_files[0])
        else:
            print("No session found.")
            db.close()
            return

    messages = []
    with open(session_file) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = _json.loads(line)
            except:
                continue

            if e.get("isSidechain"):
                continue

            etype = e.get("type")
            content = ""

            if etype == "user" and not e.get("isMeta"):
                c = e.get("message", {}).get("content", "")
                if isinstance(c, str) and len(c) > 5:
                    content = c
                    msg_type = "input"

            elif etype == "queue-operation":
                c = e.get("content", e.get("message", {}).get("content", ""))
                if isinstance(c, str) and len(c) > 5:
                    content = c
                    msg_type = "queued"

            if not content:
                continue

            # Skip command outputs and task notifications
            if content.startswith("<local-command") or content.startswith("<task-notification"):
                continue
            if content.startswith("<command-name>"):
                continue

            ts = e.get("timestamp", "")[:19]
            uuid = e.get("uuid", "")[:12]

            messages.append({
                "ts": ts,
                "uuid": uuid,
                "type": msg_type,
                "text": content,
            })

    # Apply filters
    if args.last:
        messages = messages[-args.last:]

    if args.search:
        query = args.search.lower()
        messages = [m for m in messages if query in m["text"].lower()]

    for m in messages:
        tag = " [queued]" if m["type"] == "queued" else ""
        preview = m["text"][:200].replace("\n", " ")
        if len(m["text"]) > 200:
            preview += "…"
        print(f"  [{m['ts']}]{tag}  {preview}")

    print(f"\n  {len(messages)} messages")
    db.close()


def cmd_note(args):
    """Drop a searchable trail marker into the graph."""
    db = GraphDB(args.db)
    text = " ".join(args.text)
    tags = args.tags.split(",") if args.tags else []

    from .models import Source, Thought, new_id, now_iso
    source_key = f"note:{new_id()}"
    source = Source(
        type="note",
        platform="local",
        project=args.project or _get_scope(),
        title=text[:80],
        file_path=source_key,
        metadata={"tags": tags, "author": args.author or "user"},
    )
    db.insert_source(source)

    t = Thought(
        source_id=source.id,
        content=text,
        role="user",
        turn_number=1,
        tags=tags,
    )
    db.insert_thought(t)

    from .ingest import extract_entities
    for name, etype in extract_entities(text):
        eid = db.upsert_entity(name, etype)
        db.add_mention(eid, t.id, "thought")

    db.commit()
    print(f"  ✓ Note saved (src:{source.id[:12]})")
    db.close()


def cmd_agent_runs(args):
    """Discover and ingest subagent traces."""
    db = GraphDB(args.db)

    if args.list_only:
        traces = discover_subagent_traces(session_id=args.session)
        if not traces:
            print("No subagent traces found.")
        else:
            for t in traces:
                run = parse_agent_trace(t)
                tokens = run.total_input_tokens + run.total_output_tokens
                prompt = run.prompt[:60].replace("\n", " ") if run.prompt else "?"
                print(f"  {run.agent_id[:16]:16s}  {run.total_tool_uses:3d} tools  {tokens:6d} tok  {prompt}")
            print(f"\n  {len(traces)} traces found")
        db.close()
        return

    results = ingest_all_agent_runs(db, session_id=args.session, force=args.force)
    ingested = [r for r in results if r["status"] == "ingested"]
    skipped = [r for r in results if r["status"] == "skipped"]

    for r in ingested:
        bead = f" bead:{r.get('bead_id', '?')}" if r.get("bead_id") else ""
        print(f"  + {r.get('agent_id', '?')[:16]}: "
              f"{r.get('thoughts', 0)} thoughts, {r.get('derivations', 0)} derivations, "
              f"{r.get('tool_uses', 0)} tool uses{bead}")

    if skipped:
        print(f"  ({len(skipped)} already ingested)")
    print(f"\nTotal: {len(ingested)} ingested, {len(skipped)} skipped")
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


def _cmd_primer(args):
    """Generate primer with the requested format."""
    data = collect_primer_data(
        args.bead_id,
        db=GraphDB(args.db),
        include_provenance=not args.no_provenance,
        include_pitfalls=not args.no_pitfalls,
    )
    if args.format == "dashboard":
        import json as _json
        return _json.dumps(format_for_dashboard(data), indent=2)
    return format_for_agent(data)


def cmd_wait(args):
    """Block until a dispatched bead completes, then print a compact report."""
    bead_id = args.bead_id
    timeout = args.timeout
    poll_interval = 2.0

    repo_root = Path(__file__).resolve().parent.parent.parent
    dispatch_db = repo_root / "data" / "dispatch.db"

    # ── Step 1: Check bead exists and is approved ──
    try:
        result = subprocess.run(
            ["bd", "show", bead_id, "--json"],
            capture_output=True, text=True, timeout=15,
            cwd=str(repo_root),
        )
        bd_out = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"Error: could not run bd: {e}", file=sys.stderr)
        sys.exit(1)

    if not bd_out or result.returncode != 0:
        print(f"Error: Bead '{bead_id}' not found. Check the ID and try again.", file=sys.stderr)
        sys.exit(1)

    try:
        bead_data = json.loads(bd_out)
        if isinstance(bead_data, list) and bead_data:
            bead_data = bead_data[0]
    except json.JSONDecodeError:
        print(f"Error: Bead '{bead_id}' not found.", file=sys.stderr)
        sys.exit(1)

    # Check readiness
    labels = bead_data.get("labels") or []
    readiness = "idea"
    for label in labels:
        if label.startswith("readiness:"):
            readiness = label.split(":", 1)[1]
            break

    if readiness != "approved":
        print(f"Error: Bead {bead_id} is not approved for dispatch (current: readiness:{readiness})",
              file=sys.stderr)
        sys.exit(1)

    # ── Step 2: Ensure dispatch.db exists ──
    if not dispatch_db.exists():
        print(f"Error: dispatch database not found at {dispatch_db}", file=sys.stderr)
        sys.exit(1)

    # ── Step 3: Poll for completion ──
    last_status = None
    start_time = time.monotonic()

    while True:
        elapsed = time.monotonic() - start_time
        if elapsed > timeout:
            # Timeout — report last known state
            print(f"\nError: Timeout after {timeout}s waiting for {bead_id}", file=sys.stderr)
            if last_status:
                print(f"  Last known state: {last_status}", file=sys.stderr)
            sys.exit(1)

        # Query dispatch_runs for a completed row
        try:
            conn = sqlite3.connect(str(dispatch_db))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT * FROM dispatch_runs
                   WHERE bead_id = ? AND completed_at IS NOT NULL
                   ORDER BY completed_at DESC LIMIT 1""",
                (bead_id,),
            ).fetchone()
            conn.close()
        except sqlite3.Error:
            row = None

        if row:
            # ── Step 5: Print report ──
            run = dict(row)
            status = run.get("status", "UNKNOWN")
            duration = run.get("duration_secs")
            duration_str = f"{duration}s" if duration is not None else "?"

            if status == "DONE":
                # Success report
                commit = run.get("commit_hash", "")[:7] or "none"
                added = run.get("lines_added") or 0
                removed = run.get("lines_removed") or 0
                files = run.get("files_changed") or 0
                message = run.get("commit_message") or ""
                reason = run.get("reason") or ""
                discovered = run.get("discovered_beads_count") or 0

                score_t = run.get("score_tooling")
                score_cl = run.get("score_clarity")
                score_co = run.get("score_confidence")

                print(f"\u2713 {bead_id} DONE ({duration_str})")
                if commit != "none":
                    print(f"  Commit: {commit} (+{added} -{removed}, {files} files)")
                if message:
                    print(f"  Message: {message}")
                if score_t is not None or score_cl is not None or score_co is not None:
                    parts = []
                    if score_t is not None:
                        parts.append(f"tooling={score_t}")
                    if score_cl is not None:
                        parts.append(f"clarity={score_cl}")
                    if score_co is not None:
                        parts.append(f"confidence={score_co}")
                    print(f"  Scores: {' '.join(parts)}")
                if discovered:
                    print(f"  Discovered: {discovered} beads")
                if reason:
                    print(f"  Decision: {reason}")
            else:
                # Failure report (FAILED, BLOCKED, UNKNOWN)
                exit_code = run.get("exit_code")
                failure_cat = run.get("failure_category") or ""
                reason = run.get("reason") or ""

                print(f"\u2717 {bead_id} {status} ({duration_str})")
                if exit_code is not None:
                    print(f"  Exit: {exit_code}")
                if failure_cat:
                    print(f"  Category: {failure_cat}")
                if reason:
                    print(f"  Decision: {reason}")

            sys.exit(0 if status == "DONE" else 1)

        # Not completed yet — check current state for status messages
        try:
            conn = sqlite3.connect(str(dispatch_db))
            conn.row_factory = sqlite3.Row
            running_row = conn.execute(
                """SELECT * FROM dispatch_runs
                   WHERE bead_id = ? AND completed_at IS NULL
                   ORDER BY started_at DESC LIMIT 1""",
                (bead_id,),
            ).fetchone()
            conn.close()
        except sqlite3.Error:
            running_row = None

        if running_row and running_row["started_at"]:
            current_status = "Running..."
        else:
            current_status = "Waiting for dispatch..."

        if current_status != last_status:
            print(current_status, file=sys.stderr)
            last_status = current_status

        time.sleep(poll_interval)


def main():
    parser = argparse.ArgumentParser(
        prog="autonomy-graph",
        description="Autonomy Knowledge Graph CLI",
    )
    parser.add_argument("--db", type=Path, default=_get_db_path(), help="Database path")

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
    p.add_argument("--or", dest="or_mode", action="store_true", help="Join terms with OR instead of AND")
    p.add_argument("--json", action="store_true", help="Output as JSON array")
    p.set_defaults(func=cmd_search)

    # read
    p = sub.add_parser("read", help="Read full content of a source")
    p.add_argument("source", help="Source ID (or prefix) or title search term")
    p.add_argument("--first", action="store_true", help="Read first match if multiple")
    p.add_argument("--max-chars", type=int, default=0, help="Max chars per turn (0=unlimited)")
    p.add_argument("--json", action="store_true", help="Output as structured JSON (source + entries + edges)")
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

    # primer
    p = sub.add_parser("primer", help="Generate a dynamic context primer for a bead")
    p.add_argument("bead_id", help="Bead ID to generate primer for")
    p.add_argument("--no-provenance", action="store_true", help="Skip provenance turns")
    p.add_argument("--no-pitfalls", action="store_true", help="Skip pitfall notes")
    p.add_argument("--no-tools", action="store_true", help="Skip tool docs (ignored, kept for compat)")
    p.add_argument("--format", choices=["agent", "dashboard"], default="agent",
                   help="Output format: agent (with follow-on commands) or dashboard (human-friendly)")
    p.set_defaults(func=lambda args: print(_cmd_primer(args)))

    # bead (create with provenance)
    p = sub.add_parser("bead", help="Create a bead with provenance link to source conversation")
    p.add_argument("title", help="Bead title")
    p.add_argument("-p", "--priority", type=int, default=1, help="Priority 0-3 (default: 1)")
    p.add_argument("-d", "--desc", help="Description")
    p.add_argument("-t", "--type", default="task", help="Type: task, epic, bug (default: task)")
    p.add_argument("--source", help="Source ID to link as conceived_at")
    p.add_argument("--turns", help="Turn range in source (e.g. 286 or 338-344)")
    p.add_argument("--note", "-n", help="Context note for the provenance link")
    p.set_defaults(func=cmd_bead)

    # link
    p = sub.add_parser("link", help="Create provenance edge between bead and session turns")
    p.add_argument("bead", help="Bead ID (e.g. auto-ov3)")
    p.add_argument("source", help="Source ID or prefix")
    p.add_argument("--relation", "-r", default="informed_by",
                   help="Relation type: informed_by, implemented_by, conceived_at, discussed_at (default: informed_by)")
    p.add_argument("--turns", "-t", help="Turn range (e.g. 286 or 338-344)")
    p.add_argument("--note", "-n", help="Context note for this link")
    p.set_defaults(func=cmd_link)

    # attention
    p = sub.add_parser("attention", help="Show human input from sessions chronologically")
    p.add_argument("--session", help="Path to specific session JSONL (default: most recent)")
    p.add_argument("--last", type=int, help="Show only last N messages")
    p.add_argument("--search", "-s", help="Filter to messages containing this text")
    p.set_defaults(func=cmd_attention)

    # note
    p = sub.add_parser("note", help="Drop a searchable trail marker into the graph")
    p.add_argument("text", nargs="+", help="Note text")
    p.add_argument("--project", "-p", help="Project to tag with")
    p.add_argument("--tags", "-t", help="Comma-separated tags")
    p.add_argument("--author", help="Who wrote this (default: user)")
    p.set_defaults(func=cmd_note)

    # agent-runs
    p = sub.add_parser("agent-runs", help="Discover and ingest subagent traces")
    p.add_argument("--session", help="Filter to traces from a specific session ID")
    p.add_argument("--list", dest="list_only", action="store_true", help="List traces without ingesting")
    p.add_argument("--force", action="store_true", help="Re-ingest existing traces")
    p.set_defaults(func=cmd_agent_runs)

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

    # wait
    p = sub.add_parser("wait", help="Block until a dispatched bead completes")
    p.add_argument("bead_id", help="Bead ID (e.g. auto-mys.2.1)")
    p.add_argument("--timeout", type=int, default=600, help="Max wait seconds (default: 600)")
    p.set_defaults(func=cmd_wait)

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
