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
from .dispatch_cmd import cmd_dispatch_default, cmd_dispatch_runs, cmd_dispatch_status, cmd_dispatch_approve, cmd_dispatch_watch
from .api_client import is_api_mode, api_note, api_note_update, api_comment_add, api_comment_integrate, api_bead, api_link, api_sessions, api_set_label, api_attach, api_collab_list, api_collab_tag


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
        rtype = r["result_type"]
        project = r.get("project", "")
        proj_tag = f" [{project}]" if project else ""
        sid = r.get("source_id", "?")[:12]

        if rtype == "source":
            # Direct source match
            stype = r.get("source_type", "")
            platform = r.get("platform", "")
            created = (r.get("created_at") or "")[:10]
            print(f"  [S] {r.get('source_title', '?')[:60]} (src:{sid}){proj_tag}")
            detail_parts = [p for p in [stype, platform, created] if p]
            print(f"    {' | '.join(detail_parts)}")
            print()
        elif rtype == "edge":
            # Linked source via edge
            direction = r.get("direction", "→")
            relation = r.get("relation", "linked")
            title = r.get("source_title") or "?"
            turn = r.get("turn_number")
            turn_tag = f" t{turn}" if turn else ""
            print(f"  [{direction}] {title[:50]}{turn_tag} (src:{sid}){proj_tag}")
            print(f"    relation: {relation}")
            print()
        else:
            # Normal thought/derivation FTS result
            tag = rtype[0].upper()  # T or D
            source = r.get("source_title") or "?"
            turn = r.get("turn_number", "?")
            content = r["content"]
            if len(content) > width:
                content = content[:width] + "…"
            lines = content.replace("\n", "\n    ").rstrip()
            print(f"  [{tag}] {source[:50]} t{turn} (src:{sid}){proj_tag}")
            print(f"    {lines}")
            print()

    db.close()


def _in_container() -> bool:
    """Fast container detection — /.dockerenv exists in Docker containers."""
    return Path("/.dockerenv").exists()


def _mark_read(source_id: str):
    """Drop a marker file recording that this source was read in this container session."""
    if _in_container():
        marker_dir = Path.home() / ".graph" / "reads"
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / source_id).touch()


def _require_read(source_id_prefix: str, label: str):
    """Gate: refuse to proceed if the agent hasn't read the required note."""
    if not _in_container():
        return
    marker_dir = Path.home() / ".graph" / "reads"
    if not marker_dir.exists() or not any(marker_dir.glob(f"{source_id_prefix}*")):
        print(f"REQUIRED READING: {label}", file=sys.stderr)
        print(f"Run:  graph read {source_id_prefix}", file=sys.stderr)
        sys.exit(1)


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


def _resolve_current_source(db):
    """Find the graph source for the session we're running inside.

    Uses BD_ACTOR env var (e.g. 'terminal:auto-0322-153000') to get tmux name,
    then asks the dashboard API for graph_source_id (canonical source of truth).

    Returns source dict or None.
    """
    bd_actor = os.environ.get("BD_ACTOR")
    if not bd_actor or ":" not in bd_actor:
        return None
    tmux_name = bd_actor.split(":", 1)[1]

    # Ask the dashboard API (canonical source of truth)
    api_base = os.environ.get("GRAPH_API")
    if not api_base:
        return None
    import ssl
    import urllib.error
    import urllib.parse
    import urllib.request
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    url = f"{api_base}/api/session/{urllib.parse.quote(tmux_name)}"
    try:
        resp = urllib.request.urlopen(url, timeout=5, context=ctx)
        data = json.loads(resp.read())
        graph_source_id = data.get("graph_source_id")
        if not graph_source_id:
            print(f"warning: session {tmux_name} has no graph_source_id yet", file=sys.stderr)
            return None
        return db.get_source(graph_source_id)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as e:
        print(f"warning: auto-provenance failed for {tmux_name}: {e}", file=sys.stderr)
        return None


def _auto_provenance(db):
    """Auto-detect current session source and latest turn.

    Returns (source_dict, turn_number) or (None, None).
    Uses BD_ACTOR env var -> graph source lookup -> max turn query.
    Runs graph sessions --all first to ensure current session is ingested.
    """
    subprocess.run(["graph", "sessions", "--all"], capture_output=True, timeout=30)
    source = _resolve_current_source(db)
    if not source:
        return None, None
    turn = db.get_latest_turn(source["id"])
    return source, turn


def _resolve_source_for_link(db, source_arg):
    """Strict source resolution for bead/link commands.

    Returns (source_dict, error_message). One of them is always None.
    On success, prints the resolution path so mistakes are visible.
    """
    result = db.resolve_source_strict(source_arg)

    if result is None:
        return None, f"No source found matching '{source_arg}'"

    if isinstance(result, list):
        lines = [f"Multiple sources match '{source_arg}':"]
        for s in result[:10]:
            meta = json.loads(s["metadata"]) if s.get("metadata") else {}
            title = (s.get("title") or "?")[:60]
            tmux = meta.get("tmux_session", "")
            tmux_str = f"  tmux={tmux}" if tmux else ""
            lines.append(f"  {s['id'][:12]}  {title}{tmux_str}")
        lines.append("\nUse a longer prefix to disambiguate.")
        return None, "\n".join(lines)

    # Single match — print resolution
    title = (result.get("title") or "?")[:60]
    print(f"  Resolved: {source_arg} → {result['id'][:12]} \"{title}\"")
    return result, None


def cmd_read(args):
    """Read full content of a source by ID or title search."""
    db = GraphDB(args.db)
    import json as _json

    # Parse @N version suffix
    source_arg = args.source
    version_req = None
    if "@" in source_arg:
        source_arg, version_part = source_arg.rsplit("@", 1)
        if version_part == "":
            version_req = "list"
        else:
            try:
                version_req = int(version_part)
            except ValueError:
                print(f"Error: invalid version '{version_part}' (must be integer)", file=sys.stderr)
                db.close()
                return

    result = _resolve_source(db, source_arg, first=args.first)
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
    _mark_read(source["id"])

    # Track reads for collab-tagged notes
    meta = json.loads(source.get("metadata", "{}")) if isinstance(source.get("metadata"), str) else source.get("metadata", {})
    if "collab" in meta.get("tags", []):
        actor = os.environ.get("BD_ACTOR", "user")
        if not db.read_only:
            db.record_read(source["id"], actor)

    # Handle version requests for notes
    if version_req is not None:
        if version_req == "list":
            versions = db.list_note_versions(source["id"])
            if not versions:
                print(f"No version history for {source['id'][:12]} (note has not been updated)")
            else:
                print(f"Versions for {source['id'][:12]}:")
                for v in versions:
                    preview = v["content"][:60].replace("\n", " ")
                    if len(v["content"]) > 60:
                        preview += "…"
                    print(f"  v{v['version']}  {v['created_at'][:16]}  {preview}")
            db.close()
            return
        else:
            ver = db.get_note_version(source["id"], version_req)
            if not ver:
                print(f"Version {version_req} not found for {source['id'][:12]}", file=sys.stderr)
                db.close()
                return
            proj = f" [{source['project']}]" if source.get('project') else ""
            print(f"Source: {source['id'][:12]}  {source['type']}{proj}  (version {version_req})")
            print(f"Title:  {source.get('title', '?')}")
            print(f"Date:   {ver['created_at'][:10]}")
            print(f"{'─' * 72}")
            print(f"\n{ver['content']}")
            db.close()
            return

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
                "file_path": source.get("file_path") or None,
                "metadata": source.get("metadata"),
            },
            "entries": entry_list,
            "edges": edge_list,
        }
        if source.get("type") == "note":
            comments = db.get_comments(source["id"])
            output["comments"] = [dict(c) for c in comments]
            versions = db.list_note_versions(source["id"])
            output["version_count"] = len(versions) + 1 if versions else 1
        print(_json.dumps(output, default=str))
        db.close()
        return

    # Text output (existing behavior)
    proj = f" [{source['project']}]" if source.get('project') else ""
    print(f"Source: {source['id'][:12]}  {source['type']}{proj}")
    print(f"Title:  {source.get('title', '?')}")
    print(f"Date:   {source.get('created_at', '?')[:10]}")
    if source.get("file_path"):
        print(f"File:   {source['file_path']}")
    # Show author for notes when it's not the default "user"
    if source.get("type") == "note":
        meta = source.get("metadata") or {}
        if isinstance(meta, str):
            import json as _json2
            try:
                meta = _json2.loads(meta)
            except Exception:
                meta = {}
        author = meta.get("author", "")
        if author and author != "user":
            print(f"Author: {author}")
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

    # Append comments for note sources
    if source.get("type") == "note":
        include_integrated = getattr(args, 'all_comments', False)
        comments = db.get_comments(source["id"], include_integrated=include_integrated)
        if comments:
            print(f"\n{'─' * 72}")
            print(f"## Comments ({len(comments)})")
            for c in comments:
                status = " [integrated]" if c["integrated"] else ""
                print(f"\n**{c['actor']}** · {c['created_at'][:16]}{status}  (id:{c['id'][:12]})")
                print(c["content"])

    db.close()


def cmd_sources(args):
    """List sources with optional filters."""
    db = GraphDB(args.db)
    sources = db.list_sources(
        project=args.project, source_type=args.type, limit=args.limit,
        since=args.since, until=getattr(args, 'until', None), author=args.author,
    )

    if not sources:
        print("No sources found.")
        db.close()
        return

    for s in sources:
        proj = f" [{s['project']}]" if s.get('project') else ""
        date = (s.get("created_at") or "")[:10]
        print(f"  {s['id'][:12]}  {s['type']:10s}  {date}  {s.get('title', '?')[:55]}{proj}")
        if args.verbose:
            fp = s.get("file_path")
            print(f"                {fp if fp else '(no file)'}")

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


def _print_session_status():
    """Print compact status table of live sessions from dashboard.db."""
    import sqlite3
    import time as _time

    db_path = Path(__file__).parents[2] / "data" / "dashboard.db"
    if not db_path.exists():
        print("dashboard.db not found", file=sys.stderr)
        return
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM tmux_sessions WHERE is_live=1 ORDER BY last_activity DESC"
    ).fetchall()
    conn.close()
    if not rows:
        print("No live sessions")
        return

    now = _time.time()
    print(f"{'TMUX':<28} {'LABEL':<24} {'IDLE':>6} {'TURNS':>6} {'CTX':>6} {'LAST MESSAGE'}")
    print("\u2500" * 100)
    for r in rows:
        tmux = (r["tmux_name"] or "")[:27]
        label = (r["label"] or "")[:23]
        last_act = r["last_activity"] or r["created_at"]
        idle_s = int(now - last_act) if last_act else 0
        if idle_s < 60:
            idle = f"{idle_s}s"
        elif idle_s < 3600:
            idle = f"{idle_s // 60}m"
        elif idle_s < 86400:
            idle = f"{idle_s // 3600}h"
        else:
            idle = f"{idle_s // 86400}d"
        turns = str(r["entry_count"] or 0)
        ctx = r["context_tokens"] or 0
        ctx_str = f"{ctx // 1000}K" if ctx >= 1000 else str(ctx)
        msg = (r["last_message"] or "")[:40].replace("\n", " ")
        print(f"{tmux:<28} {label:<24} {idle:>6} {turns:>6} {ctx_str:>6} {msg}")


def cmd_sessions(args):
    """Ingest Claude Code sessions."""
    if args.status:
        _print_session_status()
        return
    if is_api_mode():
        api_sessions(args)
        return
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


def cmd_set_label(args):
    """Set the label for the current session."""
    import urllib.parse
    import urllib.request

    bd_actor = os.environ.get("BD_ACTOR")
    if not bd_actor or ":" not in bd_actor:
        print("Error: $BD_ACTOR not set. Cannot identify current session.", file=sys.stderr)
        print("This command must be run inside a dashboard-managed session.", file=sys.stderr)
        sys.exit(1)
    tmux_name = bd_actor.split(":", 1)[1]

    if is_api_mode():
        api_set_label(args)
        return

    label = " ".join(args.text)
    api_base = os.environ.get("GRAPH_API", "https://localhost:8080")
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    url = f"{api_base}/api/session/{urllib.parse.quote(tmux_name)}/label"
    req = urllib.request.Request(
        url,
        data=json.dumps({"label": label}).encode(),
        method="PUT",
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10, context=ctx)
        if resp.status == 200:
            print(f"  \u2713 Label set: {label}")
        else:
            print(f"  \u2717 Failed: HTTP {resp.status}", file=sys.stderr)
            sys.exit(1)
    except urllib.error.HTTPError as e:
        print(f"  \u2717 Failed: HTTP {e.code}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"  \u2717 Cannot reach dashboard: {e.reason}", file=sys.stderr)
        sys.exit(1)


def cmd_ingest_session(args):
    """Ingest a single JSONL session file and print its graph source ID.

    Used by the dashboard to link a session to the graph at discovery time.
    Idempotent: if the source already exists, returns the existing ID.
    Prints only the source ID to stdout (for subprocess capture).
    """
    db = GraphDB(args.db)
    file_path = Path(args.file).resolve()

    if not file_path.exists():
        print(f"error: file not found: {file_path}", file=sys.stderr)
        db.close()
        sys.exit(1)

    result = ingest_claude_code_session(db, file_path, project=args.project)
    source_id = result.get("source_id")
    db.close()

    if source_id:
        print(source_id)
    else:
        print(f"error: ingest returned no source_id: {result.get('reason', 'unknown')}", file=sys.stderr)
        sys.exit(1)


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
    if is_api_mode():
        # Auto-detect source + turn before API dispatch
        if not args.source and not args.turns:
            db = GraphDB(args.db)
            source, turn = _auto_provenance(db)
            db.close()
            if source and turn:
                args.source = source["id"]
                args.turns = str(turn)
                title = (source.get("title") or "?")[:60]
                print(f'  Auto-provenance: {source["id"][:12]} turn {turn} "{title}"')
        api_bead(args)
        return
    import subprocess
    db = GraphDB(args.db)
    from .models import Edge

    # First, refresh the graph to capture latest turns
    subprocess.run(["graph", "sessions", "--all"], capture_output=True, timeout=30)

    # Read description from stdin if -d -
    desc = args.desc
    if desc == "-":
        import sys as _sys
        desc = _sys.stdin.read().strip()

    # Build bd create command — always set readiness:idea as pipeline entry point
    cmd = ["bd", "create", args.title, "-p", str(args.priority), "-l", "readiness:idea"]
    if desc:
        cmd += ["-d", desc]
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

    # Resolve source for provenance link
    source = None
    if args.source:
        # Explicit --source: strict resolution
        source, err = _resolve_source_for_link(db, args.source)
        if err:
            print(err, file=sys.stderr)
            db.close()
            return
    elif args.turns:
        # --turns given without --source: auto-detect current session
        source = _resolve_current_source(db)
        if source:
            title = (source.get("title") or "?")[:60]
            print(f"  Auto-detected source: {source['id'][:12]} \"{title}\"")
        else:
            print("  Warning: --turns given but no --source and could not auto-detect current session",
                  file=sys.stderr)
    else:
        # No --source, no --turns: auto-detect both
        source, turn = _auto_provenance(db)
        if source and turn:
            args.turns = str(turn)
            title = (source.get("title") or "?")[:60]
            print(f'  Auto-provenance: {source["id"][:12]} turn {turn} "{title}"')

    if source and args.turns:
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
        # Echo linked turn content for verification
        turn_num = turn_range.get("from") if turn_range else None
        if turn_num is not None:
            content = db.conn.execute(
                "SELECT content FROM thoughts WHERE source_id = ? AND turn_number = ? LIMIT 1",
                (source["id"], turn_num),
            ).fetchone()
            if content:
                snippet = content["content"][:120].replace("\n", " ")
                print(f'  → "{snippet}..."')

    db.close()


def cmd_link(args):
    """Create an edge between two graph nodes (bead, source, or note)."""
    if is_api_mode():
        # Auto-detect turn before API dispatch
        if not args.turns:
            db = GraphDB(args.db)
            source, turn = _auto_provenance(db)
            db.close()
            if turn:
                args.turns = str(turn)
        api_link(args)
        return
    db = GraphDB(args.db)
    from .models import Edge

    # Auto-detect turn if not provided
    if not args.turns:
        source, turn = _auto_provenance(db)
        if turn:
            args.turns = str(turn)

    # Parse turn range
    turn_range = None
    if args.turns:
        parts = args.turns.split("-")
        if len(parts) == 2:
            turn_range = {"from": int(parts[0]), "to": int(parts[1])}
        elif len(parts) == 1:
            turn_range = {"from": int(parts[0]), "to": int(parts[0])}

    # Resolve the first argument (from-node)
    from_source, from_err = _resolve_source_for_link(db, args.bead)
    if from_source:
        resolved_from_id = from_source["id"]
        resolved_from_type = from_source.get("type", "source")
    elif args.bead.startswith("auto-"):
        # Bead ID — use as-is
        resolved_from_id = args.bead
        resolved_from_type = "bead"
    else:
        print(f"Cannot resolve '{args.bead}' as a source or bead ID. "
              f"Use 'graph search' to find the right ID.", file=sys.stderr)
        db.close()
        sys.exit(1)

    # Strict source resolution for the second argument (target)
    source, err = _resolve_source_for_link(db, args.source)
    if err:
        print(err, file=sys.stderr)
        db.close()
        sys.exit(1)

    metadata = {}
    if turn_range:
        metadata["turns"] = turn_range
    if args.note:
        metadata["note"] = args.note

    db.insert_edge(Edge(
        source_id=resolved_from_id,
        source_type=resolved_from_type,
        target_id=source["id"],
        target_type="source",
        relation=args.relation,
        metadata=metadata,
    ))
    db.commit()

    turns_str = f" turns {args.turns}" if args.turns else ""
    from_label = resolved_from_id if resolved_from_type == "bead" else resolved_from_id[:12]
    print(f"  ✓ {from_label} —[{args.relation}]→ {source['id'][:12]}{turns_str}")
    # Echo linked turn content for verification
    if turn_range:
        turn_num = turn_range.get("from")
        if turn_num is not None:
            content = db.conn.execute(
                "SELECT content FROM thoughts WHERE source_id = ? AND turn_number = ? LIMIT 1",
                (source["id"], turn_num),
            ).fetchone()
            if content:
                snippet = content["content"][:120].replace("\n", " ")
                print(f'  → "{snippet}..."')
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


def _age_str(created_at: str) -> str:
    """Format a created_at timestamp as a human-friendly age string."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        days = delta.days
        if days > 0:
            return f"{days}d ago"
        hours = delta.seconds // 3600
        if hours > 0:
            return f"{hours}h ago"
        return "just now"
    except Exception:
        return created_at[:10]


def cmd_collab_list(args):
    """List collaborative reference notes ranked by activity."""
    if is_api_mode():
        api_collab_list(args)
        return
    db = GraphDB(args.db)
    sources = db.list_collab_sources(limit=args.limit)
    db.close()
    if not sources:
        print("No collab notes found. Tag notes with: graph note --tags collab")
        return
    # Header
    print(f"{'ID':14s} {'Title':50s} {'Comments':>8s} {'Reads':>6s} {'Age'}")
    print(f"{'─' * 14} {'─' * 50} {'─' * 8} {'─' * 6} {'─' * 10}")
    for s in sources:
        sid = s["id"][:12]
        title = (s.get("title") or "?")[:50]
        comments = str(s.get("comment_count", 0))
        reads = str(s.get("read_count", 0))
        age = _age_str(s.get("created_at", ""))
        print(f"{sid:14s} {title:50s} {comments:>8s} {reads:>6s} {age}")
    print(f"\n  {len(sources)} collab notes")


def cmd_collab_tag(args):
    """Add the 'collab' tag to an existing note."""
    if is_api_mode():
        api_collab_tag(args)
        return
    db = GraphDB(args.db)
    source = db.get_source(args.source_id)
    if not source:
        print(f"No source found matching '{args.source_id}'", file=sys.stderr)
        db.close()
        sys.exit(1)
    if isinstance(source, list):
        print(f"Multiple sources match '{args.source_id}' — use a longer prefix", file=sys.stderr)
        db.close()
        sys.exit(1)
    added = db.add_source_tag(source["id"], "collab")
    db.close()
    title = (source.get("title") or "?")[:60]
    if added:
        print(f"  \u2713 Tagged {source['id'][:12]} \"{title}\" as collab")
    else:
        print(f"  Already tagged: {source['id'][:12]} \"{title}\"")


def _parse_duration(s: str) -> float:
    """Parse a duration string like '1h', '30m', '2d', '1w' to seconds."""
    import re
    m = re.match(r'^(\d+)\s*([smhdw])$', s.strip())
    if not m:
        raise ValueError(f"Invalid duration: {s!r}. Use e.g. 1h, 30m, 2d, 1w")
    val, unit = int(m.group(1)), m.group(2)
    multipliers = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}
    return val * multipliers[unit]


def cmd_notes(args):
    """List notes, optionally filtered by recency."""
    db = GraphDB(args.db)
    try:
        since_iso = None
        if args.since:
            try:
                secs = _parse_duration(args.since)
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)
            from datetime import datetime, timezone, timedelta
            since_dt = datetime.now(timezone.utc) - timedelta(seconds=secs)
            since_iso = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        tags = [t.strip() for t in args.tags.split(",")] if args.tags else None

        sources = db.list_sources(
            source_type="note",
            project=args.project,
            since=since_iso,
            tags=tags,
            limit=args.limit,
        )
        if not sources:
            print("No notes found")
            return

        for s in sources:
            sid = s["id"][:11]
            date = (s.get("created_at") or "")[:10]
            title = (s.get("title") or "")[:60]
            project = s.get("project") or ""
            tags_str = ""
            meta = s.get("metadata")
            if meta and isinstance(meta, str):
                import json
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            if isinstance(meta, dict) and meta.get("tags"):
                tags_str = ",".join(meta["tags"])
            print(f"  {sid}  {date}  [{project}]  {title}")
            if tags_str:
                print(f"           tags: {tags_str}")
    finally:
        db.close()


def cmd_note_router(args):
    """Route 'graph note ...' to create or update."""
    if args.text and args.text[0] == "update":
        # graph note update <src_id> [text...]
        if len(args.text) < 2:
            print("Error: usage: graph note update <source_id> <new text...>", file=sys.stderr)
            sys.exit(1)
        args.source = args.text[1]
        args.text = args.text[2:]
        if is_api_mode():
            api_note_update(args)
        else:
            cmd_note_update(args)
    else:
        if is_api_mode():
            api_note(args)
        else:
            cmd_note(args)


def _check_single_line_content(content: str, force: bool) -> None:
    """Reject unformatted single-line content over 120 chars unless --force is used."""
    if '\n' not in content and len(content) > 120 and not force:
        print("\u2717 Note content is %d chars on a single line \u2014 this will be unreadable." % len(content), file=sys.stderr)
        print("  Write formatted markdown to a temp file, then pipe it:", file=sys.stderr)
        print("", file=sys.stderr)
        print("    cat > /tmp/note.md << 'EOF'", file=sys.stderr)
        print("    # Title", file=sys.stderr)
        print("    ", file=sys.stderr)
        print("    Your content with proper structure...", file=sys.stderr)
        print("    EOF", file=sys.stderr)
        print("    graph note -c - --tags tag1,tag2 < /tmp/note.md", file=sys.stderr)
        print("", file=sys.stderr)
        print("  To force save anyway: graph note --force \"...\"", file=sys.stderr)
        sys.exit(1)


def cmd_note(args):
    """Drop a searchable trail marker into the graph."""
    if getattr(args, 'content_stdin', None) == "-":
        text = sys.stdin.read().strip()
    elif args.text:
        text = " ".join(args.text)
    else:
        print("Error: note text required", file=sys.stderr)
        sys.exit(1)
    _check_single_line_content(text, getattr(args, 'force', False))
    db = GraphDB(args.db)
    tags = args.tags.split(",") if args.tags else []

    from .models import Source, Thought, new_id, now_iso
    source_key = f"note:{new_id()}"
    source = Source(
        type="note",
        platform="local",
        project=args.project or _get_scope(),
        title=text[:80],
        file_path=source_key,
        metadata={"tags": tags, "author": args.author or os.environ.get("BD_ACTOR", "user")},
    )
    db.insert_source(source)

    # Handle --attach: store files and substitute placeholders
    attach_paths = getattr(args, "attach", None) or []
    if attach_paths:
        att_ids = []
        for fp in attach_paths:
            att = _store_attachment(db, fp, source_id=source.id)
            if att is None:
                db.close()
                sys.exit(1)
            att_ids.append(att.id)
            print(f"  ✓ Attached {att.filename} ({att.id[:12]})")
        # Substitute positional placeholders {1}, {2}, ...
        for i, att_id in enumerate(att_ids, 1):
            text = text.replace('{' + str(i) + '}', f'graph://{att_id[:12]}')

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
    _lines = text.count("\n") + (1 if text else 0)
    print(f"  ✓ Note saved (src:{source.id[:12]}) — {_lines} lines, {len(text)} chars")
    db.close()


def cmd_comment_router(args):
    """Route 'graph comment ...' to add or integrate."""
    positionals = args.args or []
    if positionals and positionals[0] == "integrate":
        if len(positionals) < 2:
            print("Error: usage: graph comment integrate <comment_id>", file=sys.stderr)
            sys.exit(1)
        args.comment_id = positionals[1]
        if is_api_mode():
            api_comment_integrate(args)
        else:
            cmd_comment_integrate(args)
    else:
        # add mode: first arg is source, rest is text
        args.source = positionals[0] if positionals else None
        args.text = positionals[1:] if len(positionals) > 1 else []
        if is_api_mode():
            api_comment_add(args)
        else:
            cmd_comment_add(args)


def cmd_comment_add(args):
    """Add a comment to a note source."""
    db = GraphDB(args.db)

    # Resolve content
    if getattr(args, 'content_stdin', None) == "-":
        content = sys.stdin.read().strip()
    else:
        content = " ".join(args.text) if args.text else ""

    if not content:
        print("Error: no comment content provided", file=sys.stderr)
        sys.exit(1)

    if not args.source:
        print("Error: source ID required", file=sys.stderr)
        sys.exit(1)

    result = _resolve_source(db, args.source, first=True)
    if result is None:
        print(f"No source found matching '{args.source}'", file=sys.stderr)
        db.close()
        sys.exit(1)
    source = result if isinstance(result, dict) else result[0]

    if source.get("type") != "note":
        print(f"Error: comments are only supported on notes (source is type '{source.get('type')}')", file=sys.stderr)
        db.close()
        sys.exit(1)

    comment = db.insert_comment(source["id"], content, actor=args.actor)
    print(f"  ✓ Comment added (id:{comment['id'][:12]}) on {source['id'][:12]}")
    db.close()


def cmd_comment_integrate(args):
    """Mark a comment as integrated into the note body."""
    db = GraphDB(args.db)

    # Check if already integrated
    row = db.conn.execute("SELECT * FROM note_comments WHERE id = ? OR id LIKE ?",
                          (args.comment_id, f"{args.comment_id}%")).fetchone()
    if not row:
        print(f"Error: comment not found: {args.comment_id}", file=sys.stderr)
        db.close()
        sys.exit(1)

    comment = dict(row)
    if comment["integrated"]:
        print(f"  Comment {comment['id'][:12]} is already integrated")
        db.close()
        return

    db.integrate_comment(comment["id"])
    print(f"  ✓ Comment {comment['id'][:12]} marked as integrated")
    db.close()


def cmd_note_update(args):
    """Update a note with versioned history."""
    _require_read("843a8137", "Agents must read the Note Revision Protocol before updating notes.\n  See: graph://843a8137-3c7")
    db = GraphDB(args.db)

    # Resolve content
    if getattr(args, 'content_stdin', None) == "-":
        new_content = sys.stdin.read().strip()
    else:
        new_content = " ".join(args.text) if args.text else ""

    if not new_content:
        print("Error: no content provided", file=sys.stderr)
        sys.exit(1)
    _check_single_line_content(new_content, getattr(args, 'force', False))

    result = _resolve_source(db, args.source, first=True)
    if result is None:
        print(f"No source found matching '{args.source}'", file=sys.stderr)
        db.close()
        sys.exit(1)
    source = result if isinstance(result, dict) else result[0]

    if source.get("type") != "note":
        print(f"Error: can only update notes (source is type '{source.get('type')}')", file=sys.stderr)
        db.close()
        sys.exit(1)

    source_id = source["id"]

    # Handle --attach: store files and substitute placeholders
    attach_paths = getattr(args, "attach", None) or []
    if attach_paths:
        att_ids = []
        for fp in attach_paths:
            att = _store_attachment(db, fp, source_id=source_id)
            if att is None:
                db.close()
                sys.exit(1)
            att_ids.append(att.id)
            print(f"  ✓ Attached {att.filename} ({att.id[:12]})")
        for i, att_id in enumerate(att_ids, 1):
            new_content = new_content.replace('{' + str(i) + '}', f'graph://{att_id[:12]}')

    # Get current thought (turn 1)
    thoughts = db.get_thoughts_by_source(source_id)
    if not thoughts:
        print(f"Error: no thought found for source {source_id[:12]}", file=sys.stderr)
        db.close()
        sys.exit(1)
    thought = thoughts[0]

    # Determine versioning
    current_max = db.get_max_note_version(source_id)
    if current_max == 0:
        # Backfill version 1 with current content
        db.insert_note_version(source_id, 1, thought["content"])
        next_version = 2
    else:
        next_version = current_max + 1

    # Insert new version
    db.insert_note_version(source_id, next_version, new_content)

    # Update the live thought content (FTS trigger handles re-indexing)
    db.update_thought_content(thought["id"], new_content)

    # Update source title
    db.conn.execute("UPDATE sources SET title = ? WHERE id = ?", (new_content[:80], source_id))

    # Re-extract entities for the new content
    from .ingest import extract_entities
    for name, etype in extract_entities(new_content):
        eid = db.upsert_entity(name, etype)
        db.add_mention(eid, thought["id"], "thought")

    db.commit()
    _lines = new_content.count("\n") + (1 if new_content else 0)
    print(f"  ✓ Note updated to version {next_version} (src:{source_id[:12]}) — {_lines} lines, {len(new_content)} chars")

    # Integrate specified comments
    for cid in (args.integrate_ids or []):
        row = db.conn.execute(
            "SELECT id FROM note_comments WHERE (id = ? OR id LIKE ?) AND source_id = ?",
            (cid, f"{cid}%", source_id),
        ).fetchone()
        if row:
            db.integrate_comment(row["id"])
            print(f"  ✓ Comment {row['id'][:12]} integrated")
        else:
            print(f"  ⚠ Comment {cid} not found on this note", file=sys.stderr)

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


def cmd_ui_exp(args):
    """Create a UI experiment from HTML files and watch for changes."""
    import time as _time
    import urllib.request
    import ssl

    sys.stdout.reconfigure(line_buffering=True)

    dir_path = Path(args.dir)
    if not dir_path.is_dir():
        print(f"Error: {dir_path} is not a directory", file=sys.stderr)
        sys.exit(1)

    api_base = args.api.rstrip("/")
    # Skip TLS verification for self-signed certs
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    def _post(endpoint, data):
        body = json.dumps(data).encode()
        req = urllib.request.Request(
            f"{api_base}{endpoint}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, context=ctx)
        return json.loads(resp.read())

    def _scan_variants():
        variants = {}
        for f in sorted(dir_path.glob("*.html")):
            variants[f.stem] = f.read_text()
        return variants

    def _file_states():
        states = {}
        for f in sorted(dir_path.glob("*.html")):
            states[f.stem] = f.stat().st_mtime
        return states

    # Load optional fixture
    fixture = None
    if args.fixture:
        fixture = Path(args.fixture).read_text()

    # Initial scan
    variants = _scan_variants()
    if not variants:
        print(f"No .html files found in {dir_path}", file=sys.stderr)
        sys.exit(1)

    # Create experiment
    exp_data = {
        "title": args.title,
        "variants": [{"id": vid, "html": html} for vid, html in variants.items()],
    }
    if args.series:
        exp_data["series_id"] = args.series
    if fixture:
        exp_data["fixture"] = fixture

    result = _post("/api/experiments", exp_data)
    exp_id = result["id"]

    # If no series was specified, use this experiment's ID as the series
    series_id = args.series or exp_id

    print(f"  Created experiment: {exp_id}")
    print(f"  Series: {series_id}")
    print(f"  Variants: {', '.join(variants.keys())}")
    print(f"  URL: {api_base}/experiments/{exp_id}")
    print(f"  Screenshot: {dir_path}/screenshot.png (auto-updated from browser)")
    print(f"\n  Watching {dir_path}/ for changes... (Ctrl+C to stop)\n")

    latest_exp_id = exp_id
    screenshot_link = dir_path / "screenshot.png"
    _screenshot_linked = False  # True once symlink points to current experiment

    def _link_screenshot():
        """Symlink screenshot.png to the latest experiment's screenshot."""
        nonlocal _screenshot_linked
        if _screenshot_linked:
            return
        src = Path(f"data/experiments/{latest_exp_id}/screenshot.png")
        if src.exists():
            abs_src = src.resolve()
            if screenshot_link.is_symlink() or screenshot_link.exists():
                screenshot_link.unlink()
            screenshot_link.symlink_to(abs_src)
            print(f"  📸 screenshot.png → {abs_src}")
            _screenshot_linked = True

    prev_states = _file_states()

    try:
        while True:
            _time.sleep(1)

            # Check for screenshot until we have one for the current experiment
            _link_screenshot()

            curr_states = _file_states()

            if curr_states == prev_states:
                continue

            # Detect changes
            added = set(curr_states) - set(prev_states)
            removed = set(prev_states) - set(curr_states)
            changed = {k for k in set(curr_states) & set(prev_states)
                       if curr_states[k] != prev_states[k]}

            if not added and not removed and not changed:
                continue

            changes = []
            if added:
                changes.append(f"+{', '.join(added)}")
            if removed:
                changes.append(f"-{', '.join(removed)}")
            if changed:
                changes.append(f"~{', '.join(changed)}")
            print(f"  [{_time.strftime('%H:%M:%S')}] {' '.join(changes)}")

            # Post new experiment in the series with current file state
            variants = _scan_variants()
            if not variants:
                print("  WARNING: no variants left, skipping")
                prev_states = curr_states
                continue

            exp_data = {
                "title": args.title,
                "series_id": series_id,
                "variants": [{"id": vid, "html": html} for vid, html in variants.items()],
            }
            if fixture:
                exp_data["fixture"] = fixture

            try:
                result = _post("/api/experiments", exp_data)
                new_id = result["id"]
                latest_exp_id = new_id
                _screenshot_linked = False  # New experiment, need fresh symlink
                print(f"  → {new_id} ({len(variants)} variants)")
            except Exception as e:
                print(f"  ERROR: {e}", file=sys.stderr)

            prev_states = curr_states

    except KeyboardInterrupt:
        print(f"\n  Stopped watching. Series: {series_id}")


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
            conn = sqlite3.connect(f"file:{dispatch_db}?immutable=1", uri=True)
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
                commit = (run.get("commit_hash") or "")[:7] or "none"
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
            conn = sqlite3.connect(f"file:{dispatch_db}?immutable=1", uri=True)
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


def _store_attachment(db, file_path_str, source_id=None, turn_number=None):
    """Hash, dedup, store a file and insert an attachment record.

    Returns the Attachment object (new or existing).
    Shared by cmd_attach() and cmd_note() --attach handling.
    """
    import hashlib
    import mimetypes
    import shutil
    from .models import Attachment

    file_path = Path(file_path_str)
    if not file_path.is_file():
        print(f"Error: {file_path} not found or not a file", file=sys.stderr)
        return None

    file_data = file_path.read_bytes()
    file_hash = hashlib.sha256(file_data).hexdigest()
    size_bytes = len(file_data)
    filename = file_path.name

    existing = db.get_attachment_by_hash(file_hash)
    if existing:
        # Update source_id if provided and not already set
        if source_id and not existing.get("source_id"):
            db.conn.execute("UPDATE attachments SET source_id = ? WHERE id = ?", (source_id, existing["id"]))
            db.conn.commit()
        return Attachment(
            id=existing["id"], hash=file_hash, filename=filename,
            mime_type=existing.get("mime_type"), size_bytes=size_bytes,
            file_path=existing["file_path"], source_id=source_id or existing.get("source_id"),
        )

    mime_type, _ = mimetypes.guess_type(filename)
    ext = file_path.suffix or ""
    store_dir = db.db_path.parent / "attachments" / file_hash[:2]
    store_path = store_dir / f"{file_hash}{ext}"
    store_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(file_path), str(store_path))

    att = Attachment(
        hash=file_hash,
        filename=filename,
        mime_type=mime_type,
        size_bytes=size_bytes,
        file_path=str(store_path),
        source_id=source_id,
        turn_number=int(turn_number) if turn_number else None,
    )
    db.insert_attachment(att)
    return att


def cmd_attach(args):
    """Attach a file to the graph with hash-based dedup."""
    if is_api_mode():
        api_attach(args)
        return

    db = GraphDB(args.db)

    # Resolve provenance
    source_id = getattr(args, "source", None)
    turn_number = getattr(args, "turn", None)

    if not source_id and not turn_number:
        auto_src, auto_turn = _auto_provenance(db)
        if auto_src:
            source_id = auto_src["id"]
            turn_number = auto_turn

    att = _store_attachment(db, args.file_path, source_id=source_id,
                            turn_number=turn_number)
    if att is None:
        db.close()
        sys.exit(1)

    src_label = f" src:{source_id[:12]}" if source_id else ""
    print(f"  ✓ Attached {att.filename} ({att.id[:12]}{src_label}) — {att.size_bytes} bytes, {att.mime_type or 'unknown'}")
    db.close()


def cmd_attachment(args):
    """Show metadata for a single attachment."""
    db = GraphDB(args.db)
    att = db.get_attachment(args.id)
    if not att:
        print(f"No attachment found matching '{args.id}'", file=sys.stderr)
        db.close()
        sys.exit(1)

    print(f"  id:          {att['id']}")
    print(f"  filename:    {att['filename']}")
    print(f"  mime_type:   {att['mime_type'] or 'unknown'}")
    print(f"  size_bytes:  {att['size_bytes']}")
    print(f"  hash:        {att['hash']}")
    print(f"  file_path:   {att['file_path']}")
    print(f"  source_id:   {att['source_id'] or '—'}")
    print(f"  turn:        {att['turn_number'] if att['turn_number'] is not None else '—'}")
    print(f"  created_at:  {att['created_at']}")
    meta = att.get("metadata", "{}")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            meta = {}
    if meta:
        print(f"  metadata:    {json.dumps(meta)}")
    db.close()


def cmd_attachments(args):
    """List attachments, optionally filtered by source."""
    db = GraphDB(args.db)
    source_id = getattr(args, "source_id", None)
    atts = db.list_attachments(source_id=source_id, limit=getattr(args, "limit", 50))
    if not atts:
        print("  No attachments found.")
        db.close()
        return

    for att in atts:
        mime = att.get("mime_type") or "unknown"
        print(f"  {att['id'][:12]}  {mime:20s}  {att['size_bytes']:>8}  {att['filename']}")
    db.close()


def _parse_duration(s: str) -> float:
    """Parse a human duration string (e.g. '1h', '30m', '2d') to seconds."""
    import re
    m = re.fullmatch(r"(\d+)\s*([smhd])", s.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"Invalid duration: {s!r} (use e.g. 1h, 30m, 2d)")
    val, unit = int(m.group(1)), m.group(2)
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return val * mult[unit]


def cmd_crosstalk(args):
    """Display recent CrossTalk messages."""
    from datetime import datetime

    auth_db_path = Path(__file__).resolve().parents[1] / "data" / "auth.db"
    if not auth_db_path.exists():
        print("auth.db not found", file=sys.stderr)
        return

    from tools.dashboard.dao import auth_db
    if auth_db._conn is None:
        auth_db.init_db(auth_db_path)

    since_epoch = None
    if args.since:
        secs = _parse_duration(args.since)
        since_epoch = time.time() - secs

    messages = auth_db.get_messages(
        limit=args.limit,
        since=since_epoch,
        session=args.session,
    )

    if not messages:
        print("No CrossTalk messages found")
        return

    for msg in reversed(messages):
        ts = datetime.fromtimestamp(msg["timestamp"]).strftime("%H:%M:%S")
        sender = msg["sender_label"] or msg["sender_session"]
        target = msg["target_session"]
        text = (msg["message"] or "")[:120].replace("\n", " ")
        delivered = "✓" if msg["delivered"] else "·"
        print(f"  {ts} {delivered} {sender} → {target}")
        print(f"         {text}")
        if len(msg["message"] or "") > 120:
            print(f"         ... ({len(msg['message'])} chars)")


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
    p.add_argument("source", help="Source ID (or prefix) or title search term. Use @N for version, @ to list versions")
    p.add_argument("--first", action="store_true", help="Read first match if multiple")
    p.add_argument("--max-chars", type=int, default=0, help="Max chars per turn (0=unlimited)")
    p.add_argument("--json", action="store_true", help="Output as structured JSON (source + entries + edges)")
    p.add_argument("--all-comments", action="store_true", help="Include integrated comments")
    p.set_defaults(func=cmd_read)

    # sources
    p = sub.add_parser("sources", help="List sources")
    p.add_argument("--project", "-p", help="Filter by project")
    p.add_argument("--type", "-t", help="Filter by type (session, status, git-log, etc.)")
    p.add_argument("--verbose", "-v", action="store_true", help="Show file paths under each source")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    p.add_argument("--since", help="Filter to sources created on or after this timestamp (ISO 8601)")
    p.add_argument("--until", help="Filter to sources created on or before this timestamp (ISO 8601)")
    p.add_argument("--author", help="Filter by metadata author (e.g. terminal:auto-t3, user)")
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

    # set-label
    p = sub.add_parser("set-label", help="Set a working title for the current session")
    p.add_argument("text", nargs="+", help="Label text")
    p.set_defaults(func=cmd_set_label)

    # sessions
    p = sub.add_parser("sessions", help="Ingest Claude Code sessions")
    p.add_argument("--all", action="store_true", help="Ingest all projects, not just current")
    p.add_argument("--project", help="Specific project path")
    p.add_argument("--force", action="store_true", help="Re-ingest existing sessions")
    p.add_argument("--status", action="store_true", help="Show live session status table from dashboard.db")
    p.set_defaults(func=cmd_sessions)

    # ingest-session
    p = sub.add_parser("ingest-session", help="Ingest a single JSONL session file and print its graph source ID")
    p.add_argument("file", help="Path to JSONL session file")
    p.add_argument("--project", help="Project name (auto-detected from path if omitted)")
    p.set_defaults(func=cmd_ingest_session)

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
    p = sub.add_parser("link", help="Create edge between two graph nodes (bead, source, or note)")
    p.add_argument("bead", help="Source node: bead ID (auto-xxx), source/note ID, or prefix")
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

    # note (handles both create and update)
    p_note = sub.add_parser("note", help="Create or update trail marker notes")
    p_note.add_argument("text", nargs="*", help="Note text, or 'update <src_id> <new text>'")
    p_note.add_argument("-c", dest="content_stdin", nargs="?", const="-", default=None, help="Read content from stdin")
    p_note.add_argument("--project", "-p", help="Project to tag with")
    p_note.add_argument("--tags", "-t", help="Comma-separated tags")
    p_note.add_argument("--author", help="Who wrote this (default: user)")
    p_note.add_argument("--force", action="store_true", help="Bypass single-line length check")
    p_note.add_argument("--integrate", dest="integrate_ids", action="append", default=[], help="Comment ID to mark as integrated (repeatable)")
    p_note.add_argument("--attach", action="append", default=[], help="Attach file to note (repeatable). Use {1}, {2} in text for inline placement. For images use markdown syntax: ![alt]({1}). Unplaced attachments appear as downloads")
    p_note.set_defaults(func=cmd_note_router)

    # notes (list notes with optional recency filter)
    p_notes = sub.add_parser("notes", help="List notes")
    p_notes.add_argument("--since", help="Duration filter, e.g. 1h, 30m, 2d, 1w")
    p_notes.add_argument("--project", help="Filter by project")
    p_notes.add_argument("--tags", help="Filter by tag (comma-separated)")
    p_notes.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")
    p_notes.set_defaults(func=cmd_notes)

    # comment (handles both add and integrate)
    p_comment = sub.add_parser("comment", help="Add or manage comments on notes")
    p_comment.add_argument("args", nargs="*", help="<source_id> <text...> or 'integrate <comment_id>'")
    p_comment.add_argument("-c", dest="content_stdin", nargs="?", const="-", default=None, help="Read content from stdin")
    p_comment.add_argument("--actor", default="user", help="Who is commenting")
    p_comment.set_defaults(func=cmd_comment_router)

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

    p = sub.add_parser("ui-exp", help="Create and live-watch a UI experiment from HTML files")
    p.add_argument("title", help="Experiment title")
    p.add_argument("dir", help="Directory of .html variant files")
    p.add_argument("--series", help="Existing series ID to append to")
    p.add_argument("--fixture", help="Path to fixture JSON file")
    p.add_argument("--api", default="https://localhost:8080", help="Dashboard API base URL")
    p.set_defaults(func=cmd_ui_exp)

    # dispatch
    p_dispatch = sub.add_parser("dispatch", help="Show dispatch state (running/queued/history)")
    p_dispatch.add_argument("--json", action="store_true", help="Output as JSON")
    p_dispatch.set_defaults(func=cmd_dispatch_default)
    dispatch_sub = p_dispatch.add_subparsers(dest="dispatch_subcmd")

    p_runs = dispatch_sub.add_parser("runs", help="Recent run history")
    p_runs.add_argument("--running", action="store_true", help="Only active runs")
    p_runs.add_argument("--failed", action="store_true", help="Only failures")
    p_runs.add_argument("--completed", action="store_true", help="Only completed (DONE) runs")
    p_runs.add_argument("--primer", action="store_true", help="Rich per-run output with scores, merge state, etc.")
    p_runs.add_argument("--limit", type=int, default=20, help="Max results (default 20)")
    p_runs.add_argument("--json", action="store_true", help="Output as JSON")
    p_runs.set_defaults(func=cmd_dispatch_runs)

    p_dstatus = dispatch_sub.add_parser("status", help="Compact one-liner summary")
    p_dstatus.add_argument("--json", action="store_true", help="Output as JSON")
    p_dstatus.set_defaults(func=cmd_dispatch_status)

    p_approve = dispatch_sub.add_parser("approve", help="Approve bead(s) for dispatch")
    p_approve.add_argument("bead_ids", nargs="+", help="One or more bead IDs")
    p_approve.set_defaults(func=cmd_dispatch_approve)

    p_watch = dispatch_sub.add_parser("watch", help="Block until next dispatch completes")
    p_watch.add_argument("--timeout", type=int, default=600, help="Timeout in seconds (default: 600)")
    p_watch.set_defaults(func=cmd_dispatch_watch)

    # attach
    p = sub.add_parser("attach", help="Attach a file to the graph with hash-based dedup")
    p.add_argument("file_path", help="Path to file to attach")
    p.add_argument("--source", help="Source ID to link as provenance")
    p.add_argument("--turn", type=int, help="Turn number in source conversation")
    p.set_defaults(func=cmd_attach)

    # attachment (show single)
    p = sub.add_parser("attachment", help="Show metadata for an attachment")
    p.add_argument("id", help="Attachment ID or prefix")
    p.set_defaults(func=cmd_attachment)

    # attachments (list)
    p = sub.add_parser("attachments", help="List attachments")
    p.add_argument("source_id", nargs="?", help="Filter by source ID")
    p.add_argument("--limit", type=int, default=50, help="Max results (default 50)")
    p.set_defaults(func=cmd_attachments)

    # collab
    p_collab = sub.add_parser("collab", help="Discover collaborative reference notes")
    p_collab.add_argument("--limit", type=int, default=50)
    p_collab.set_defaults(func=cmd_collab_list)
    collab_sub = p_collab.add_subparsers(dest="collab_subcmd")
    p_collab_tag = collab_sub.add_parser("tag", help="Add collab tag to an existing note")
    p_collab_tag.add_argument("source_id", help="Source ID or prefix")
    p_collab_tag.set_defaults(func=cmd_collab_tag)

    # crosstalk
    p = sub.add_parser("crosstalk", help="View CrossTalk message log")
    p.add_argument("--session", help="Filter by sender or target session")
    p.add_argument("--since", help="Duration filter, e.g. 1h, 30m, 2d")
    p.add_argument("--limit", type=int, default=30, help="Max messages (default: 30)")
    p.set_defaults(func=cmd_crosstalk)

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
