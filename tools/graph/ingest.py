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
from .db import GraphDB, resolve_caller_db_path


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
_CODEX_NOISE_PREFIXES = (
    "<crosstalk ",
    "<system-",
    "<local-command",
    "<task-notification",
    "<command-name>",
    "<command-message>",
    "<command-args>",
)


class ClaudeTurnExtractor:
    """Stateful incremental extractor for Claude Code JSONL turns (W1).

    ``feed(entry)`` consumes one already-parsed JSONL line at a time and
    returns the turn dict to append, or ``None`` if the entry produced no
    turn (tool noise, sidechain, compaction metadata, low-signal text,
    etc.). All cross-entry state — running turn number, token totals,
    first/last timestamp, and the pending compact-metadata carry-over —
    lives in ``.state``, a JSON-serializable dict.

    ``parse_claude_code_session`` batch-feeds every line through a fresh
    extractor. The tail appender (W3) instead resumes via
    ``ClaudeTurnExtractor.from_state(saved_state)`` and feeds only the
    newly appended lines — the state carries everything needed (in
    particular ``pending_compact_meta``, which bridges a ``system`` entry
    to the ``isCompactSummary`` entry that may arrive in a later batch) to
    make that produce byte-identical turns to a full reparse.
    """

    def __init__(self, state: dict | None = None):
        self._s: dict = {
            "turn_number": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "model": None,
            "first_ts": None,
            "last_ts": None,
            "pending_compact_meta": None,
        }
        if state:
            self._s.update(state)

    @property
    def state(self) -> dict:
        return dict(self._s)

    @classmethod
    def from_state(cls, state: dict) -> "ClaudeTurnExtractor":
        return cls(state=state)

    def feed(self, entry: dict) -> dict | None:
        s = self._s
        etype = entry.get("type")
        ts = entry.get("timestamp", "")

        # Track timestamps
        if ts:
            if s["first_ts"] is None:
                s["first_ts"] = ts
            s["last_ts"] = ts

        # Context-compaction boundary: Claude writes a `type=system` entry with
        # `compactMetadata`, followed by a user-role entry with `isCompactSummary`
        # carrying the multi-thousand-char summary of the prior session. Capture
        # the metadata so it can ride along with the summary turn.
        if etype == "system" and entry.get("compactMetadata"):
            s["pending_compact_meta"] = entry["compactMetadata"]
            return None

        # Skip non-conversation entries (but keep queue-operation = human mid-work input)
        if etype not in ("user", "assistant", "queue-operation"):
            return None

        # Skip sidechain (subagent) entries
        if entry.get("isSidechain"):
            return None

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
                return None
            s["turn_number"] += 1
            turn_entry = {
                "turn_number": s["turn_number"],
                "role": "compact_summary",
                "content": content_raw,
                "message_id": entry.get("uuid"),
                "parent_uuid": entry.get("parentUuid"),
                "timestamp": ts,
            }
            if s["pending_compact_meta"] is not None:
                turn_entry["compact_metadata"] = s["pending_compact_meta"]
                s["pending_compact_meta"] = None
            return turn_entry

        # Skip isMeta system entries
        if entry.get("isMeta"):
            return None

        # Queue operations are human messages sent while agent was working
        if etype == "queue-operation":
            qcontent = entry.get("content", entry.get("message", {}).get("content", ""))
            if isinstance(qcontent, str) and len(qcontent) > 5:
                # Skip task notifications and command outputs
                if qcontent.startswith(("<task-notification", "<local-command", "<command-name")):
                    return None
                s["turn_number"] += 1
                return {
                    "turn_number": s["turn_number"],
                    "role": "user",
                    "content": qcontent,
                    "message_id": entry.get("uuid"),
                    "parent_uuid": entry.get("parentUuid"),
                    "timestamp": ts,
                    "queued": True,
                }
            return None

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
                return None

            text = "\n".join(text_parts)

        # Clean system noise from content
        text = SYSTEM_NOISE.sub("", text)
        text = REQUEST_INTERRUPTED.sub("", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

        # Skip empty or trivially short content
        if len(text) < 5:
            return None

        # Track model
        if etype == "assistant" and msg.get("model"):
            s["model"] = msg["model"]

        # Track tokens
        usage = msg.get("usage", {})
        s["total_input_tokens"] += usage.get("input_tokens", 0)
        s["total_output_tokens"] += usage.get("output_tokens", 0)

        s["turn_number"] += 1
        return {
            "turn_number": s["turn_number"],
            "role": etype if etype == "user" else "assistant",
            "content": text,
            "message_id": entry.get("uuid"),
            "parent_uuid": entry.get("parentUuid"),
            "timestamp": ts,
        }


def parse_claude_code_session(file_path: Path) -> tuple[dict, list[dict]]:
    """Parse a Claude Code JSONL session into metadata and content turns.

    Filters out tool_use, tool_result, file-history-snapshot, progress entries.
    Only keeps actual user prompts and assistant text responses.
    Skips sidechain (subagent) entries.

    Thin batch wrapper over :class:`ClaudeTurnExtractor` — feeds every line
    through a fresh extractor and reads the running totals back out of
    its final state.
    """
    meta = {
        "session_id": file_path.stem,
        "platform": "claude-code",
    }
    extractor = ClaudeTurnExtractor()
    turns: list[dict] = []

    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            turn = extractor.feed(entry)
            if turn is not None:
                turns.append(turn)

    s = extractor.state
    meta["started_at"] = s["first_ts"]
    meta["ended_at"] = s["last_ts"]
    meta["model"] = s["model"]
    meta["total_input_tokens"] = s["total_input_tokens"]
    meta["total_output_tokens"] = s["total_output_tokens"]
    meta["total_turns"] = len(turns)

    return meta, turns


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _clean_codex_text(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", str(text or "")).strip()


def _is_codex_noise_text(text: str) -> bool:
    stripped = str(text or "").strip()
    return stripped.startswith(_CODEX_NOISE_PREFIXES)


def _codex_message_id(
    payload: dict, entry: dict, role: str, text: str,
) -> str | None:
    """Return the deterministic message_id for a Codex event_msg chat turn.

    Mirrors :func:`tools.dashboard.session_harness.codex_message_id` so the
    live overlay path and graph ingest agree on the same identity for
    ``event_msg`` user/agent rows that lack a payload UUID. The shared
    ``codex-<role>:<sha1[:16]>`` fallback is what lets accepted turn
    corrections on those turns resolve back to the ingested thought.

    Falls back to ``entry.uuid`` when the shared rule yields nothing — keeps
    behaviour stable for legacy rollouts where the outer entry carried a
    UUID but the payload did not.
    """
    from tools.dashboard.session_harness import codex_message_id

    msg_id = codex_message_id(payload, role, text)
    if msg_id:
        return msg_id
    outer = entry.get("uuid") if isinstance(entry, dict) else None
    if isinstance(outer, str) and outer:
        return outer
    return None


class CodexTurnExtractor:
    """Stateful incremental extractor for Codex rollout JSONL turns (W1).

    Same contract as :class:`ClaudeTurnExtractor` — ``feed(entry)`` returns
    a turn dict or ``None``; ``.state`` / ``from_state()`` carry everything
    needed to resume mid-file. Codex token totals are *absolute* snapshots
    reported by each ``token_count`` event (not deltas), so unlike Claude's
    running sum, resuming just needs the most recent value, which state
    already holds.

    W1 §12.3: ``user_message`` text matching :data:`_CODEX_NOISE_PREFIXES`
    (operator briefs injected into the session) is no longer dropped — it's
    ingested with ``role='injected'`` so it stays searchable via FTS while
    remaining filtered out of attention/title-probe (role != 'user'). The
    ``message_id`` is still computed with role="user" to match the live
    viewer overlay's identity (see :func:`_codex_message_id`), so accepted
    turn-corrections keep resolving correctly regardless of which role the
    graph stored.
    """

    def __init__(self, state: dict | None = None):
        self._s: dict = {
            "turn_number": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "model": None,
            "first_ts": None,
            "last_ts": None,
            "originator": None,
            "model_provider": None,
        }
        if state:
            self._s.update(state)

    @property
    def state(self) -> dict:
        return dict(self._s)

    @classmethod
    def from_state(cls, state: dict) -> "CodexTurnExtractor":
        return cls(state=state)

    def feed(self, entry: dict) -> dict | None:
        s = self._s
        ts = entry.get("timestamp", "")
        if ts:
            if s["first_ts"] is None:
                s["first_ts"] = ts
            s["last_ts"] = ts

        etype = entry.get("type")
        payload = entry.get("payload") or {}
        if not isinstance(payload, dict):
            return None

        if etype == "session_meta":
            if payload.get("originator"):
                s["originator"] = payload["originator"]
            if payload.get("model_provider"):
                s["model_provider"] = payload["model_provider"]
            if payload.get("model"):
                s["model"] = str(payload["model"])
            return None

        if etype == "compacted":
            return None

        if etype != "event_msg":
            return None

        event_type = payload.get("type")
        if event_type == "token_count":
            info = payload.get("info") or {}
            total_usage = info.get("total_token_usage") or {}
            s["total_input_tokens"] = _safe_int(
                total_usage.get("input_tokens"), s["total_input_tokens"]
            )
            s["total_output_tokens"] = _safe_int(
                total_usage.get("output_tokens"), s["total_output_tokens"]
            )
            return None

        if event_type == "user_message":
            text = _clean_codex_text(str(payload.get("message") or ""))
            if len(text) < 5:
                return None
            role = "injected" if _is_codex_noise_text(text) else "user"
            s["turn_number"] += 1
            return {
                "turn_number": s["turn_number"],
                "role": role,
                "content": text,
                "message_id": _codex_message_id(payload, entry, "user", text),
                "timestamp": ts,
            }

        if event_type == "agent_message":
            text = _clean_codex_text(str(payload.get("message") or ""))
            if len(text) < 5:
                return None
            s["turn_number"] += 1
            return {
                "turn_number": s["turn_number"],
                "role": "assistant",
                "content": text,
                "message_id": _codex_message_id(payload, entry, "assistant", text),
                "timestamp": ts,
            }

        return None


def parse_codex_session(file_path: Path) -> tuple[dict, list[dict]]:
    """Parse a Codex rollout JSONL session into metadata and content turns.

    Keeps only operator-visible text from ``event_msg.user_message`` and
    ``event_msg.agent_message``. Tool use/results, progress items, and
    compaction metadata are excluded from graph content ingest.

    Thin batch wrapper over :class:`CodexTurnExtractor` — feeds every line
    through a fresh extractor and reads the running totals back out of its
    final state.
    """
    meta = {
        "session_id": file_path.stem,
        "platform": "codex-cli",
    }
    extractor = CodexTurnExtractor()
    turns: list[dict] = []

    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            turn = extractor.feed(entry)
            if turn is not None:
                turns.append(turn)

    s = extractor.state
    if s["originator"]:
        meta["originator"] = s["originator"]
    if s["model_provider"]:
        meta["model_provider"] = s["model_provider"]
    meta["started_at"] = s["first_ts"]
    meta["ended_at"] = s["last_ts"]
    meta["model"] = s["model"]
    meta["total_input_tokens"] = s["total_input_tokens"]
    meta["total_output_tokens"] = s["total_output_tokens"]
    meta["total_turns"] = len(turns)

    return meta, turns


# Repo root for scanning agent-run session directories
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


_META_WALK_MAX_DEPTH = 5


def _load_session_meta(file_path: Path) -> dict:
    """Look upward from *file_path* for a ``.session_meta.json``.

    Walks ``file_path.parent`` and successive ancestors until the meta
    is found, the ancestor is named ``agent-runs`` (run-tree boundary),
    or :data:`_META_WALK_MAX_DEPTH` ancestors have been checked. Codex
    rollouts live three directories below their meta
    (``<run>/sessions/YYYY/MM/DD/rollout-*.jsonl`` vs.
    ``<run>/sessions/.session_meta.json``); the deepest meta wins so
    nested layouts route correctly. Returns ``{}`` if nothing is found.
    """
    current = file_path.parent
    for _ in range(_META_WALK_MAX_DEPTH):
        meta_file = current / ".session_meta.json"
        if meta_file.exists():
            try:
                return json.loads(meta_file.read_text())
            except (json.JSONDecodeError, OSError):
                return {}
        if current.name == "agent-runs" or current.parent == current:
            break
        current = current.parent
    return {}


# Claude Code project-dir → org slug. Host sessions live at
# ``~/.claude/projects/-<encoded-cwd>/<uuid>.jsonl`` and carry no
# ``.session_meta.json``, so the only routing signal is the encoded cwd.
# Add entries here when a new on-host project dir starts producing sessions.
_HOST_PROJECT_TO_ORG: dict[str, str] = {
    "-home-jeremy-workspace-enterprise-ng": "anchore",
    "-home-jeremy-workspace-enterprise": "anchore",
    "-home-jeremy-workspace-enterprise-dev-compose-files": "anchore",
    "-home-jeremy-workspace-autonomy": "autonomy",
    "-home-jeremy-infra": "blindhash",
    "-home-jeremy-blindhash": "blindhash",
    "-home-jeremy-jira": "personal",
    "-home-jeremy-boatlore": "personal",
    "-home-jeremy-boatlore-chartroom": "personal",
    "-home-jeremy-boatlore-compendium": "personal",
    "-home-jeremy-boatlore-passage": "personal",
    "-home-jeremy-ai-pres-my-video": "personal",
}


def _org_from_host_project_path(file_path: Path) -> str | None:
    """Return the org slug for a Claude-Code host .jsonl, or ``None``.

    Recognises the ``~/.claude/projects/-<encoded-cwd>/<uuid>.jsonl`` layout
    and looks the encoded-cwd segment up in :data:`_HOST_PROJECT_TO_ORG`.
    Returns ``None`` for unknown projects or non-host paths — the caller
    will fall back to its other signals.
    """
    parent_name = file_path.parent.name
    if not parent_name.startswith("-"):
        return None
    return _HOST_PROJECT_TO_ORG.get(parent_name)


def session_target_org(file_path: Path | str, default: str | None = None) -> str | None:
    """Return the org slug a session file should land in, or ``None``.

    Resolution order:

    1. ``.session_meta.json`` near *file_path* (container sessions) —
       returns ``graph_org`` or the legacy ``graph_project`` if set.
    2. Claude-Code host project dir lookup
       (:data:`_HOST_PROJECT_TO_ORG`) — for bare host ``.jsonl`` files
       that have no meta alongside them.
    3. *default*.

    The default is ``None`` so callers fail-closed (skip ingest) for
    sessions without org context, rather than silently routing to
    ``personal.db``.

    Helper for per-org DB write routing. Pure read; no mutation.
    """
    file_path = Path(file_path)
    meta = _load_session_meta(file_path)
    from_meta = meta.get("graph_org") or meta.get("graph_project")
    if from_meta:
        return from_meta
    from_host = _org_from_host_project_path(file_path)
    if from_host:
        return from_host
    return default


def _open_db_for_session(
    file_path: Path, *, default_org: str | None = None,
) -> GraphDB | None:
    """Open the GraphDB that *file_path*'s session should write to.

    Returns ``None`` when the session has no resolvable org (no meta or
    meta lacking ``graph_org``/``graph_project``). The caller is expected
    to skip ingest in that case so an unscoped session can never be
    silently filed in ``personal.db``.
    """
    org = session_target_org(file_path, default=default_org)
    if org is None:
        return None
    return GraphDB(resolve_caller_db_path(org))


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

    Legacy fallback only — dispatch sessions stamp ``bead_title`` into
    ``.session_meta.json`` at launch (see ``launch_session_cli.py``), so
    ``_derive_session_title`` reads that first and only reaches here for
    sessions launched before that field existed.

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
        bead_title = session_meta.get("bead_title") or _lookup_bead_title(bead_id)
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


def _normalize_session_path(file_path: Path) -> str:
    abs_path = str(file_path.resolve())
    abs_path = abs_path.replace("/home/agent/", "/home/jeremy/")
    abs_path = abs_path.replace("/workspace/repo/", "/home/jeremy/workspace/autonomy/")
    return abs_path


def detect_session_format(file_path: Path) -> str:
    """Return the JSONL harness format for *file_path*."""
    session_meta = _load_session_meta(file_path)
    harness = str(session_meta.get("harness") or "").strip().lower()
    if harness == "codex":
        return "codex"
    if harness == "claude":
        return "claude"
    if file_path.name.startswith("rollout-"):
        return "codex"
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
    except OSError:
        return "claude"
    if not first_line:
        return "claude"
    try:
        raw = json.loads(first_line)
    except json.JSONDecodeError:
        return "claude"
    payload = raw.get("payload") or {}
    if raw.get("type") == "session_meta" and isinstance(payload, dict):
        if payload.get("originator") == "codex-tui":
            return "codex"
    return "claude"


def _ingest_agentic_session(
    db: GraphDB,
    *,
    file_path: Path,
    abs_path: str,
    parser,
    default_model: str,
    existing_source: dict,
    session_meta: dict,
    force: bool,
) -> dict:
    """Append agentic-session turns onto an existing eager-created source.

    The dashboard's ``api_agent_action_dispatch`` (auto-pqgrl, Round 5)
    inserts a ``type='agentic'`` source row when the action launches.
    The agent's JSONL is later appended onto THAT row by this function
    rather than creating a fresh ``type='session'`` row by file_path.

    Differences from the generic session ingest path:

    * Source resolved by ``agentic_source_id`` from
      ``.session_meta.json``, not by ``file_path``.
    * Title is preserved (the dispatch endpoint set it to the action's
      label, e.g. "Update Title & Summary"); ``_derive_session_title``
      is intentionally NOT called.
    * ``file_path`` on the source row is left as-is (the dashboard
      stores ``agentic:<slug>`` there to keep the row uniquely
      addressable independent of the JSONL location).
    * Returns ``status='agentic_updated'`` so callers can distinguish
      this branch in logs.

    Mirrors the incremental-append logic of ``_ingest_text_session``'s
    "existing" branch — turns whose ``turn_number`` is greater than the
    current max are appended; older turns are skipped to keep the call
    idempotent.
    """
    source_id = existing_source["id"]
    current_size = file_path.stat().st_size
    existing_meta = (
        json.loads(existing_source["metadata"]) if existing_source.get("metadata") else {}
    )

    if not force:
        last_size = existing_meta.get("file_size", 0)
        if last_size and current_size == last_size:
            return {
                "status": "skipped",
                "source_id": source_id,
                "reason": "already up to date (agentic)",
            }

    meta, turns = parser(file_path)
    if not turns:
        return {
            "status": "skipped",
            "source_id": source_id,
            "reason": "no content turns found (agentic)",
        }

    max_turn = db.get_max_turn(source_id)
    new_turns = _dedup_new_turns(db, source_id, turns, max_turn)

    thoughts, derivations, all_entities = _write_new_turns(
        db, source_id, new_turns, model=meta.get("model", default_model),
    )

    new_meta = _build_summary_meta(
        existing_meta, meta, file_path, session_meta, current_size,
    )
    db.update_source_summary(
        source_id,
        title=None,  # preserve dashboard-set title
        metadata=new_meta,
        last_activity_at=meta.get("ended_at") or existing_source.get("last_activity_at"),
    )
    db.commit()

    if not new_turns:
        return {
            "status": "agentic_refreshed",
            "source_id": source_id,
            "reason": "summary refreshed",
        }
    return {
        "status": "agentic_updated",
        "source_id": source_id,
        "session_id": meta["session_id"],
        "new_thoughts": len(thoughts),
        "new_derivations": len(derivations),
        "new_entities": len(all_entities),
        "from_turn": max_turn + 1,
        "to_turn": turns[-1]["turn_number"],
    }


def _dedup_new_turns(
    db: GraphDB, source_id: str, turns: list[dict], max_turn: int,
) -> list[dict]:
    """Filter ``turns`` down to the ones genuinely new for ``source_id``.

    Two stages, both required:

    1. The coarse/cheap ``turn_number > max_turn`` filter. Still correct
       on its own for *finding* candidates — a renumbering can only ever
       shift turn numbers upward (entries that used to be dropped now get
       counted), so every genuinely-new turn still clears this bar — but
       it is NOT sufficient to exclude duplicates (see below).
    2. An identity filter dropping any candidate whose ``message_id``
       already exists among this source's thoughts/derivations.

    Why both are needed (auto-cpg1x): ``turn_number`` is POSITIONAL, not a
    stable identity. W1's codex ``role='injected'`` change made previously
    -dropped noise ``user_message`` entries start consuming turn-number
    slots. A codex source ingested *before* that change and grown *after*
    it re-parses with every turn after the first noise entry shifted to a
    higher number — the positional cursor alone reads those shifted
    positions as new content and duplicates already-ingested turns
    (confirmed prod damage: source e26143b6-a08 duplicated turns 76-84).
    Codex ``message_id``s are content hashes (see ``_codex_message_id``),
    so a genuine duplicate collides exactly. Claude sessions have no
    filter change and were never affected, but this fix applies uniformly
    since it costs nothing extra when there's nothing to dedup.

    A turn with no ``message_id`` (rare) has no identity to check and is
    trusted to the turn_number filter alone — unchanged from before this
    fix. This is also the at-least-once idempotency the tail-primary
    ``GraphAppender`` (W3) needs for crash-safe redelivery, so it reuses
    this exact function rather than reimplementing the check.
    """
    candidates = [t for t in turns if t["turn_number"] > max_turn]
    if not candidates:
        return candidates
    existing_ids = {
        row["message_id"] for row in db.conn.execute(
            "SELECT message_id FROM thoughts WHERE source_id = ? AND message_id IS NOT NULL "
            "UNION SELECT message_id FROM derivations WHERE source_id = ? AND message_id IS NOT NULL",
            (source_id, source_id),
        ).fetchall()
    }
    if not existing_ids:
        return candidates
    return [
        t for t in candidates
        if not t.get("message_id") or t["message_id"] not in existing_ids
    ]


def _write_new_turns(
    db: GraphDB, source_id: str, new_turns: list[dict], *, model: str | None,
) -> tuple[list[Thought], list[Derivation], dict]:
    """Write a batch of new turns (thoughts/derivations/entities/edges) onto
    an existing source. Shared by the full-reparse incremental path
    (``_ingest_text_session``'s existing branch) and the tail-primary
    ``GraphAppender`` (W3) — identical writes, identical role branching
    (compact_summary/injected/user/assistant), identical ``last_thought_id``
    threading for derivation→thought edges, so a tail-fed batch and a
    sweep-fed batch land byte-identical content regardless of which path
    got there first (the W3 soak's dedup guarantee depends on this).

    ``new_turns`` must already be filtered to ``turn_number > max_turn`` —
    this function does no deduping of its own beyond the message-id/turn-
    number identity SQLite enforces via each turn's own insert.

    Returns ``(thoughts, derivations, all_entities)`` — ``all_entities``
    maps lowercased entity name → ``(name, type)`` for the caller's
    entity-count bookkeeping. Caller owns the transaction (commit/rollback);
    this function only executes/queues writes on ``db.conn``.
    """
    thoughts: list[Thought] = []
    derivations: list[Derivation] = []
    all_entities: dict = {}
    if not new_turns:
        return thoughts, derivations, all_entities

    last_thought_row = db.conn.execute(
        "SELECT id FROM thoughts WHERE source_id = ? ORDER BY turn_number DESC LIMIT 1",
        (source_id,)
    ).fetchone()
    last_thought_id = last_thought_row["id"] if last_thought_row else None

    for turn in new_turns:
        if turn["role"] == "compact_summary":
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

        if turn["role"] == "injected":
            # Codex operator briefs (W1 §12.3) — kept searchable via FTS
            # but role-filtered out of attention/title-probe like
            # compact_summary. No entity extraction, no thread edge: these
            # aren't part of the user<->assistant exchange.
            t = Thought(
                source_id=source_id,
                content=turn["content"],
                role="injected",
                turn_number=turn["turn_number"],
                message_id=turn.get("message_id"),
                metadata={"timestamp": turn.get("timestamp", "")},
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
                model=model,
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

    return thoughts, derivations, all_entities


def _ingest_text_session(
    db: GraphDB,
    file_path: str | Path,
    *,
    parser,
    platform: str,
    default_model: str,
    force: bool = False,
    project: str | None = None,
) -> dict:
    """Shared ingest path for text-only JSONL session harnesses."""
    file_path = Path(file_path)
    abs_path = _normalize_session_path(file_path)

    session_meta = _load_session_meta(file_path)
    if project is None:
        project = session_meta.get("graph_project")

    # ── Agentic session routing (auto-gh2iv) ────────────────────
    # When .session_meta.json carries type='agentic' + agentic_source_id,
    # the source row was eager-created by the dashboard's
    # api_agent_action_dispatch endpoint at launch time. The ingest must
    # APPEND turns to that existing row — not create a new source —
    # so /graph/<agentic_source_id> renders the agent's work after
    # completion. The title is set by the dispatch endpoint (the
    # action's label) and must NOT be overwritten by _derive_session_title.
    if session_meta.get("type") == "agentic":
        agentic_source_id = session_meta.get("agentic_source_id")
        if agentic_source_id:
            existing_agentic = db.get_source(agentic_source_id)
            if existing_agentic is not None:
                return _ingest_agentic_session(
                    db,
                    file_path=file_path,
                    abs_path=abs_path,
                    parser=parser,
                    default_model=default_model,
                    existing_source=existing_agentic,
                    session_meta=session_meta,
                    force=force,
                )
            # If the source row doesn't exist yet (race / mismatched org),
            # fall through to the legacy create-by-file_path path so the
            # session content isn't dropped.

    existing = db.get_source_by_path(abs_path)

    current_size = file_path.stat().st_size
    if existing and not force:
        existing_meta = json.loads(existing["metadata"]) if existing["metadata"] else {}
        last_size = existing_meta.get("file_size", 0)
        if last_size and current_size == last_size:
            return {"status": "skipped", "source_id": existing["id"], "reason": "already up to date"}

    meta, turns = parser(file_path)

    if not turns:
        return {"status": "skipped", "source_id": existing["id"] if existing else None, "reason": "no content turns found"}

    if existing and force:
        db.delete_source(existing["id"])
        existing = None

    if existing:
        source_id = existing["id"]
        max_turn = db.get_max_turn(source_id)
        new_turns = _dedup_new_turns(db, source_id, turns, max_turn)

        thoughts, derivations, all_entities = _write_new_turns(
            db, source_id, new_turns, model=meta.get("model", default_model),
        )

        existing_meta = json.loads(existing["metadata"]) if existing["metadata"] else {}
        new_meta = _build_summary_meta(existing_meta, meta, file_path, session_meta, current_size)
        # Title derivation runs at source creation only (W5) — incremental
        # passes leave the title column untouched (title=None here means
        # "don't SET it", not "set it to NULL" — see update_source_summary).
        # Dashboard label renames reach the title via write-through
        # (server.py's update_source_title), not by re-deriving here on
        # every tick. Passing the re-read existing["title"] instead would be
        # a read-modify-write race: write-through can rename the source
        # mid-pass (entity extraction on a big delta takes seconds) and this
        # update would then clobber the new title with the stale value.
        #
        # W2 exception: a source eager-created at session init (before any
        # JSONL content existed) has no title to preserve — it's None, not
        # yet-derived. The first pass that lands real content owes it one
        # derivation; every pass after that falls back to the W5 rule above
        # once existing["title"] is truthy.
        new_title = None
        if not existing.get("title") and existing_meta.get("eager"):
            new_title = _derive_session_title(meta, file_path, session_meta, turns)
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

    title = _derive_session_title(meta, file_path, session_meta, turns)
    source_meta = _build_summary_meta({}, meta, file_path, session_meta, current_size)

    source = Source(
        type="session",
        platform=platform,
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

        if turn["role"] == "injected":
            # Codex operator briefs (W1 §12.3) — kept searchable via FTS but
            # role-filtered out of attention/title-probe like compact_summary.
            # No entity extraction, no thread edge: these aren't part of the
            # user<->assistant exchange.
            t = Thought(
                source_id=source.id,
                content=turn["content"],
                role="injected",
                turn_number=turn["turn_number"],
                message_id=turn.get("message_id"),
                metadata={"timestamp": turn.get("timestamp", "")},
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
                model=meta.get("model", default_model),
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


def ingest_claude_code_session(
    db: GraphDB, file_path: str | Path, force: bool = False, project: str | None = None,
) -> dict:
    """Ingest a Claude Code JSONL session into the graph."""
    return _ingest_text_session(
        db,
        file_path,
        parser=parse_claude_code_session,
        platform="claude-code",
        default_model="claude-code",
        force=force,
        project=project,
    )


def ingest_codex_session(
    db: GraphDB, file_path: str | Path, force: bool = False, project: str | None = None,
) -> dict:
    """Ingest a Codex rollout JSONL session into the graph."""
    return _ingest_text_session(
        db,
        file_path,
        parser=parse_codex_session,
        platform="codex-cli",
        default_model="codex-cli",
        force=force,
        project=project,
    )


def ingest_session_file(
    db: GraphDB, file_path: str | Path, force: bool = False, project: str | None = None,
) -> dict:
    """Ingest a JSONL session file, routing by detected harness format."""
    path = Path(file_path)
    if detect_session_format(path) == "codex":
        return ingest_codex_session(db, path, force=force, project=project)
    return ingest_claude_code_session(db, path, force=force, project=project)


def refresh_session_source(source: dict) -> dict:
    """Best-effort refresh for one existing session source."""
    if source.get("type") != "session":
        return source

    source_id = str(source.get("id") or "").strip()
    home_org = str(source.get("org") or "").strip()
    file_path = str(source.get("file_path") or "").strip()
    if not source_id or not home_org or not file_path:
        return source

    jsonl_path = Path(file_path)
    if not jsonl_path.exists():
        return source

    try:
        current_size = jsonl_path.stat().st_size
        existing_meta = (
            json.loads(source.get("metadata") or "{}")
            if isinstance(source.get("metadata"), str)
            else (source.get("metadata") or {})
        )
    except (OSError, TypeError, ValueError):
        current_size = None
        existing_meta = {}
    if current_size is not None and existing_meta.get("file_size") == current_size:
        return source

    db = GraphDB.open_org_db(home_org, mode="rw")
    try:
        ingest_session_file(db, jsonl_path, force=False)
        refreshed = db.get_source(source_id) or source
        refreshed.setdefault("org", home_org)
        return refreshed
    finally:
        db.close()


def _ingest_session_routed(jsonl_file: Path, force: bool) -> dict:
    """Open the right per-org DB for *jsonl_file* and ingest.

    Separate from :func:`ingest_claude_code_session` so tests and explicit
    callers (single-session CLI, tests pinning ``GRAPH_DB``) keep passing
    a ``db`` handle. Batch entry points below call this helper so each
    session lands in the DB named by its own ``.session_meta.json``.

    Fail-closed: if the session has no resolvable org (missing meta or
    meta without ``graph_org``/``graph_project``), the file is skipped
    rather than dumped into ``personal.db``. This prevents the cross-org
    duplicates we got when re-ingest passes filed autonomy sessions
    twice — once routed correctly at session-end, once into personal
    on a later sweep that couldn't find the meta.
    """
    db = _open_db_for_session(jsonl_file)
    if db is None:
        return {"status": "skipped", "reason": "no graph_org in meta"}
    try:
        return ingest_session_file(db, jsonl_file, force=force)
    finally:
        db.close()


def ingest_claude_code_project(
    db: GraphDB | None = None,
    project_path: str | Path = None,
    force: bool = False,
) -> list[dict]:
    """Ingest all Claude Code sessions for a project (or the current one).

    ``db=None`` (the norm) routes each session to its own per-org DB based
    on ``.session_meta.json.graph_org``. Passing a ``db`` handle forces
    every session into that connection — legacy / test behaviour.
    """
    if project_path is None:
        # Default: current project
        project_path = Path.home() / ".claude" / "projects" / "-home-jeremy-workspace-autonomy"
    project_path = Path(project_path)

    results = []
    for jsonl_file in sorted(project_path.glob("*.jsonl")):
        if db is None:
            result = _ingest_session_routed(jsonl_file, force)
        else:
            result = ingest_session_file(db, jsonl_file, force)
        result["file"] = str(jsonl_file)
        results.append(result)

    return results


def _scan_session_files() -> list[Path]:
    """Every session JSONL across both known roots, as a flat list.

    1. ~/.claude/projects/ — user sessions, chatwith, terminal containers
    2. data/agent-runs/*/sessions/ — dispatch and librarian agent sessions

    Shared by :func:`ingest_all_claude_code` (legacy full-reparse sweep)
    and :func:`catch_up_sweep` (W4 manifest-driven sweep) so both agree
    on exactly what "the estate" means.
    """
    files: list[Path] = []

    projects_dir = Path.home() / ".claude" / "projects"
    if projects_dir.exists():
        for project_dir in sorted(projects_dir.iterdir()):
            if not project_dir.is_dir():
                continue
            files.extend(sorted(project_dir.glob("*.jsonl")))

    agent_runs_dir = _REPO_ROOT / "data" / "agent-runs"
    if agent_runs_dir.exists():
        for run_dir in sorted(agent_runs_dir.iterdir()):
            if not run_dir.is_dir():
                continue
            sessions_dir = run_dir / "sessions"
            if not sessions_dir.is_dir():
                continue
            files.extend(sorted(sessions_dir.rglob("*.jsonl")))

    return files


def ingest_all_claude_code(
    db: GraphDB | None = None, force: bool = False,
) -> list[dict]:
    """Ingest all Claude Code sessions across all projects.

    Org routing comes from each session's ``.session_meta.json``
    (``graph_org`` / legacy ``graph_project`` field). Sessions launched
    via the autonomy infrastructure always carry meta; sessions without
    meta default to ``personal.db`` (auto-txg5.3 scopeless convergence).
    Passing an explicit ``db`` short-circuits routing — every session is
    written to that connection (legacy / test behaviour).

    Full-reparse sweep — opens a GraphDB per file regardless of whether
    it changed since the last pass. Superseded as the steady-state sweep
    by :func:`catch_up_sweep` (W4); kept for explicit ``--force`` full
    reparse and for callers that pass an explicit ``db`` (tests, single-
    connection legacy behavior).
    """
    results = []
    for jsonl_file in _scan_session_files():
        if db is None:
            result = _ingest_session_routed(jsonl_file, force)
        else:
            result = ingest_session_file(db, jsonl_file, force)
        result["file"] = str(jsonl_file)
        results.append(result)
    return results


def catch_up_sweep(*, force: bool = False) -> dict:
    """Manifest-driven catch-up sweep (W4 §FS-4).

    Steady state (nothing changed since the last pass): scandir + stat
    only, comparing against ``ingest_manifest`` — zero ``GraphDB`` opens.
    Changed or new files are grouped by org and ingested with ONE
    ``GraphDB`` connection per org for the whole batch (org resolved
    once per file via :func:`session_target_org`, not re-resolved per
    write). Sealed rows (files this sweep no longer finds on disk — the
    session ended and nothing further will change) are skipped by future
    passes unless ``force=True``, which also unseals anything it
    successfully re-ingests.

    This is the function ``/api/graph/sessions --all`` and the CLI's
    ``graph sessions --all`` call — the replacement for
    ``ingest_all_claude_code`` as the routine/timer-driven sweep.
    ``ingest_all_claude_code`` remains available for explicit full
    reparse and tests.

    Returns ``{"scanned", "unchanged", "changed", "sealed", "db_opens",
    "orgs_touched", "results"}``.
    """
    from tools.dashboard.dao.dashboard_db import (
        get_active_manifest_paths,
        get_manifest_entry,
        get_sealed_manifest_paths,
        seal_manifest_entry,
        upsert_manifest_entry,
    )

    files = _scan_session_files()
    scanned_paths: set[str] = set()
    sealed_paths = get_sealed_manifest_paths()

    by_org: dict[str, list[Path]] = {}
    unchanged = 0

    for f in files:
        abs_path = _normalize_session_path(f)
        scanned_paths.add(abs_path)
        if not force and abs_path in sealed_paths:
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        entry = get_manifest_entry(abs_path)
        if not force and entry and entry["size"] == st.st_size and entry["mtime"] == st.st_mtime:
            unchanged += 1
            continue
        org = session_target_org(f)
        if not org:
            continue
        by_org.setdefault(org, []).append(f)

    results: list[dict] = []
    db_opens = 0
    for org, org_files in by_org.items():
        db = GraphDB(resolve_caller_db_path(org))
        db_opens += 1
        try:
            for f in org_files:
                result = ingest_session_file(db, f, force=force)
                result["file"] = str(f)
                results.append(result)
                try:
                    st = f.stat()
                except OSError:
                    continue
                abs_path = _normalize_session_path(f)
                upsert_manifest_entry(
                    abs_path, size=st.st_size, mtime=st.st_mtime,
                    inode=getattr(st, "st_ino", None), org=org,
                    source_id=result.get("source_id"),
                    ingest_offset=st.st_size, state="active",
                )
        finally:
            db.close()

    # Seal manifest rows for files this pass no longer finds on disk —
    # the session's file went away (moved/deleted); nothing more will
    # ever change about it. Rows already sealed are left alone (no-op
    # UPDATE avoided).
    sealed = 0
    for stale_path in get_active_manifest_paths() - scanned_paths:
        seal_manifest_entry(stale_path)
        sealed += 1

    return {
        "scanned": len(files),
        "unchanged": unchanged,
        "changed": sum(len(v) for v in by_org.values()),
        "sealed": sealed,
        "db_opens": db_opens,
        "orgs_touched": len(by_org),
        "results": results,
    }


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
