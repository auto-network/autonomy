"""AgentRun — transport-agnostic lifecycle for agent capture and linking.

This is the core interface for capturing agent work product, regardless of
whether the agent ran as an in-process subagent (Agent tool → tool-results/)
or in a Docker container (docker run → docker cp output).

The lifecycle:
  1. LAUNCH:  compose prompt, spawn agent
  2. CAPTURE: find trace, parse JSONL, ingest into graph
  3. LINK:    create edges from bead → session source
  4. CLOSE:   update bead status based on result
"""

from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import Source, Thought, Derivation, Edge, new_id, now_iso
from .db import GraphDB
from .ingest import extract_entities, SYSTEM_NOISE, REQUEST_INTERRUPTED


# ── AgentRun Dataclass ────────────────────────────────────────

@dataclass
class AgentRun:
    """A completed agent execution, ready for ingestion."""
    agent_id: str                         # unique run ID (from Agent tool or container ID)
    bead_id: str | None = None            # bead this agent worked on (if any)
    prompt: str = ""                      # the prompt sent to the agent
    trace_path: Path | None = None        # path to JSONL trace file
    parent_session_id: str | None = None  # parent session that spawned this agent
    project: str | None = None            # project scope
    model: str | None = None              # model used (extracted from trace)
    status: str = "unknown"               # completed/failed/partial (extracted from trace)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tool_uses: int = 0
    duration_ms: int = 0
    started_at: str = ""
    ended_at: str = ""
    result_text: str = ""                 # final assistant text output
    metadata: dict = field(default_factory=dict)


# ── Trace Parser ──────────────────────────────────────────────

def parse_agent_trace(trace_path: Path) -> AgentRun:
    """Parse a subagent JSONL trace file into an AgentRun.

    Works with both:
    - tool-results/*.txt files (in-process subagents, isSidechain=true)
    - docker output JSONL files (container agents)
    """
    run = AgentRun(agent_id=trace_path.stem)
    run.trace_path = trace_path

    turns = []
    turn_number = 0
    first_ts = None
    last_ts = None

    with open(trace_path, "r", encoding="utf-8") as f:
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

            if ts:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts

            # Extract agent ID from first entry
            if entry.get("agentId") and run.agent_id == trace_path.stem:
                run.agent_id = entry["agentId"]

            # Extract parent session
            if entry.get("sessionId") and not run.parent_session_id:
                run.parent_session_id = entry["sessionId"]

            # Skip non-conversation entries
            if etype not in ("user", "assistant"):
                if etype == "progress":
                    run.total_tool_uses += 1
                continue

            msg = entry.get("message", {})
            content = msg.get("content", "")

            # Extract text content
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text_parts = []
                tool_use_count = 0
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "text":
                        text_parts.append(c["text"])
                    elif c.get("type") == "tool_use":
                        tool_use_count += 1
                    elif c.get("type") == "tool_result":
                        pass

                run.total_tool_uses += tool_use_count

                if not text_parts:
                    continue
                text = "\n".join(text_parts)

            # Clean system noise
            text = SYSTEM_NOISE.sub("", text)
            text = REQUEST_INTERRUPTED.sub("", text)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()

            if len(text) < 5:
                continue

            # Track model
            if etype == "assistant" and msg.get("model"):
                run.model = msg["model"]

            # Track tokens
            usage = msg.get("usage", {})
            run.total_input_tokens += usage.get("input_tokens", 0)
            run.total_output_tokens += usage.get("output_tokens", 0)

            turn_number += 1
            turns.append({
                "turn_number": turn_number,
                "role": "user" if etype == "user" else "assistant",
                "content": text,
                "message_id": entry.get("uuid"),
                "timestamp": ts,
            })

            # Capture the last assistant text as result
            if etype == "assistant":
                run.result_text = text

    run.started_at = first_ts or ""
    run.ended_at = last_ts or ""
    run.metadata["turns"] = turns
    run.metadata["turn_count"] = len(turns)

    # Extract prompt from first user turn
    if turns and turns[0]["role"] == "user":
        run.prompt = turns[0]["content"]

    return run


# ── Ingestion ─────────────────────────────────────────────────

def ingest_agent_run(db: GraphDB, run: AgentRun, force: bool = False) -> dict:
    """Ingest a completed agent run into the graph.

    Creates:
    - A source (type='agent-run') with full metadata
    - Thoughts for user turns, derivations for assistant turns
    - Entities extracted from content
    - Edge: source → parent session (spawned_by)
    - Edge: source → bead (implements)
    """
    if not run.trace_path:
        return {"status": "error", "reason": "no trace_path"}

    abs_path = str(run.trace_path.resolve())

    # Dedup check
    existing = db.get_source_by_path(abs_path)
    if existing and not force:
        return {"status": "skipped", "source_id": existing["id"], "reason": "already ingested"}
    if existing:
        db.delete_source(existing["id"])

    turns = run.metadata.get("turns", [])
    if not turns:
        return {"status": "skipped", "reason": "no content turns"}

    # Title from prompt
    title = run.prompt[:80].replace("\n", " ").strip()
    if len(run.prompt) > 80:
        title += "…"

    source = Source(
        type="agent-run",
        platform="claude-code",
        project=run.project,
        title=title,
        file_path=abs_path,
        metadata={
            "agent_id": run.agent_id,
            "bead_id": run.bead_id,
            "parent_session_id": run.parent_session_id,
            "model": run.model,
            "total_input_tokens": run.total_input_tokens,
            "total_output_tokens": run.total_output_tokens,
            "total_tool_uses": run.total_tool_uses,
            "duration_ms": run.duration_ms,
            "turn_count": len(turns),
            "status": run.status,
        },
        created_at=run.started_at or now_iso(),
    )
    db.insert_source(source)

    thoughts = []
    derivations = []
    all_entities = {}
    last_thought_id = None

    for turn in turns:
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
                model=run.model or "claude-code",
                turn_number=turn["turn_number"],
                message_id=turn.get("message_id"),
                metadata={"timestamp": turn.get("timestamp", "")},
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

    # ── Cross-reference edges ─────────────────────────────────

    # Link to parent session
    if run.parent_session_id:
        parent_source = db.conn.execute(
            "SELECT id FROM sources WHERE json_extract(metadata, '$.session_id') = ? LIMIT 1",
            (run.parent_session_id,)
        ).fetchone()
        if parent_source:
            db.insert_edge(Edge(
                source_id=source.id, source_type="source",
                target_id=parent_source["id"], target_type="source",
                relation="spawned_by",
            ))

    # Link to bead
    if run.bead_id:
        db.insert_edge(Edge(
            source_id=run.bead_id, source_type="bead",
            target_id=source.id, target_type="source",
            relation="implemented_by",
        ))

    db.commit()
    return {
        "status": "ingested",
        "source_id": source.id,
        "agent_id": run.agent_id,
        "bead_id": run.bead_id,
        "thoughts": len(thoughts),
        "derivations": len(derivations),
        "entities": len(all_entities),
        "tool_uses": run.total_tool_uses,
        "tokens": run.total_input_tokens + run.total_output_tokens,
    }


# ── Discovery ─────────────────────────────────────────────────

def _is_agent_trace(path: Path) -> bool:
    """Check if a file is a JSONL agent trace (not cached tool output)."""
    try:
        with open(path) as fh:
            first_line = fh.readline().strip()
        if not first_line or first_line[0] != "{":
            return False
        entry = json.loads(first_line)
        if not isinstance(entry, dict):
            return False
        return bool(entry.get("isSidechain") or entry.get("type") in ("user", "assistant"))
    except (json.JSONDecodeError, OSError, KeyError):
        return False


def discover_subagent_traces(session_id: str | None = None) -> list[Path]:
    """Find all subagent trace files for a session or all sessions.

    Looks in two locations:
    1. ~/.claude/projects/*/tool-results/*.txt (persisted by Claude Code)
    2. /tmp/claude-*/tasks/*.output (background task outputs)
    """
    traces = []

    # Location 1: tool-results
    projects_dir = Path.home() / ".claude" / "projects"
    if projects_dir.exists():
        for project_dir in projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            for session_dir in project_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                if session_id and session_id not in session_dir.name:
                    continue
                results_dir = session_dir / "tool-results"
                if results_dir.exists():
                    for f in results_dir.iterdir():
                        if not f.suffix == ".txt":
                            continue
                        # Check if it's actually JSONL (not cached tool output)
                        if _is_agent_trace(f):
                            traces.append(f)

    # Location 2: /tmp task outputs
    tmp_base = Path("/tmp")
    for claude_dir in tmp_base.glob("claude-*"):
        for tasks_dir in claude_dir.rglob("tasks"):
            if not tasks_dir.is_dir():
                continue
            for f in tasks_dir.iterdir():
                if not f.suffix == ".output":
                    continue
                if _is_agent_trace(f):
                    traces.append(f)

    return sorted(set(traces))


def ingest_all_agent_runs(db: GraphDB, session_id: str | None = None, force: bool = False) -> list[dict]:
    """Discover and ingest all subagent traces."""
    traces = discover_subagent_traces(session_id)
    results = []
    for trace_path in traces:
        run = parse_agent_trace(trace_path)
        result = ingest_agent_run(db, run, force=force)
        result["file"] = str(trace_path)
        results.append(result)
    return results
