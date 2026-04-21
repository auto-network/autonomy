"""Ingestion pipeline for the Autonomy Knowledge Graph.

Parses markdown conversation files, musings, and Claude Code sessions
into structured graph objects.
"""

from __future__ import annotations
import json
import re
import subprocess
from pathlib import Path

from .models import Source, Thought, Derivation, Entity, Edge, now_iso
from .db import GraphDB


# ── Frontmatter Parser ───────────────────────────────────────

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from markdown body."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            value = value.strip().strip('"').strip("'")
            if value.isdigit():
                value = int(value)
            meta[key.strip()] = value
    return meta, parts[2]


# ── Conversation Parser ─────────────────────────────────────

TURN_PATTERN = re.compile(
    r"^## Turn (\d+)\s*—\s*(USER|ASSISTANT)\s*$",
    re.MULTILINE,
)
MESSAGE_ID_PATTERN = re.compile(r"<!--\s*message_id:\s*(\S+)\s*-->")
THINKING_PATTERN = re.compile(r"^>\s*\*\*Thinking:\*\*.*$", re.MULTILINE)


def parse_conversation(text: str) -> tuple[dict, list[dict]]:
    """Parse a conversation markdown file into metadata and turns."""
    meta, body = parse_frontmatter(text)

    turns = []
    matches = list(TURN_PATTERN.finditer(body))

    for i, match in enumerate(matches):
        turn_num = int(match.group(1))
        role = match.group(2).lower()

        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        content = body[start:end].strip()

        # Extract message_id if present
        msg_match = MESSAGE_ID_PATTERN.search(content)
        message_id = msg_match.group(1) if msg_match else None
        if msg_match:
            content = content[:msg_match.start()] + content[msg_match.end():]
            content = content.strip()

        # Strip thinking annotations from assistant turns
        if role == "assistant":
            content = THINKING_PATTERN.sub("", content).strip()

        turns.append({
            "turn_number": turn_num,
            "role": role,
            "content": content,
            "message_id": message_id,
        })

    return meta, turns


# ── Musings Parser ───────────────────────────────────────────

def parse_musing(text: str, file_path: str) -> tuple[dict, list[str]]:
    """Parse a musing file into sections split on blank lines."""
    meta, body = parse_frontmatter(text)

    # Split on triple+ newlines (section breaks used in musings)
    sections = re.split(r"\n{3,}", body.strip())
    sections = [s.strip() for s in sections if s.strip()]

    if not meta.get("title"):
        # Use filename as title
        meta["title"] = Path(file_path).stem

    return meta, sections


# ── Entity Extraction ────────────────────────────────────────

# Key concepts from the Autonomy vision (bootstrap vocabulary)
SEED_ENTITIES = {
    "Autonomy Network": "project",
    "Autonomy Core": "concept",
    "Autonomy Runtime": "concept",
    "Autonomy Surface": "concept",
    "Autonomy Infra": "concept",
    "Autonomy Modules": "concept",
    "Alice": "concept",
    "sovereignty line": "concept",
    "CRDT": "technology",
    "Automerge": "technology",
    "Peritext": "technology",
    "Pijul": "technology",
    "Yjs": "technology",
    "Loro": "technology",
    "BlindHash": "concept",
    "Signpost": "concept",
    "Uni.Lat": "concept",
    "autoresearch": "project",
    "program.md": "concept",
    "knowledge graph": "concept",
    "claims": "concept",
    "provenance": "concept",
    "trust vector": "concept",
    "feature flag": "concept",
    "workstream": "concept",
    "malleable software": "concept",
    "sovereignty": "concept",
    "gossip": "concept",
    "agentic loop": "concept",
    "harness": "concept",
}

# Common words to exclude from entity extraction
STOP_WORDS = {
    "the", "this", "that", "these", "those", "here", "there", "when", "where",
    "what", "which", "who", "how", "why", "will", "would", "could", "should",
    "have", "has", "had", "been", "being", "are", "were", "was", "not", "but",
    "and", "for", "with", "from", "into", "over", "under", "then", "than",
    "very", "just", "also", "only", "even", "still", "much", "more", "most",
    "some", "any", "all", "each", "every", "both", "few", "many", "well",
    "yes", "right", "okay", "sure", "let", "get", "got", "set", "put",
    "use", "used", "using", "make", "made", "take", "give", "keep",
    "want", "need", "know", "think", "mean", "say", "see", "look",
    "come", "going", "way", "thing", "point", "example", "instead",
    "because", "since", "already", "really", "actually", "basically",
    "probably", "exactly", "essentially", "specifically", "particularly",
    "important", "different", "possible", "necessary", "interesting",
    "first", "second", "third", "last", "next", "new", "old", "good",
    "bad", "big", "small", "long", "short", "high", "low",
    "true", "false", "null", "none", "something", "everything", "nothing",
    "user", "system", "data", "model", "layer", "level", "part",
    "note", "see", "like", "else", "case", "work", "done",
    "start", "end", "run", "call", "read", "write", "create",
}

# Pattern for capitalized terms (potential entities)
CAPITALIZED_TERM = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b")
# Pattern for technical terms in backticks
BACKTICK_TERM = re.compile(r"`([^`]+)`")
# Bold terms
BOLD_TERM = re.compile(r"\*\*([^*]+)\*\*")


def extract_entities(text: str) -> list[tuple[str, str]]:
    """Extract potential entity names from text. Returns (name, type) tuples."""
    found = {}

    # First: seed vocabulary matches
    text_lower = text.lower()
    for name, etype in SEED_ENTITIES.items():
        if name.lower() in text_lower:
            found[name.lower()] = (name, etype)

    # Backtick terms (likely technical)
    for match in BACKTICK_TERM.finditer(text):
        term = match.group(1).strip()
        if len(term) >= 2 and len(term) <= 50 and term.lower() not in STOP_WORDS:
            key = term.lower()
            if key not in found:
                found[key] = (term, "concept")

    # Multi-word capitalized terms only (single caps words are mostly sentence starts)
    for match in CAPITALIZED_TERM.finditer(text):
        term = match.group(1).strip()
        words = term.split()
        if len(words) >= 2 and len(term) >= 5:
            if all(w.lower() not in STOP_WORDS for w in words):
                key = term.lower()
                if key not in found:
                    found[key] = (term, "concept")

    return list(found.values())


# ── Ingestion Pipeline ───────────────────────────────────────

def ingest_conversation(db: GraphDB, file_path: str | Path, force: bool = False) -> dict:
    """Ingest a conversation markdown file into the graph."""
    file_path = Path(file_path)
    abs_path = str(file_path.resolve())

    # Check for existing
    existing = db.get_source_by_path(abs_path)
    if existing and not force:
        return {"status": "skipped", "source_id": existing["id"], "reason": "already ingested"}
    if existing:
        db.delete_source(existing["id"])

    text = file_path.read_text(encoding="utf-8")
    meta, turns = parse_conversation(text)

    # Create source
    source = Source(
        type="conversation",
        platform=meta.get("source", "unknown"),
        title=meta.get("title"),
        url=meta.get("url"),
        file_path=abs_path,
        metadata={k: v for k, v in meta.items() if k not in ("title", "source", "url")},
        created_at=meta.get("extracted_at", now_iso()),
    )
    db.insert_source(source)

    thoughts = []
    derivations = []
    all_entities = {}
    last_thought_id = None

    for turn in turns:
        # Extract entities from content
        ents = extract_entities(turn["content"])
        for name, etype in ents:
            key = name.lower()
            if key not in all_entities:
                all_entities[key] = (name, etype)

        if turn["role"] == "user":
            t = Thought(
                source_id=source.id,
                content=turn["content"],
                turn_number=turn["turn_number"],
                message_id=turn.get("message_id"),
            )
            db.insert_thought(t)
            thoughts.append(t)
            last_thought_id = t.id

            # Link entities to thought
            for name, etype in ents:
                eid = db.upsert_entity(name, etype)
                db.add_mention(eid, t.id, "thought")

        elif turn["role"] == "assistant":
            d = Derivation(
                source_id=source.id,
                thought_id=last_thought_id,
                content=turn["content"],
                model=meta.get("source", "unknown"),
                turn_number=turn["turn_number"],
                message_id=turn.get("message_id"),
            )
            db.insert_derivation(d)
            derivations.append(d)

            # Link entities to derivation
            for name, etype in ents:
                eid = db.upsert_entity(name, etype)
                db.add_mention(eid, d.id, "derivation")

            # Edge: derivation responds_to thought
            if last_thought_id:
                db.insert_edge(Edge(
                    source_id=d.id, source_type="derivation",
                    target_id=last_thought_id, target_type="thought",
                    relation="responds_to",
                ))

    db.commit()
    return {
        "status": "ingested",
        "source_id": source.id,
        "thoughts": len(thoughts),
        "derivations": len(derivations),
        "entities": len(all_entities),
    }


def ingest_musing(db: GraphDB, file_path: str | Path, force: bool = False) -> dict:
    """Ingest a musing markdown file into the graph."""
    file_path = Path(file_path)
    abs_path = str(file_path.resolve())

    existing = db.get_source_by_path(abs_path)
    if existing and not force:
        return {"status": "skipped", "source_id": existing["id"], "reason": "already ingested"}
    if existing:
        db.delete_source(existing["id"])

    text = file_path.read_text(encoding="utf-8")
    meta, sections = parse_musing(text, abs_path)

    source = Source(
        type="musing",
        platform="local",
        title=meta.get("title"),
        file_path=abs_path,
        metadata=meta,
    )
    db.insert_source(source)

    thoughts = []
    all_entities = {}

    for i, section in enumerate(sections):
        t = Thought(
            source_id=source.id,
            content=section,
            role="user",
            turn_number=i + 1,
        )
        db.insert_thought(t)
        thoughts.append(t)

        ents = extract_entities(section)
        for name, etype in ents:
            key = name.lower()
            if key not in all_entities:
                all_entities[key] = (name, etype)
            eid = db.upsert_entity(name, etype)
            db.add_mention(eid, t.id, "thought")

    db.commit()
    return {
        "status": "ingested",
        "source_id": source.id,
        "thoughts": len(thoughts),
        "entities": len(all_entities),
    }


def ingest_directory(db: GraphDB, dir_path: str | Path, force: bool = False) -> list[dict]:
    """Ingest all markdown files in a directory."""
    dir_path = Path(dir_path)
    results = []

    for md_file in sorted(dir_path.glob("*.md")):
        # Skip TOOL.md, CLAUDE.md, README.md (non-content files)
        if md_file.name.upper() in ("TOOL.MD", "CLAUDE.MD"):
            continue

        # Detect type by parent directory or content
        text = md_file.read_text(encoding="utf-8")
        if "## Turn " in text and ("— USER" in text or "— ASSISTANT" in text):
            result = ingest_conversation(db, md_file, force)
        else:
            result = ingest_musing(db, md_file, force)

        result["file"] = str(md_file)
        results.append(result)

    return results


# ── Claude Code Session Parser ───────────────────────────────

# Strip system-injected XML tags from content
SYSTEM_NOISE = re.compile(
    r"<(?:command-name|command-message|command-args|local-command-\w+|"
    r"system-reminder|available-deferred-tools|persisted-output)"
    r"[^>]*>[\s\S]*?</[^>]+>",
    re.DOTALL,
)
REQUEST_INTERRUPTED = re.compile(r"\[Request interrupted by user.*?\]")


def parse_claude_code_session(file_path: Path) -> tuple[dict, list[dict]]:
    """Parse a Claude Code JSONL session into metadata and content turns.

    Filters out tool_use, tool_result, file-history-snapshot, progress entries.
    Only keeps actual user prompts and assistant text responses.
    Skips sidechain (subagent) entries.
    """
    meta = {
        "session_id": file_path.stem,
        "platform": "claude-code",
    }
    turns = []
    turn_number = 0
    first_ts = None
    last_ts = None
    model = None
    total_input_tokens = 0
    total_output_tokens = 0
    pending_compact_meta: dict | None = None

    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = entry.get("type")
            ts = entry.get("timestamp", "")

            # Track timestamps
            if ts:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts

            # Context-compaction boundary: Claude writes a `type=system` entry with
            # `compactMetadata`, followed by a user-role entry with `isCompactSummary`
            # carrying the multi-thousand-char summary of the prior session. Capture
            # the metadata so it can ride along with the summary turn.
            if etype == "system" and entry.get("compactMetadata"):
                pending_compact_meta = entry["compactMetadata"]
                continue

            # Skip non-conversation entries (but keep queue-operation = human mid-work input)
            if etype not in ("user", "assistant", "queue-operation"):
                continue

            # Skip sidechain (subagent) entries
            if entry.get("isSidechain"):
                continue

            # Compact-summary turns: Claude's continuation boilerplate. Ingest with a
            # distinct role so role='user' filters (attention, title probe) skip them,
            # while keeping the content indexed in FTS.
            if entry.get("isCompactSummary") or entry.get("isVisibleInTranscriptOnly"):
                msg = entry.get("message", {})
                content_raw = msg.get("content", "")
                if isinstance(content_raw, list):
                    content_raw = "\n".join(
                        c.get("text", "") for c in content_raw
                        if isinstance(c, dict) and c.get("type") == "text"
                    )
                if not isinstance(content_raw, str) or len(content_raw) < 5:
                    continue
                turn_number += 1
                turn_entry = {
                    "turn_number": turn_number,
                    "role": "compact_summary",
                    "content": content_raw,
                    "message_id": entry.get("uuid"),
                    "parent_uuid": entry.get("parentUuid"),
                    "timestamp": ts,
                }
                if pending_compact_meta is not None:
                    turn_entry["compact_metadata"] = pending_compact_meta
                    pending_compact_meta = None
                turns.append(turn_entry)
                continue

            # Skip isMeta system entries
            if entry.get("isMeta"):
                continue

            # Queue operations are human messages sent while agent was working
            if etype == "queue-operation":
                qcontent = entry.get("content", entry.get("message", {}).get("content", ""))
                if isinstance(qcontent, str) and len(qcontent) > 5:
                    # Skip task notifications and command outputs
                    if qcontent.startswith(("<task-notification", "<local-command", "<command-name")):
                        continue
                    turn_number += 1
                    turns.append({
                        "turn_number": turn_number,
                        "role": "user",
                        "content": qcontent,
                        "message_id": entry.get("uuid"),
                        "parent_uuid": entry.get("parentUuid"),
                        "timestamp": ts,
                        "queued": True,
                    })
                continue

            msg = entry.get("message", {})
            content = msg.get("content", "")

            # Extract text content
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text_parts = []
                has_tool_result = False
                has_tool_use = False
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "text":
                        text_parts.append(c["text"])
                    elif c.get("type") == "tool_result":
                        has_tool_result = True
                    elif c.get("type") == "tool_use":
                        has_tool_use = True

                # Skip pure tool_result/tool_use entries with no text
                if not text_parts and (has_tool_result or has_tool_use):
                    continue

                text = "\n".join(text_parts)

            # Clean system noise from content
            text = SYSTEM_NOISE.sub("", text)
            text = REQUEST_INTERRUPTED.sub("", text)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()

            # Skip empty or trivially short content
            if len(text) < 5:
                continue

            # Track model
            if etype == "assistant" and msg.get("model"):
                model = msg["model"]

            # Track tokens
            usage = msg.get("usage", {})
            total_input_tokens += usage.get("input_tokens", 0)
            total_output_tokens += usage.get("output_tokens", 0)

            turn_number += 1
            turns.append({
                "turn_number": turn_number,
                "role": etype if etype == "user" else "assistant",
                "content": text,
                "message_id": entry.get("uuid"),
                "parent_uuid": entry.get("parentUuid"),
                "timestamp": ts,
            })

    meta["started_at"] = first_ts
    meta["ended_at"] = last_ts
    meta["model"] = model
    meta["total_input_tokens"] = total_input_tokens
    meta["total_output_tokens"] = total_output_tokens
    meta["total_turns"] = len(turns)

    return meta, turns


# Repo root for scanning agent-run session directories
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_session_meta(file_path: Path) -> dict:
    """Look for .session_meta.json in the same directory or parent directory.

    Returns the parsed dict if found, otherwise an empty dict.
    Used to enrich session source metadata with session type, bead_id, etc.
    """
    for search_dir in (file_path.parent, file_path.parent.parent):
        meta_file = search_dir / ".session_meta.json"
        if meta_file.exists():
            try:
                return json.loads(meta_file.read_text())
            except (json.JSONDecodeError, OSError):
                pass
    return {}


def session_target_org(file_path: Path | str, default: str = "autonomy") -> str:
    """Return the org slug a session file should land in.

    Reads ``.session_meta.json`` next to (or one level above) *file_path*
    and returns its ``graph_project`` value, falling back to *default*
    (``autonomy`` per the platform default-scope convention).

    Helper for per-org DB write routing (auto-9iq2s migration + auto-36v11
    routing). Pure read; no DB or filesystem mutation.
    """
    meta = _load_session_meta(Path(file_path))
    return meta.get("graph_project") or default


# Patterns for low-signal first-turn content that should NOT become a title.
_HANDSHAKE_RE = re.compile(r"\[dashboard\] confirming terminal link", re.IGNORECASE)
_IMAGE_PLACEHOLDER_RE = re.compile(r"^\s*\[Image #\d+\]\s*$", re.IGNORECASE)
_TASK_HEADER_RE = re.compile(r"^\s*#\s+Task:\s*", re.IGNORECASE)


def _is_low_signal_title(text: str) -> bool:
    """True if a candidate title is a placeholder/handshake we should skip."""
    if not text:
        return True
    if _IMAGE_PLACEHOLDER_RE.match(text):
        return True
    if _HANDSHAKE_RE.search(text):
        return True
    return False


def _lookup_dashboard_label(file_path: Path, session_uuid: str | None) -> str | None:
    """Return the user-set label from dashboard.db for a session, if any.

    Looks the session up by session_uuid first, then by jsonl_path. Tries
    both live and dead session tables. Best-effort: returns None on any
    error (DB missing, dashboard.db not initialised, etc.).
    """
    abs_path = str(file_path.resolve())
    try:
        from tools.dashboard.dao.dashboard_db import find_live_session, find_dead_session
        for finder in (find_live_session, find_dead_session):
            row = finder(session_uuid=session_uuid, file_path=abs_path)
            if row and (row.get("label") or "").strip():
                return row["label"].strip()
        return None
    except Exception:
        return None


def _lookup_bead_title(bead_id: str) -> str | None:
    """Best-effort lookup of a bead title from the beads (Dolt) DB.

    Returns None if Dolt is unreachable or the bead does not exist.
    Cached per-call only — callers ingest one session at a time.
    """
    if not bead_id:
        return None
    try:
        from tools.dashboard.dao.beads import get_bead_title_priority
        info = get_bead_title_priority([bead_id]).get(bead_id)
        if info and info.get("title"):
            return info["title"].strip()
    except Exception:
        pass
    return None


def _derive_session_title(meta: dict, file_path: Path, session_meta: dict,
                          turns: list[dict]) -> str | None:
    """Pick the best human-readable title for a session source.

    Preference order:
    1. dashboard.db.tmux_sessions.label (the working title the user set)
    2. For dispatch/librarian: bead_id + bead title (joined from beads DB)
    3. tmux_name / container_name (matches what active cards show)
    4. First text turn that isn't an image placeholder or handshake
    """
    session_uuid = file_path.stem
    label = _lookup_dashboard_label(file_path, session_uuid)
    if label:
        return label

    bead_id = session_meta.get("bead_id")
    if bead_id:
        bead_title = _lookup_bead_title(bead_id)
        if bead_title:
            return f"{bead_id}: {bead_title}"
        return bead_id

    container_name = session_meta.get("container_name")
    if container_name:
        return container_name

    for t in turns:
        if t.get("role") != "user":
            continue
        content = (t.get("content") or "").strip()
        if not content or _is_low_signal_title(content):
            continue
        title = content[:80].replace("\n", " ").strip()
        if len(content) > 80:
            title += "…"
        return title

    return None


def _build_summary_meta(existing_meta: dict, parsed_meta: dict, file_path: Path,
                       session_meta: dict, current_size: int) -> dict:
    """Compose the metadata blob written on every (re)ingest.

    Preserves existing fields (so user-curated keys like `tags` survive),
    overwrites the summary fields the ingester owns.
    """
    out = dict(existing_meta) if existing_meta else {}
    out.update({
        "session_id": parsed_meta.get("session_id") or out.get("session_id") or file_path.stem,
        "session_uuid": file_path.stem,
        "model": parsed_meta.get("model") or out.get("model"),
        "total_input_tokens": parsed_meta.get("total_input_tokens", 0),
        "total_output_tokens": parsed_meta.get("total_output_tokens", 0),
        "total_turns": parsed_meta.get("total_turns", 0),
        "started_at": parsed_meta.get("started_at") or out.get("started_at"),
        "ended_at": parsed_meta.get("ended_at"),
        "file_size": current_size,
    })
    # Overlay session_meta fields (session_type, bead_id, etc.) — these are
    # immutable across the session lifetime, so write them every time in case
    # .session_meta.json appeared after first ingest.
    for key in ("type", "bead_id", "job_id", "job_type", "context_id",
                "container_name", "launched_at", "graph_project", "graph_tags"):
        if key in session_meta:
            out[f"session_{key}" if key == "type" else key] = session_meta[key]
    return out


def ingest_claude_code_session(
    db: GraphDB, file_path: str | Path, force: bool = False, project: str | None = None,
) -> dict:
    """Ingest a Claude Code JSONL session into the graph.

    Supports incremental ingestion: if a source already exists and force=False,
    only new turns (beyond the highest turn_number already stored) are added.
    Uses file size tracking to skip unchanged files without parsing.
    """
    file_path = Path(file_path)
    abs_path = str(file_path.resolve())
    # Normalize container paths to host paths to prevent duplicates
    abs_path = abs_path.replace("/home/agent/", "/home/jeremy/")
    abs_path = abs_path.replace("/workspace/repo/", "/home/jeremy/workspace/autonomy/")

    # Project + tags are sourced from .session_meta.json. Sessions launched via
    # session_launcher.launch_session() always write graph_project (and
    # graph_tags) into the meta file. Legacy sessions without meta are left
    # with whatever project the existing source row already carries.
    session_meta = _load_session_meta(file_path)
    if project is None:
        project = session_meta.get("graph_project")

    existing = db.get_source_by_path(abs_path)

    # Fast path: check file size before parsing
    current_size = file_path.stat().st_size
    if existing and not force:
        existing_meta = json.loads(existing["metadata"]) if existing["metadata"] else {}
        last_size = existing_meta.get("file_size", 0)
        if last_size and current_size == last_size:
            return {"status": "skipped", "source_id": existing["id"], "reason": "already up to date"}

    meta, turns = parse_claude_code_session(file_path)

    if not turns:
        return {"status": "skipped", "source_id": existing["id"] if existing else None, "reason": "no content turns found"}

    if existing and force:
        db.delete_source(existing["id"])
        existing = None

    if existing:
        # Incremental: only ingest turns beyond what we already have
        max_turn = db.get_max_turn(existing["id"])
        new_turns = [t for t in turns if t["turn_number"] > max_turn]

        source_id = existing["id"]
        thoughts = []
        derivations = []
        all_entities = {}

        if new_turns:
            # Find the last thought from existing data to link new derivations
            last_thought_row = db.conn.execute(
                "SELECT id FROM thoughts WHERE source_id = ? ORDER BY turn_number DESC LIMIT 1",
                (source_id,)
            ).fetchone()
            last_thought_id = last_thought_row["id"] if last_thought_row else None

            for turn in new_turns:
                if turn["role"] == "compact_summary":
                    # Compaction boundary summary: index content for FTS but
                    # skip entity extraction and don't update last_thought_id —
                    # subsequent assistant responses shouldn't responds_to it.
                    t_meta = {"timestamp": turn.get("timestamp", "")}
                    if turn.get("compact_metadata"):
                        t_meta["compact_metadata"] = turn["compact_metadata"]
                    t = Thought(
                        source_id=source_id,
                        content=turn["content"],
                        role="compact_summary",
                        turn_number=turn["turn_number"],
                        message_id=turn.get("message_id"),
                        metadata=t_meta,
                        created_at=turn.get("timestamp") or now_iso(),
                    )
                    db.insert_thought(t)
                    thoughts.append(t)
                    continue

                ents = extract_entities(turn["content"])
                for name, etype in ents:
                    key = name.lower()
                    if key not in all_entities:
                        all_entities[key] = (name, etype)

                if turn["role"] == "user":
                    t = Thought(
                        source_id=source_id,
                        content=turn["content"],
                        turn_number=turn["turn_number"],
                        message_id=turn.get("message_id"),
                        metadata={"timestamp": turn.get("timestamp", "")},
                        created_at=turn.get("timestamp") or now_iso(),
                    )
                    db.insert_thought(t)
                    thoughts.append(t)
                    last_thought_id = t.id

                    for name, etype in ents:
                        eid = db.upsert_entity(name, etype)
                        db.add_mention(eid, t.id, "thought")

                elif turn["role"] == "assistant":
                    d = Derivation(
                        source_id=source_id,
                        thought_id=last_thought_id,
                        content=turn["content"],
                        model=meta.get("model", "claude-code"),
                        turn_number=turn["turn_number"],
                        message_id=turn.get("message_id"),
                        metadata={"timestamp": turn.get("timestamp", "")},
                        created_at=turn.get("timestamp") or now_iso(),
                    )
                    db.insert_derivation(d)
                    derivations.append(d)

                    for name, etype in ents:
                        eid = db.upsert_entity(name, etype)
                        db.add_mention(eid, d.id, "derivation")

                    if last_thought_id:
                        db.insert_edge(Edge(
                            source_id=d.id, source_type="derivation",
                            target_id=last_thought_id, target_type="thought",
                            relation="responds_to",
                        ))

        # Refresh summary fields on every incremental pass — even when no new
        # content turns landed (file may have grown via tool_use/tool_result
        # entries that bump file_size and ended_at without producing new turns).
        existing_meta = json.loads(existing["metadata"]) if existing["metadata"] else {}
        new_meta = _build_summary_meta(existing_meta, meta, file_path, session_meta, current_size)
        # Re-derive title in case set-label fired or bead linkage appeared after first ingest.
        new_title = _derive_session_title(meta, file_path, session_meta, turns) or existing.get("title")
        db.update_source_summary(
            source_id,
            title=new_title,
            metadata=new_meta,
            last_activity_at=meta.get("ended_at") or existing.get("last_activity_at"),
        )
        db.commit()

        if not new_turns:
            return {"status": "refreshed", "source_id": source_id, "reason": "summary refreshed"}

        return {
            "status": "updated",
            "source_id": source_id,
            "session_id": meta["session_id"],
            "new_thoughts": len(thoughts),
            "new_derivations": len(derivations),
            "new_entities": len(all_entities),
            "from_turn": max_turn + 1,
            "to_turn": turns[-1]["turn_number"],
        }

    # Fresh ingestion
    title = _derive_session_title(meta, file_path, session_meta, turns)
    source_meta = _build_summary_meta({}, meta, file_path, session_meta, current_size)

    source = Source(
        type="session",
        platform="claude-code",
        project=project,
        title=title,
        file_path=abs_path,
        metadata=source_meta,
        created_at=meta.get("started_at", now_iso()),
        last_activity_at=meta.get("ended_at") or meta.get("started_at") or now_iso(),
    )
    db.insert_source(source)

    thoughts = []
    derivations = []
    all_entities = {}
    last_thought_id = None

    for turn in turns:
        if turn["role"] == "compact_summary":
            t_meta = {"timestamp": turn.get("timestamp", "")}
            if turn.get("compact_metadata"):
                t_meta["compact_metadata"] = turn["compact_metadata"]
            t = Thought(
                source_id=source.id,
                content=turn["content"],
                role="compact_summary",
                turn_number=turn["turn_number"],
                message_id=turn.get("message_id"),
                metadata=t_meta,
                created_at=turn.get("timestamp") or now_iso(),
            )
            db.insert_thought(t)
            thoughts.append(t)
            continue

        ents = extract_entities(turn["content"])
        for name, etype in ents:
            key = name.lower()
            if key not in all_entities:
                all_entities[key] = (name, etype)

        if turn["role"] == "user":
            t = Thought(
                source_id=source.id,
                content=turn["content"],
                turn_number=turn["turn_number"],
                message_id=turn.get("message_id"),
                metadata={"timestamp": turn.get("timestamp", "")},
                created_at=turn.get("timestamp") or now_iso(),
            )
            db.insert_thought(t)
            thoughts.append(t)
            last_thought_id = t.id

            for name, etype in ents:
                eid = db.upsert_entity(name, etype)
                db.add_mention(eid, t.id, "thought")

        elif turn["role"] == "assistant":
            d = Derivation(
                source_id=source.id,
                thought_id=last_thought_id,
                content=turn["content"],
                model=meta.get("model", "claude-code"),
                turn_number=turn["turn_number"],
                message_id=turn.get("message_id"),
                metadata={"timestamp": turn.get("timestamp", "")},
                created_at=turn.get("timestamp") or now_iso(),
            )
            db.insert_derivation(d)
            derivations.append(d)

            for name, etype in ents:
                eid = db.upsert_entity(name, etype)
                db.add_mention(eid, d.id, "derivation")

            if last_thought_id:
                db.insert_edge(Edge(
                    source_id=d.id, source_type="derivation",
                    target_id=last_thought_id, target_type="thought",
                    relation="responds_to",
                ))

    db.commit()
    return {
        "status": "ingested",
        "source_id": source.id,
        "session_id": meta["session_id"],
        "title": title,
        "thoughts": len(thoughts),
        "derivations": len(derivations),
        "entities": len(all_entities),
        "model": meta.get("model"),
        "tokens": meta.get("total_input_tokens", 0) + meta.get("total_output_tokens", 0),
    }


def ingest_claude_code_project(db: GraphDB, project_path: str | Path = None, force: bool = False) -> list[dict]:
    """Ingest all Claude Code sessions for a project (or the current one)."""
    if project_path is None:
        # Default: current project
        project_path = Path.home() / ".claude" / "projects" / "-home-jeremy-workspace-autonomy"
    project_path = Path(project_path)

    results = []
    for jsonl_file in sorted(project_path.glob("*.jsonl")):
        result = ingest_claude_code_session(db, jsonl_file, force)
        result["file"] = str(jsonl_file)
        results.append(result)

    return results


def ingest_all_claude_code(db: GraphDB, force: bool = False) -> list[dict]:
    """Ingest all Claude Code sessions across all projects.

    Scans two locations:
    1. ~/.claude/projects/ — user sessions, chatwith, terminal containers
    2. data/agent-runs/*/sessions/ — dispatch and librarian agent sessions

    Project scoping comes from each session's ``.session_meta.json``
    (``graph_project`` field). Sessions launched via the autonomy
    infrastructure always carry meta; legacy sessions retain whatever
    project they were first ingested with.
    """
    results = []

    # ── Location 1: host ~/.claude/projects ───────────────────
    projects_dir = Path.home() / ".claude" / "projects"
    if projects_dir.exists():
        for project_dir in sorted(projects_dir.iterdir()):
            if not project_dir.is_dir():
                continue
            for jsonl_file in sorted(project_dir.glob("*.jsonl")):
                result = ingest_claude_code_session(db, jsonl_file, force)
                result["file"] = str(jsonl_file)
                results.append(result)

    # ── Location 2: data/agent-runs/*/sessions/ ───────────────
    agent_runs_dir = _REPO_ROOT / "data" / "agent-runs"
    if agent_runs_dir.exists():
        for run_dir in sorted(agent_runs_dir.iterdir()):
            if not run_dir.is_dir():
                continue
            sessions_dir = run_dir / "sessions"
            if not sessions_dir.is_dir():
                continue
            for project_dir in sorted(sessions_dir.iterdir()):
                if not project_dir.is_dir():
                    continue
                for jsonl_file in sorted(project_dir.glob("*.jsonl")):
                    result = ingest_claude_code_session(db, jsonl_file, force)
                    result["file"] = str(jsonl_file)
                    results.append(result)

    return results


# ── Status File Ingestion ────────────────────────────────────

def _extract_status_category(file_path: Path) -> str:
    """Extract the status category (active/complete/pending/archived/consolidated) from path."""
    parts = file_path.parts
    for p in reversed(parts):
        if p in ("active", "complete", "completed", "pending", "archived", "consolidated"):
            return p
    return "unknown"


def ingest_doc_file(db: GraphDB, file_path: str | Path, project: str | None = None, force: bool = False) -> dict:
    """Ingest a documentation markdown file (TOOL.md, CLAUDE.md, README.md) as a searchable source."""
    file_path = Path(file_path)
    abs_path = str(file_path.resolve())

    existing = db.get_source_by_path(abs_path)
    if existing and not force:
        return {"status": "skipped", "source_id": existing["id"], "reason": "already ingested"}
    if existing:
        db.delete_source(existing["id"])

    text = file_path.read_text(encoding="utf-8", errors="replace")
    if len(text.strip()) < 10:
        return {"status": "skipped", "reason": "empty file"}

    # Title from first heading or filename
    title_match = re.match(r"^#\s+(.+)", text)
    title = title_match.group(1).strip() if title_match else file_path.name

    source = Source(
        type="docs",
        platform="local",
        project=project,
        title=title,
        file_path=abs_path,
        metadata={"filename": file_path.name, "authorship": "human"},
        created_at=now_iso(),
    )
    db.insert_source(source)

    # Split on ## headings to create one thought per section
    sections = re.split(r"\n(?=## )", text.strip())
    if len(sections) == 1:
        # No ## headings — split on blank-line-separated blocks
        sections = re.split(r"\n{3,}", text.strip())
    sections = [s.strip() for s in sections if s.strip() and len(s.strip()) > 10]

    thoughts = []
    all_entities = {}

    for i, section in enumerate(sections):
        t = Thought(
            source_id=source.id,
            content=section,
            role="user",
            turn_number=i + 1,
        )
        db.insert_thought(t)
        thoughts.append(t)

        ents = extract_entities(section)
        for name, etype in ents:
            key = name.lower()
            if key not in all_entities:
                all_entities[key] = (name, etype)
            eid = db.upsert_entity(name, etype)
            db.add_mention(eid, t.id, "thought")

    db.commit()
    return {
        "status": "ingested",
        "source_id": source.id,
        "title": title,
        "thoughts": len(thoughts),
        "entities": len(all_entities),
    }


def ingest_docs_dir(db: GraphDB, dir_path: str | Path, project: str | None = None, force: bool = False) -> list[dict]:
    """Recursively ingest documentation markdown files (TOOL.md, CLAUDE.md, README.md, etc.)."""
    dir_path = Path(dir_path)
    doc_patterns = ["**/TOOL.md", "**/CLAUDE.md", "**/README.md", "**/ABOUT.md", "**/docs/**/*.md"]
    seen = set()
    results = []

    for pattern in doc_patterns:
        for md_file in sorted(dir_path.glob(pattern)):
            if str(md_file) in seen:
                continue
            seen.add(str(md_file))
            result = ingest_doc_file(db, md_file, project=project, force=force)
            result["file"] = str(md_file)
            results.append(result)

    return results


def ingest_status_file(db: GraphDB, file_path: str | Path, project: str | None = None, authorship: str = "mixed", force: bool = False) -> dict:
    """Ingest a status markdown file into the graph."""
    file_path = Path(file_path)
    abs_path = str(file_path.resolve())

    existing = db.get_source_by_path(abs_path)
    if existing and not force:
        return {"status": "skipped", "source_id": existing["id"], "reason": "already ingested"}
    if existing:
        db.delete_source(existing["id"])

    text = file_path.read_text(encoding="utf-8", errors="replace")
    if len(text.strip()) < 10:
        return {"status": "skipped", "reason": "empty file"}

    category = _extract_status_category(file_path)

    # Try to extract date from filename (common patterns: 20251024_000659_NAME.md or 2026-01-14-name.md)
    fname = file_path.stem
    date_match = re.match(r"(\d{4})(\d{2})(\d{2})", fname) or re.match(r"(\d{4})-(\d{2})-(\d{2})", fname)
    created_at = None
    if date_match:
        y, m, d = date_match.groups()
        created_at = f"{y}-{m}-{d}T00:00:00Z"

    # Title from first heading or filename
    title_match = re.match(r"^#\s+(.+)", text)
    title = title_match.group(1).strip() if title_match else fname.replace("_", " ")

    source = Source(
        type="status",
        platform="local",
        project=project,
        title=title,
        file_path=abs_path,
        metadata={"category": category, "filename": file_path.name, "authorship": authorship},
        created_at=created_at or now_iso(),
    )
    db.insert_source(source)

    # Split into sections on ## headings, or paragraph blocks
    sections = re.split(r"\n(?=## )", text.strip())
    if len(sections) == 1:
        sections = re.split(r"\n{3,}", text.strip())
    sections = [s.strip() for s in sections if s.strip() and len(s.strip()) > 10]

    thoughts = []
    all_entities = {}

    for i, section in enumerate(sections):
        t = Thought(
            source_id=source.id,
            content=section,
            role="user",
            turn_number=i + 1,
        )
        db.insert_thought(t)
        thoughts.append(t)

        ents = extract_entities(section)
        for name, etype in ents:
            key = name.lower()
            if key not in all_entities:
                all_entities[key] = (name, etype)
            eid = db.upsert_entity(name, etype)
            db.add_mention(eid, t.id, "thought")

    db.commit()
    return {
        "status": "ingested",
        "source_id": source.id,
        "title": title,
        "category": category,
        "thoughts": len(thoughts),
        "entities": len(all_entities),
    }


def ingest_status_dir(db: GraphDB, dir_path: str | Path, project: str | None = None, authorship: str = "mixed", force: bool = False) -> list[dict]:
    """Recursively ingest all status markdown files under a directory."""
    dir_path = Path(dir_path)
    results = []

    for md_file in sorted(dir_path.rglob("*.md")):
        result = ingest_status_file(db, md_file, project=project, authorship=authorship, force=force)
        result["file"] = str(md_file)
        results.append(result)

    return results


# ── Git Commit Ingestion ─────────────────────────────────────

def parse_git_log(repo_path: Path, since: str | None = None) -> list[dict]:
    """Parse git log into structured commits."""
    cmd = [
        "git", "-C", str(repo_path), "log",
        "--format=%H%x00%an%x00%ae%x00%aI%x00%s%x00%b%x1e",
    ]
    if since:
        cmd.append(f"--since={since}")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return []

    commits = []
    for entry in result.stdout.split("\x1e"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("\x00")
        if len(parts) < 5:
            continue
        commits.append({
            "hash": parts[0],
            "author": parts[1],
            "email": parts[2],
            "date": parts[3],
            "subject": parts[4],
            "body": parts[5].strip() if len(parts) > 5 else "",
        })

    return commits


def ingest_git_commits(
    db: GraphDB, repo_path: str | Path, project: str | None = None,
    since: str | None = None, force: bool = False,
) -> dict:
    """Ingest git commit history as a source with thoughts."""
    repo_path = Path(repo_path).resolve()
    # Use repo path as the source file_path for dedup
    source_key = f"git:{repo_path}"

    existing = db.get_source_by_path(source_key)

    commits = parse_git_log(repo_path, since=since)
    if not commits:
        return {"status": "skipped", "reason": "no commits found"}

    if existing and not force:
        # Incremental: check if we have new commits
        existing_meta = json.loads(existing["metadata"]) if existing["metadata"] else {}
        last_hash = existing_meta.get("latest_hash")
        if last_hash:
            new_commits = []
            for c in commits:
                if c["hash"] == last_hash:
                    break
                new_commits.append(c)
            if not new_commits:
                return {"status": "skipped", "source_id": existing["id"], "reason": "already up to date"}
            commits = new_commits
        else:
            # First incremental run — skip, already ingested
            return {"status": "skipped", "source_id": existing["id"], "reason": "already ingested"}

    if existing and force:
        db.delete_source(existing["id"])
        existing = None

    if not existing:
        source = Source(
            type="git-log",
            platform="git",
            project=project,
            title=f"Git log: {repo_path.name}",
            file_path=source_key,
            metadata={
                "repo_path": str(repo_path),
                "latest_hash": commits[0]["hash"],
                "commit_count": len(commits),
            },
            created_at=commits[-1]["date"] if commits else now_iso(),
        )
        db.insert_source(source)
        source_id = source.id
    else:
        source_id = existing["id"]
        # Update latest hash
        existing_meta = json.loads(existing["metadata"]) if existing["metadata"] else {}
        existing_meta["latest_hash"] = commits[0]["hash"]
        existing_meta["commit_count"] = existing_meta.get("commit_count", 0) + len(commits)
        db.update_source_metadata(source_id, existing_meta)

    thoughts = []
    all_entities = {}

    # Commits are newest-first from git log; reverse for chronological turn numbering
    base_turn = db.get_max_turn(source_id) if existing else 0
    for i, commit in enumerate(reversed(commits)):
        content = f"**{commit['subject']}**"
        if commit["body"]:
            content += f"\n\n{commit['body']}"
        content += f"\n\n_commit {commit['hash'][:12]} by {commit['author']} on {commit['date'][:10]}_"

        t = Thought(
            source_id=source_id,
            content=content,
            role="user",
            turn_number=base_turn + i + 1,
            message_id=commit["hash"],
            metadata={"author": commit["author"], "date": commit["date"]},
        )
        db.insert_thought(t)
        thoughts.append(t)

        ents = extract_entities(commit["subject"] + " " + commit["body"])
        for name, etype in ents:
            key = name.lower()
            if key not in all_entities:
                all_entities[key] = (name, etype)
            eid = db.upsert_entity(name, etype)
            db.add_mention(eid, t.id, "thought")

    db.commit()
    return {
        "status": "ingested" if not existing else "updated",
        "source_id": source_id,
        "commits": len(commits),
        "entities": len(all_entities),
        "repo": str(repo_path),
    }
