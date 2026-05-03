"""Session harness adapter seam for live transcript parsing.

Harnesses own raw transcript parsing.  The dashboard above this module owns
shared normalized entries, activity state, SSE delivery, and rendering.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import re
import shlex
import sqlite3
from typing import Protocol, Any


logger = logging.getLogger(__name__)


_CODEX_PATCH_FILE_RE = re.compile(r"^\*\*\* (Update|Add|Delete) File: (.+)$", re.MULTILINE)
_CODEX_TOOL_OUTPUT_SESSION_RE = re.compile(r"Process running with session ID (\d+)")
_CODEX_TOOL_OUTPUT_EXIT_RE = re.compile(r"Process exited with code (-?\d+)")
_CODEX_TOOL_OUTPUT_TIME_RE = re.compile(r"Wall time:\s*([0-9.]+)\s*seconds?")
_CODEX_TOOL_OUTPUT_BODY_RE = re.compile(r"\nOutput:\n", re.MULTILINE)
_CODEX_SESSION_PROGRESS_STATE: dict[str, dict[str, dict[str, str] | set[str]]] = {}


class SessionHarness(Protocol):
    """Contract for a session transcript harness."""

    name: str

    async def register_session(
        self,
        *,
        monitor: Any,
        tmux_name: str,
        session_type: str,
        project: str,
        run_dir: Path | None = None,
        seed_message: str = "",
        session_uuid: str | None = None,
        jsonl_path: Path | None = None,
        resolution_dir: Path | None = None,
        bead_id: str | None = None,
    ) -> None:
        """Register a new session with the shared monitor."""

    def resolve_session(
        self,
        *,
        tmux_name: str,
        row: dict | None = None,
        jsonl_path: Path | None = None,
        handshake_text: str | None = None,
    ) -> dict | None:
        """Resolve a session onto its backing transcript and persist the link."""

    def attach_live_monitoring(
        self,
        *,
        monitor: Any,
        tmux_name: str,
        jsonl_path: Path,
        resolution_dir: Path | None = None,
        reset_offset: bool = False,
        reset_state: bool = False,
    ) -> None:
        """Attach live tailing for an already-linked session."""

    def parse_line(self, line: str) -> dict | list[dict] | None:
        """Parse one raw transcript line into normalized viewer entries."""

    def postprocess_entries(
        self,
        entries: list[dict],
        *,
        session_dir: Path | None = None,
    ) -> list[dict]:
        """Apply harness-specific entry post-processing."""

    def extract_message_text(self, raw_entry: dict) -> str:
        """Return the best last-message preview text from a raw transcript event."""

    def extract_context_tokens(self, raw_entry: dict, current_tokens: int) -> int:
        """Update the current context-token estimate from a raw transcript event."""

    def extract_model(self, raw_entry: dict, current_model: str | None) -> str | None:
        """Return the model id observed in this entry, or ``current_model`` if none.

        Called once per parsed JSONL line during ingest. Implementations must
        be cheap and tolerant of missing fields; callers persist the latest
        non-empty value to ``tmux_sessions.model`` (auto-ngis4).
        """

    def extract_harness_state(
        self,
        raw_entry: dict,
        current_state: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Return updated harness-specific session state, or ``current_state``."""


class ClaudeSessionHarness:
    """Adapter over the current Claude-only implementation."""

    name = "claude"

    async def register_session(
        self,
        *,
        monitor: Any,
        tmux_name: str,
        session_type: str,
        project: str,
        run_dir: Path | None = None,
        seed_message: str = "",
        session_uuid: str | None = None,
        jsonl_path: Path | None = None,
        resolution_dir: Path | None = None,
        bead_id: str | None = None,
    ) -> None:
        if session_type == "host":
            projects_dir = Path.home() / ".claude" / "projects" / project
            await monitor.register(
                tmux_name=tmux_name,
                session_type="host",
                project=project,
                harness=self.name,
                resolution_dir=projects_dir,
            )
            asyncio.create_task(
                _watch_for_claude_host_jsonl(monitor, projects_dir, tmux_name),
            )
            return

        sess_dir = resolution_dir
        if sess_dir is None:
            if run_dir is not None:
                sess_dir = run_dir / "sessions"
            elif jsonl_path is not None:
                sess_dir = jsonl_path if jsonl_path.is_dir() else jsonl_path.parent
        await monitor.register(
            tmux_name=tmux_name,
            session_type=session_type,
            project=project,
            jsonl_path=sess_dir,
            bead_id=bead_id,
            seed_message=seed_message,
            session_uuid=session_uuid,
            resolution_dir=sess_dir,
            harness=self.name,
        )

    def resolve_session(
        self,
        *,
        tmux_name: str,
        row: dict | None = None,
        jsonl_path: Path | None = None,
        handshake_text: str | None = None,
    ) -> dict | None:
        if handshake_text:
            return _resolve_claude_handshake_link(tmux_name, handshake_text)

        if jsonl_path is not None:
            if row and row.get("type") == "host":
                return None
            return _link_session_file(tmux_name, jsonl_path, project=(row or {}).get("project"))

        if row and row.get("type") == "host":
            found = _resolve_claude_host_jsonl(tmux_name)
            if found is not None:
                return _link_session_file(tmux_name, found)
        return None

    def attach_live_monitoring(
        self,
        *,
        monitor: Any,
        tmux_name: str,
        jsonl_path: Path,
        resolution_dir: Path | None = None,
        reset_offset: bool = False,
        reset_state: bool = False,
    ) -> None:
        _attach_live_monitoring(
            monitor,
            tmux_name=tmux_name,
            jsonl_path=jsonl_path,
            resolution_dir=resolution_dir,
            reset_offset=reset_offset,
            reset_state=reset_state,
        )

    def parse_line(self, line: str) -> dict | list[dict] | None:
        return parse_claude_log_line(line)

    def postprocess_entries(
        self,
        entries: list[dict],
        *,
        session_dir: Path | None = None,
    ) -> list[dict]:
        return postprocess_claude_entries(entries, session_dir=session_dir)

    def extract_message_text(self, raw_entry: dict) -> str:
        if raw_entry.get("isSidechain"):
            return ""
        if raw_entry.get("isCompactSummary") or raw_entry.get("isVisibleInTranscriptOnly"):
            return ""
        etype = raw_entry.get("type")
        if etype not in ("user", "assistant"):
            return ""
        msg = raw_entry.get("message", {})
        content = msg.get("content", "")
        if isinstance(content, str) and len(content) > 5:
            return content[:150]
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if len(text) > 5:
                        return text[:150]
        return ""

    def extract_context_tokens(self, raw_entry: dict, current_tokens: int) -> int:
        if raw_entry.get("type") != "assistant":
            return current_tokens
        usage = raw_entry.get("message", {}).get("usage", {})
        if not usage:
            return current_tokens
        ctx = (
            usage.get("input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
        )
        return ctx if ctx > 0 else current_tokens

    def extract_model(self, raw_entry: dict, current_model: str | None) -> str | None:
        if raw_entry.get("type") != "assistant":
            return current_model
        model = raw_entry.get("message", {}).get("model")
        if isinstance(model, str) and model:
            return model
        return current_model

    def extract_harness_state(
        self,
        raw_entry: dict,
        current_state: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        updated_state = _update_last_user_message_at(raw_entry, current_state)
        return current_state if updated_state == (current_state or {}) else updated_state


CLAUDE_HARNESS = ClaudeSessionHarness()


class CodexSessionHarness:
    """Codex rollout JSONL adapter."""

    name = "codex"

    async def register_session(
        self,
        *,
        monitor: Any,
        tmux_name: str,
        session_type: str,
        project: str,
        run_dir: Path | None = None,
        seed_message: str = "",
        session_uuid: str | None = None,
        jsonl_path: Path | None = None,
        resolution_dir: Path | None = None,
        bead_id: str | None = None,
    ) -> None:
        sess_dir = resolution_dir
        if sess_dir is None:
            if run_dir is not None:
                sess_dir = run_dir / "sessions"
            elif jsonl_path is not None:
                sess_dir = jsonl_path if jsonl_path.is_dir() else jsonl_path.parent
        await monitor.register(
            tmux_name=tmux_name,
            session_type=session_type,
            project=project,
            jsonl_path=sess_dir,
            bead_id=bead_id,
            seed_message=seed_message,
            session_uuid=session_uuid,
            resolution_dir=sess_dir,
            harness=self.name,
        )

    def resolve_session(
        self,
        *,
        tmux_name: str,
        row: dict | None = None,
        jsonl_path: Path | None = None,
        handshake_text: str | None = None,
    ) -> dict | None:
        _ = row, handshake_text
        if jsonl_path is None:
            return None
        return _link_session_file(
            tmux_name,
            jsonl_path,
            project=(row or {}).get("project"),
        )

    def attach_live_monitoring(
        self,
        *,
        monitor: Any,
        tmux_name: str,
        jsonl_path: Path,
        resolution_dir: Path | None = None,
        reset_offset: bool = False,
        reset_state: bool = False,
    ) -> None:
        _attach_live_monitoring(
            monitor,
            tmux_name=tmux_name,
            jsonl_path=jsonl_path,
            resolution_dir=resolution_dir,
            reset_offset=reset_offset,
            reset_state=reset_state,
        )

    def parse_line(self, line: str) -> dict | list[dict] | None:
        return parse_codex_log_line(line)

    def postprocess_entries(
        self,
        entries: list[dict],
        *,
        session_dir: Path | None = None,
    ) -> list[dict]:
        return postprocess_codex_entries(entries, session_dir=session_dir)

    def extract_message_text(self, raw_entry: dict) -> str:
        return extract_codex_message_text(raw_entry)

    def extract_context_tokens(self, raw_entry: dict, current_tokens: int) -> int:
        return extract_codex_context_tokens(raw_entry, current_tokens)

    def extract_model(self, raw_entry: dict, current_model: str | None) -> str | None:
        return extract_codex_model(raw_entry, current_model)

    def extract_harness_state(
        self,
        raw_entry: dict,
        current_state: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        return extract_codex_harness_state(raw_entry, current_state)


CODEX_HARNESS = CodexSessionHarness()


# auto-ngis4: harness + model are optional (additive) so legacy envelopes
# without those attrs continue to parse and pre-existing callers see the
# same group set. Named groups insulate consumers from positional shifts.
_CROSSTALK_RE = re.compile(
    r'<crosstalk\s+from="(?P<from_>[^"]+)"\s+label="(?P<label>[^"]*)"'
    r'\s+source="(?P<source>[^"]*)"\s+turn="(?P<turn>[^"]*)"'
    r'(?:\s+harness="(?P<harness>[^"]*)")?'
    r'(?:\s+model="(?P<model>[^"]*)")?'
    r'\s+timestamp="(?P<timestamp>[^"]+)">\n(?P<body>.*)\n</crosstalk>',
    re.DOTALL,
)


def _graph_db_path() -> str | None:
    return os.environ.get("GRAPH_DB") or None


def _classify_crosstalk(text: str) -> dict | None:
    stripped = text.strip()
    m = _CROSSTALK_RE.fullmatch(stripped)
    if not m:
        return None
    body = m.group("body")
    if "</crosstalk>" in body:
        return None
    return {
        "from": m.group("from_"),
        "label": m.group("label"),
        "source": m.group("source"),
        "turn": m.group("turn"),
        "timestamp": m.group("timestamp"),
        "harness": m.group("harness") or "",
        "model": m.group("model") or "",
        "message": body,
    }


def _parse_crosstalk_send(command: str, timestamp: str) -> dict | None:
    if "crosstalk/send" not in command:
        return None
    import shlex

    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    payload = None
    for i, tok in enumerate(tokens):
        if tok == "-d" and i + 1 < len(tokens):
            try:
                parsed = json.loads(tokens[i + 1])
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(parsed, dict) and "target" in parsed and "message" in parsed:
                payload = parsed
                break
    if not payload:
        return None
    return {
        "type": "crosstalk",
        "role": "crosstalk",
        "content": payload.get("message", ""),
        "sender": "self",
        "sender_label": "",
        "source_id": "",
        "turn": "",
        "target": payload.get("target", ""),
        "direction": "sent",
        "timestamp": timestamp,
    }


def _parse_graph_comment_cmd(command: str, timestamp: str) -> dict | None:
    m = re.search(r"graph comment\s+(\S+)", command)
    if not m:
        return None
    return {
        "type": "semantic_bash",
        "semantic_type": "comment-added",
        "role": "assistant",
        "source_id": m.group(1),
        "content": "Added comment",
        "timestamp": timestamp,
    }


def _parse_dispatch_approve_cmd(command: str, timestamp: str) -> dict | None:
    m = re.search(r"graph dispatch approve\s+(\S+)", command)
    if not m:
        return None
    return {
        "type": "semantic_bash",
        "semantic_type": "dispatch-approved",
        "role": "assistant",
        "bead_id": m.group(1),
        "content": f"Approved {m.group(1)} for dispatch",
        "timestamp": timestamp,
    }


def _parse_bd_setstate_cmd(command: str, timestamp: str) -> dict | None:
    m = re.search(r"bd set-state\s+(\S+)\s+(\S+=\S+)", command)
    if not m:
        return None
    return {
        "type": "semantic_bash",
        "semantic_type": "state-changed",
        "role": "assistant",
        "bead_id": m.group(1),
        "state": m.group(2),
        "content": f"Set {m.group(2)} on {m.group(1)}",
        "timestamp": timestamp,
    }


def _upconvert_turn_correction(content: str, timestamp: str, tool_id: str = "") -> dict | None:
    """Upconvert ``graph turn-correction suggest --json`` output into a typed
    parser entry the SessionMonitor can persist as a sparse overlay row.

    Bead auto-edec1.1. The CLI prints a single JSON object whose ``type`` is
    ``turn_correction``; we recognize that discriminator, validate the
    replacement text, and emit a ``turn_correction`` entry instead of leaving
    the result as opaque Bash text. Target resolution and hash derivation now
    happen later in SessionMonitor so the agent-facing command can stay
    one-shot and only remit the corrected text.
    """
    if not isinstance(content, str):
        return None
    stripped = content.strip()
    if not stripped or stripped[0] != "{":
        return None
    # Cheap discriminator gate so we don't try to JSON-decode every Bash line.
    if "turn_correction" not in stripped:
        return None
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    return _build_turn_correction_entry(payload, timestamp, tool_id=tool_id)


def _build_turn_correction_entry(
    payload: dict,
    timestamp: str,
    *,
    tool_id: str = "",
) -> dict | None:
    if not isinstance(payload, dict) or payload.get("type") != "turn_correction":
        return None
    corrected = payload.get("corrected_text")
    if not isinstance(corrected, str):
        return None
    entry: dict[str, Any] = {
        "type": "turn_correction",
        "role": "tool",
        "timestamp": timestamp,
        "corrected_text": corrected,
    }
    target = payload.get("target_message_id")
    if isinstance(target, str) and target:
        entry["target_message_id"] = target
    sha = payload.get("original_sha256")
    if isinstance(sha, str) and sha:
        entry["original_sha256"] = sha
    if tool_id:
        entry["tool_id"] = tool_id
    mode = payload.get("mode")
    if isinstance(mode, str) and mode:
        entry["mode"] = mode
    reason = payload.get("reason")
    if isinstance(reason, str) and reason:
        entry["reason"] = reason
    confidence = payload.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        entry["confidence"] = float(confidence)
    return entry


def _upconvert_turn_correction_command(
    command: str,
    timestamp: str,
    *,
    tool_id: str = "",
) -> dict | None:
    """Fallback for blank exec_command completions.

    Some live Codex ``exec_command_end`` envelopes arrive with empty captured
    output even though the immediate tool return contained the JSON payload.
    For the canonical one-shot command, the corrected replacement string and
    optional metadata are already present in the command text, so we can
    synthesize the same typed event without depending on stdout capture.
    """
    if not isinstance(command, str) or not command.strip():
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens:
        return None
    while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return None
    for op in ("|", "&&", ";"):
        if op in tokens:
            tokens = tokens[:tokens.index(op)]
            break
    if not tokens:
        return None
    prefix_len = 0
    if tokens[0] == "graph":
        prefix_len = 1
    elif (
        len(tokens) >= 3
        and tokens[0].startswith("python")
        and tokens[1] == "-m"
        and tokens[2] == "tools.graph"
    ):
        prefix_len = 3
    else:
        return None
    if tokens[prefix_len:prefix_len + 2] != ["turn-correction", "suggest"]:
        return None
    args = tokens[prefix_len + 2:]
    corrected_text: str | None = None
    mode: str | None = None
    reason: str | None = None
    confidence: float | None = None
    uses_stdin = False
    saw_json = False
    idx = 0
    while idx < len(args):
        token = args[idx]
        if token == "--json":
            saw_json = True
            idx += 1
            continue
        if token == "--stdin":
            uses_stdin = True
            idx += 1
            continue
        if token == "--mode" and idx + 1 < len(args):
            mode = args[idx + 1]
            idx += 2
            continue
        if token.startswith("--mode="):
            mode = token.split("=", 1)[1]
            idx += 1
            continue
        if token == "--reason" and idx + 1 < len(args):
            reason = args[idx + 1]
            idx += 2
            continue
        if token.startswith("--reason="):
            reason = token.split("=", 1)[1]
            idx += 1
            continue
        if token == "--confidence" and idx + 1 < len(args):
            try:
                confidence = float(args[idx + 1])
            except (TypeError, ValueError):
                return None
            idx += 2
            continue
        if token.startswith("--confidence="):
            try:
                confidence = float(token.split("=", 1)[1])
            except (TypeError, ValueError):
                return None
            idx += 1
            continue
        if token.startswith("--"):
            return None
        if corrected_text is not None:
            return None
        corrected_text = token
        idx += 1
    if not saw_json or uses_stdin or corrected_text is None:
        return None
    payload: dict[str, Any] = {
        "type": "turn_correction",
        "corrected_text": corrected_text,
    }
    if mode:
        payload["mode"] = mode
    if reason:
        payload["reason"] = reason
    if confidence is not None:
        payload["confidence"] = confidence
    return _build_turn_correction_entry(payload, timestamp, tool_id=tool_id)


def _upconvert_graph_result(content: str, timestamp: str, tool_id: str = "") -> dict | None:
    if not isinstance(content, str):
        return None
    base = {"type": "semantic_bash", "role": "tool", "timestamp": timestamp}
    if tool_id:
        base["tool_id"] = tool_id
    m = re.search(r"^\s*\u2713 Note saved \(src:([a-f0-9-]+)\)", content, re.MULTILINE)
    if m:
        return {
            **base,
            "semantic_type": "note-created",
            "source_id": m.group(1),
            "content": content.strip()[:100],
        }
    m = re.search(r"^\s*\u2713 Captured:\s*([a-f0-9-]+)", content, re.MULTILINE)
    if m:
        return {
            **base,
            "semantic_type": "thought-captured",
            "source_id": m.group(1),
            "content": content.strip()[:100],
        }
    m = re.search(r"^\s*\u2713 Comment added.*?id:([a-f0-9-]+)", content, re.MULTILINE)
    if m:
        return {
            **base,
            "semantic_type": "comment-added",
            "comment_id": m.group(1),
            "content": content.strip()[:100],
        }
    return None


def _enrich_semantic_tile(entry: dict) -> None:
    source_id = entry.get("source_id") or entry.get("comment_id")
    if not source_id:
        return
    db_path = _graph_db_path()
    try:
        if db_path:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        else:
            default = Path(__file__).resolve().parents[2] / "data" / "graph.db"
            if not default.exists():
                return
            conn = sqlite3.connect(f"file:{default}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except (sqlite3.OperationalError, OSError):
        return
    try:
        row = conn.execute(
            "SELECT id, title, metadata FROM sources WHERE id = ?",
            (source_id,),
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT id, title, metadata FROM sources WHERE id LIKE ? LIMIT 1",
                (f"{source_id}%",),
            ).fetchone()
        if not row:
            return
        meta: dict[str, Any] = {}
        try:
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        if entry.get("semantic_type") == "comment-added" and meta.get("parent_source_id"):
            parent_row = conn.execute(
                "SELECT id, title, metadata FROM sources WHERE id = ?",
                (meta["parent_source_id"],),
            ).fetchone()
            if not parent_row:
                parent_row = conn.execute(
                    "SELECT id, title, metadata FROM sources WHERE id LIKE ? LIMIT 1",
                    (f"{meta['parent_source_id']}%",),
                ).fetchone()
            if parent_row:
                row = parent_row
                try:
                    meta = json.loads(parent_row["metadata"]) if parent_row["metadata"] else {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}
        title = (row["title"] or "").lstrip("#").strip()
        tags = meta.get("tags", [])
        content_row = conn.execute(
            "SELECT content FROM thoughts WHERE source_id = ? ORDER BY turn_number LIMIT 1",
            (row["id"],),
        ).fetchone()
        preview = ""
        if content_row and content_row["content"]:
            lines = content_row["content"].split("\n")
            body_lines = [l for l in lines if not l.startswith("#") and l.strip()]
            preview = " ".join(body_lines)[:120]
        entry["title"] = title
        entry["preview"] = preview
        entry["tags"] = tags if isinstance(tags, list) else []
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        pass
    finally:
        conn.close()


def _classify_system_message(text: str) -> dict | None:
    stripped = text.strip()
    if "<task-notification>" in stripped:
        summary = ""
        status = ""
        m_summary = re.search(r"<summary>(.*?)</summary>", stripped, re.DOTALL)
        m_status = re.search(r"<status>(.*?)</status>", stripped, re.DOTALL)
        if m_summary:
            summary = m_summary.group(1).strip()
        if m_status:
            status = m_status.group(1).strip()
        label = summary if summary else f"Task {status}" if status else "Task notification"
        return {"summary": label, "tag": "task-notification"}
    if "<system-reminder>" in stripped:
        m = re.search(r"<system-reminder>(.*?)</system-reminder>", stripped, re.DOTALL)
        body = m.group(1).strip() if m else stripped
        return {"summary": "System reminder", "tag": "system-reminder", "body": body}
    if "<local-command-stdout>" in stripped:
        return {"summary": "Command output", "tag": "local-command-stdout"}
    if "<command-name>" in stripped:
        m = re.search(r"<command-name>(.*?)</command-name>", stripped, re.DOTALL)
        name = m.group(1).strip() if m else "command"
        return {"summary": f"Command: {name}", "tag": "command-name"}
    return None


def parse_claude_log_line(line: str) -> dict | list[dict] | None:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None

    entry_type = raw.get("type")
    timestamp = raw.get("timestamp", "")
    is_sidechain = raw.get("isSidechain", False)
    identity: dict = {}
    if (msg_uuid := raw.get("uuid")) is not None:
        identity["message_id"] = msg_uuid
    if (parent := raw.get("parentUuid")) is not None:
        identity["parent_uuid"] = parent

    if raw.get("isCompactSummary") or raw.get("isVisibleInTranscriptOnly"):
        message = raw.get("message", {})
        content_raw = message.get("content", "")
        text = ""
        if isinstance(content_raw, str):
            text = content_raw
        elif isinstance(content_raw, list):
            text = "".join(
                b.get("text", "") for b in content_raw
                if isinstance(b, dict) and b.get("type") == "text"
            )
        if not text:
            return None
        return {
            "type": "compact_summary",
            "role": "compact_summary",
            "content": text,
            "timestamp": timestamp,
        }

    if entry_type == "queue-operation":
        op = raw.get("operation")
        content = raw.get("content", "")
        if op == "enqueue" and content and not content.startswith("<task-notification"):
            ct = _classify_crosstalk(content)
            if ct:
                return {
                    "type": "crosstalk",
                    "role": "crosstalk",
                    "content": ct["message"],
                    "sender": ct["from"],
                    "sender_label": ct["label"],
                    "source_id": ct["source"],
                    "turn": ct["turn"],
                    "timestamp": timestamp,
                    "queued": True,
                }
            return {"type": "user", "content": content, "timestamp": timestamp, "queued": True}
        return None

    if entry_type in ("progress", "system") or is_sidechain:
        return None

    message = raw.get("message", {})
    content_raw = message.get("content", "")

    if entry_type == "user":
        text = ""
        tool_results: list[dict] = []
        if isinstance(content_raw, str):
            text = content_raw
        elif isinstance(content_raw, list):
            for block in content_raw:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    text += block.get("text", "")
                elif btype == "tool_result":
                    result_content = block.get("content", "")
                    if isinstance(result_content, list):
                        result_content = "".join(
                            b.get("text", "") for b in result_content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    tool_use_id = block.get("tool_use_id", "")
                    tool_results.append({
                        "type": "tool_result",
                        "role": "tool",
                        "tool_id": tool_use_id,
                        "content": result_content,
                        "is_error": block.get("is_error", False),
                        "timestamp": timestamp,
                    })
                    tc = _upconvert_turn_correction(
                        result_content, timestamp, tool_id=tool_use_id,
                    )
                    if tc:
                        tool_results.append(tc)
                    sem = _upconvert_graph_result(result_content, timestamp, tool_id=tool_use_id)
                    if sem:
                        _enrich_semantic_tile(sem)
                        tool_results.append(sem)
        entries: list[dict] = []
        if text:
            ct = _classify_crosstalk(text)
            if ct:
                entries.append({
                    "type": "crosstalk",
                    "role": "crosstalk",
                    "content": ct["message"],
                    "sender": ct["from"],
                    "sender_label": ct["label"],
                    "source_id": ct["source"],
                    "turn": ct["turn"],
                    "timestamp": timestamp,
                })
            elif (sys_info := _classify_system_message(text)):
                sys_entry = {
                    "type": "system",
                    "role": "system",
                    "content": sys_info["summary"],
                    "tag": sys_info["tag"],
                    "timestamp": timestamp,
                }
                if sys_info.get("body"):
                    sys_entry["body"] = sys_info["body"]
                entries.append(sys_entry)
            else:
                entries.append({
                    "type": "user",
                    "role": "user",
                    "content": text,
                    "timestamp": timestamp,
                    **identity,
                })
        entries.extend(tool_results)
        if not entries:
            return None
        return entries if len(entries) > 1 else entries[0]

    if entry_type == "assistant" and isinstance(content_raw, list):
        blocks: list[dict] = []
        for block in content_raw:
            btype = block.get("type", "")
            if btype == "text":
                text = block.get("text", "").strip()
                if text:
                    blocks.append({
                        "type": "assistant_text",
                        "role": "assistant",
                        "content": text,
                        "timestamp": timestamp,
                        **identity,
                    })
            elif btype == "tool_use":
                tool_input = block.get("input", {})
                tool_name = block.get("name", "?")
                if tool_name == "Bash":
                    cmd = tool_input.get("command") or ""
                    if "crosstalk/send" in cmd:
                        ct_entry = _parse_crosstalk_send(cmd, timestamp)
                        if ct_entry:
                            blocks.append(ct_entry)
                            continue
                    if "graph comment" in cmd and "integrate" not in cmd:
                        parsed = _parse_graph_comment_cmd(cmd, timestamp)
                        if parsed:
                            blocks.append(parsed)
                            continue
                    if "graph dispatch approve" in cmd:
                        parsed = _parse_dispatch_approve_cmd(cmd, timestamp)
                        if parsed:
                            blocks.append(parsed)
                            continue
                    if "bd set-state" in cmd:
                        parsed = _parse_bd_setstate_cmd(cmd, timestamp)
                        if parsed:
                            blocks.append(parsed)
                            continue
                blocks.append({
                    "type": "tool_use",
                    "role": "assistant",
                    "tool_name": tool_name,
                    "tool_id": block.get("id", ""),
                    "input": tool_input,
                    "timestamp": timestamp,
                })
            elif btype == "thinking":
                thinking = block.get("thinking", "").strip()
                if thinking:
                    blocks.append({
                        "type": "thinking",
                        "role": "assistant",
                        "content": thinking,
                        "timestamp": timestamp,
                    })
        return blocks if blocks else None

    if entry_type == "tool_result":
        tool_id = raw.get("toolUseId", "")
        result_content = ""
        if isinstance(content_raw, str):
            result_content = content_raw
        elif isinstance(content_raw, list):
            for block in content_raw:
                if isinstance(block, dict) and block.get("type") == "text":
                    result_content += block.get("text", "")
        if not result_content:
            return {
                "type": "tool_result",
                "role": "tool",
                "tool_id": tool_id,
                "content": "",
                "is_error": raw.get("is_error", False),
                "timestamp": timestamp,
            }
        base_result = {
            "type": "tool_result",
            "role": "tool",
            "tool_id": tool_id,
            "content": result_content,
            "is_error": raw.get("is_error", False),
            "timestamp": timestamp,
        }
        tc = _upconvert_turn_correction(result_content, timestamp, tool_id=tool_id)
        sem = _upconvert_graph_result(result_content, timestamp, tool_id=tool_id)
        if tc and sem:
            _enrich_semantic_tile(sem)
            return [base_result, tc, sem]
        if tc:
            return [base_result, tc]
        if sem:
            _enrich_semantic_tile(sem)
            return [base_result, sem]
        return base_result

    return None


def enrich_claude_entries(entries: list[dict], session_dir: Path | None = None) -> None:
    if session_dir is None:
        return
    agent_descriptions: dict[str, str] = {}
    claimed: set[str] = set()
    for entry in entries:
        if entry.get("type") == "tool_use" and entry.get("tool_name") == "Agent":
            tool_id = entry.get("tool_id", "")
            desc = entry.get("input", {}).get("description", "")
            if tool_id and desc:
                agent_descriptions[tool_id] = desc
        elif entry.get("type") == "tool_result" and entry.get("tool_id"):
            tool_id = entry["tool_id"]
            if tool_id not in agent_descriptions:
                continue
            target_desc = agent_descriptions[tool_id]
            subagents_dir = session_dir / "subagents"
            if not subagents_dir.is_dir():
                continue
            for meta_path in sorted(subagents_dir.glob("*.meta.json")):
                if str(meta_path) in claimed:
                    continue
                try:
                    meta = json.loads(meta_path.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                if meta.get("description") == target_desc:
                    claimed.add(str(meta_path))
                    jsonl_path = meta_path.with_suffix("").with_suffix(".jsonl")
                    if jsonl_path.exists():
                        from tools.dashboard.session_monitor import count_tool_uses

                        count = count_tool_uses(jsonl_path)
                        if count > 0:
                            entry["tool_calls"] = count
                    break


def dedup_claude_entries(entries: list[dict]) -> list[dict]:
    result = []
    last_enqueue_content = None
    for entry in entries:
        if entry.get("queued"):
            last_enqueue_content = entry.get("content", "").strip()
            result.append(entry)
        elif (
            entry.get("type") in ("user", "crosstalk")
            and last_enqueue_content
            and entry.get("content", "").strip() == last_enqueue_content
        ):
            last_enqueue_content = None
        else:
            result.append(entry)
    return result


def postprocess_claude_entries(
    entries: list[dict],
    *,
    session_dir: Path | None = None,
) -> list[dict]:
    processed = dedup_claude_entries(entries)
    enrich_claude_entries(processed, session_dir=session_dir)
    return processed


def parse_plan_snapshot(arguments: str) -> list[dict] | None:
    """Best-effort parser for Codex-style ``update_plan`` arguments.

    Not wired into production yet; included now to anchor the shared todo
    abstraction and to make the intended Codex primitive explicit.
    """

    try:
        payload = json.loads(arguments)
    except (TypeError, json.JSONDecodeError):
        return None
    plan = payload.get("plan")
    if not isinstance(plan, list):
        return None
    out: list[dict] = []
    for item in plan:
        if not isinstance(item, dict):
            continue
        step = str(item.get("step") or "").strip()
        status = str(item.get("status") or "").strip()
        if not step:
            continue
        out.append({"subject": step, "status": status or "pending"})
    return out or None


def _codex_infer_read_line_count(command: str) -> int | None:
    if not command:
        return None
    range_match = re.search(r"\bsed\s+-n\s+['\"]?(\d+)\s*,\s*(\d+)p['\"]?", command)
    if range_match:
        start = int(range_match.group(1))
        end = int(range_match.group(2))
        return (end - start + 1) if end >= start else None
    single_match = re.search(r"\bsed\s+-n\s+['\"]?(\d+)p['\"]?", command)
    if single_match:
        return 1
    return None


def _codex_is_ripgrep_search(command: str) -> bool:
    return bool(re.match(r"^\s*rg(?:\s|$)", command))


def _codex_semantic_op_from_parsed(parsed: dict) -> dict | None:
    if not isinstance(parsed, dict):
        return None
    parsed_type = parsed.get("type")
    if parsed_type == "read":
        return {
            "tool_name": "Read",
            "input": {"file_path": parsed.get("path") or parsed.get("name") or ""},
            "line_count": _codex_infer_read_line_count(str(parsed.get("cmd") or "")),
        }
    if parsed_type == "search" and _codex_is_ripgrep_search(str(parsed.get("cmd") or "")):
        return {
            "tool_name": "Grep",
            "input": {
                "pattern": parsed.get("query") or parsed.get("cmd") or "",
                "path": parsed.get("path") or "",
            },
            "line_count": None,
        }
    return None


def _codex_take_leading_lines(text: str, line_count: int) -> tuple[str, str] | None:
    if line_count < 0:
        return None
    if line_count == 0:
        return "", text
    idx = 0
    seen = 0
    while seen < line_count:
        nl = text.find("\n", idx)
        if nl == -1:
            if idx >= len(text):
                return None
            idx = len(text)
            seen += 1
            break
        idx = nl + 1
        seen += 1
    return text[:idx], text[idx:]


def _codex_split_semantic_output(content: str, ops: list[dict]) -> list[str] | None:
    if not content:
        return None
    if len(ops) == 1:
        return [content]

    if all(op.get("tool_name") == "Read" and op.get("line_count") is not None for op in ops):
        parts: list[str] = []
        rest = content
        for op in ops:
            chunk = _codex_take_leading_lines(rest, int(op["line_count"]))
            if chunk is None:
                return None
            head, rest = chunk
            parts.append(head)
        if rest.strip():
            return None
        return parts

    read_prefix = 0
    while (
        read_prefix < len(ops)
        and ops[read_prefix].get("tool_name") == "Read"
        and ops[read_prefix].get("line_count") is not None
    ):
        read_prefix += 1
    if read_prefix > 0 and read_prefix == len(ops) - 1 and ops[-1].get("tool_name") == "Grep":
        parts = []
        rest = content
        for idx in range(read_prefix):
            chunk = _codex_take_leading_lines(rest, int(ops[idx]["line_count"]))
            if chunk is None:
                return None
            head, rest = chunk
            parts.append(head)
        parts.append(rest)
        return parts

    return None


def _build_codex_semantic_tool_use(
    entry: dict,
    op: dict,
    tool_id: str,
    *,
    preserve_timestamp: bool,
) -> dict:
    timestamp = entry.get("timestamp", "") if preserve_timestamp else ""
    role = entry.get("role") or "assistant"
    return {
        "type": "tool_use",
        "role": role,
        "tool_name": op["tool_name"],
        "tool_id": tool_id,
        "input": dict(op["input"]),
        "timestamp": timestamp,
        "semantic_from_exec": True,
    }


def _build_codex_bash_tool_use(entry: dict) -> dict:
    tool_use = dict(entry)
    tool_use["tool_name"] = "Bash"
    return tool_use


def _build_codex_semantic_result(
    entry: dict,
    op: dict,
    tool_id: str,
    parsed: dict,
    content: str,
) -> dict:
    result = dict(entry)
    result["tool_id"] = tool_id
    result["content"] = content
    result["parsed_cmd"] = [parsed]
    result["semantic_from_exec"] = True
    line_count = op.get("line_count")
    if line_count is not None:
        result["line_count"] = line_count
    else:
        result.pop("line_count", None)
    return result


def _codex_semantic_transform(entry: dict) -> dict | None:
    parsed_cmd = entry.get("parsed_cmd")
    if not isinstance(parsed_cmd, list) or not parsed_cmd:
        return None
    ops: list[dict] = []
    for parsed in parsed_cmd:
        op = _codex_semantic_op_from_parsed(parsed)
        if op is None:
            return None
        ops.append(op)
    split_content = _codex_split_semantic_output(str(entry.get("content") or ""), ops)
    if len(ops) > 1 and split_content is None:
        return None
    if split_content is None:
        split_content = [str(entry.get("content") or "")]
    results = [
        _build_codex_semantic_result(
            entry,
            ops[idx],
            entry["tool_id"] if idx == 0 else f'{entry["tool_id"]}#{idx + 1}',
            parsed_cmd[idx],
            split_content[idx],
        )
        for idx in range(len(ops))
    ]
    return {"ops": ops, "results": results}


def postprocess_codex_entries(
    entries: list[dict],
    *,
    session_dir: Path | None = None,
) -> list[dict]:
    state = _codex_session_progress_state(session_dir)
    tool_names = state["tool_names"]
    exec_sessions = state["exec_sessions"]
    write_calls = state["write_calls"]
    completed_tools = state["completed_tools"]

    normalized: list[dict] = []
    patch_results: dict[str, dict] = {}

    for entry in entries:
        if entry.get("type") == "tool_use" and entry.get("tool_id"):
            tool_id = entry.get("tool_id") or ""
            tool_name = str(entry.get("tool_name") or "")
            tool_names[tool_id] = tool_name
            if tool_name == "write_stdin":
                session_id = (entry.get("input") or {}).get("session_id")
                if session_id not in (None, ""):
                    write_calls[tool_id] = str(session_id)
                continue
            normalized.append(entry)
            continue

        if entry.get("type") == "tool_result" and entry.get("tool_id"):
            tool_id = entry.get("tool_id") or ""
            result_kind = entry.get("result_kind")

            if result_kind == "patch_apply_end":
                patch_results[tool_id] = entry
                normalized.append(entry)
                continue

            if result_kind == "custom_tool_call_output":
                normalized.append(entry)
                continue

            if result_kind == "exec_command":
                process_id = str(entry.get("process_id") or "")
                if process_id:
                    exec_sessions[process_id] = tool_id
                if entry.get("status") != "running":
                    completed_tools.add(tool_id)
                normalized.append(entry)
                continue

            if result_kind == "function_call_output":
                tool_name = str(tool_names.get(tool_id) or "")
                if tool_name == "exec_command":
                    if tool_id in completed_tools:
                        continue
                    process_id = str(entry.get("process_id") or "")
                    if process_id:
                        exec_sessions[process_id] = tool_id
                    progress = _build_codex_exec_progress_result(
                        entry,
                        tool_id=tool_id,
                        process_id=process_id,
                    )
                    if progress:
                        if progress.get("status") != "running":
                            completed_tools.add(tool_id)
                        normalized.append(progress)
                    continue
                write_session_id = str(write_calls.get(tool_id) or "")
                if write_session_id:
                    parent_tool_id = str(exec_sessions.get(write_session_id) or "")
                    if parent_tool_id and parent_tool_id not in completed_tools:
                        progress = _build_codex_exec_progress_result(
                            entry,
                            tool_id=parent_tool_id,
                            process_id=write_session_id,
                        )
                        if progress:
                            if progress.get("status") != "running":
                                completed_tools.add(parent_tool_id)
                            normalized.append(progress)
                        continue
                    continue

        normalized.append(entry)

    if patch_results:
        normalized = [
            entry for entry in normalized
            if not (
                entry.get("type") == "tool_result"
                and entry.get("result_kind") == "custom_tool_call_output"
                and entry.get("tool_id") in patch_results
            )
        ]

    use_ids = {
        entry.get("tool_id")
        for entry in normalized
        if entry.get("type") == "tool_use" and entry.get("tool_name") == "exec_command" and entry.get("tool_id")
    }
    transforms: dict[str, dict] = {}
    for entry in normalized:
        if entry.get("type") != "tool_result" or entry.get("result_kind") != "exec_command":
            continue
        tool_id = entry.get("tool_id") or ""
        if not tool_id:
            continue
        transform = _codex_semantic_transform(entry)
        if transform is None:
            continue
        transform["use_in_entries"] = tool_id in use_ids
        transforms[tool_id] = transform

    out: list[dict] = []
    for entry in normalized:
        tool_id = entry.get("tool_id") or ""
        transform = transforms.get(tool_id)
        if (
            entry.get("type") == "tool_use"
            and entry.get("tool_name") == "exec_command"
        ):
            if transform is None:
                out.append(_build_codex_bash_tool_use(entry))
                continue
            ops = transform["ops"]
            out.append(
                _build_codex_semantic_tool_use(
                    entry,
                    ops[0],
                    tool_id,
                    preserve_timestamp=True,
                ),
            )
            for idx, op in enumerate(ops[1:], start=2):
                out.append(
                    _build_codex_semantic_tool_use(
                        entry,
                        op,
                        f"{tool_id}#{idx}",
                        preserve_timestamp=True,
                    ),
                )
            continue
        if (
            entry.get("type") == "tool_result"
            and entry.get("result_kind") == "exec_command"
            and transform is not None
        ):
            if not transform["use_in_entries"]:
                ops = transform["ops"]
                out.append(
                    _build_codex_semantic_tool_use(
                        entry,
                        ops[0],
                        tool_id,
                        preserve_timestamp=False,
                    ),
                )
                for idx, op in enumerate(ops[1:], start=2):
                    out.append(
                        _build_codex_semantic_tool_use(
                            entry,
                            op,
                            f"{tool_id}#{idx}",
                            preserve_timestamp=False,
                        ),
                    )
            out.extend(transform["results"])
            continue
        out.append(entry)
    return out


def _extract_codex_text_blocks(blocks: Any) -> str:
    if isinstance(blocks, str):
        return blocks.strip()
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"input_text", "output_text", "text"}:
            text = str(block.get("text") or "")
            if text:
                parts.append(text)
    return "".join(parts).strip()


def _is_codex_visible_message(role: str, text: str) -> bool:
    if role not in {"user", "assistant"}:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.startswith("<environment_context>") and stripped.endswith("</environment_context>"):
        return False
    return True


def _extract_codex_compaction_message(message: Any) -> str:
    if isinstance(message, str):
        return message.strip()
    return _extract_codex_text_blocks(message)


def _format_codex_compaction_history_item(item: Any) -> str | None:
    if not isinstance(item, dict) or item.get("type") != "message":
        return None
    role = str(item.get("role") or "")
    text = _extract_codex_text_blocks(item.get("content"))
    if not _is_codex_visible_message(role, text):
        return None
    if role == "user":
        ct = _classify_crosstalk(text)
        if ct:
            source = ct.get("label") or ct.get("from") or "crosstalk"
            message = str(ct.get("message") or "").strip()
            return f"Crosstalk from {source}: {message}" if message else f"Crosstalk from {source}"
        sys_info = _classify_system_message(text)
        if sys_info:
            body = str(sys_info.get("body") or "").strip()
            summary = str(sys_info.get("summary") or "System").strip()
            return f"System: {summary}\n{body}" if body else f"System: {summary}"
        return f"User: {text}"
    if role == "assistant":
        return f"Assistant: {text}"
    return None


def _build_codex_compact_summary(payload: dict, timestamp: str) -> dict:
    summary = _extract_codex_compaction_message(payload.get("message"))
    if not summary:
        history = payload.get("replacement_history")
        if isinstance(history, list):
            parts = []
            for item in history:
                rendered = _format_codex_compaction_history_item(item)
                if rendered:
                    parts.append(rendered)
            summary = "\n\n".join(parts).strip()
    if not summary:
        summary = "Context compacted."
    return {
        "type": "compact_summary",
        "role": "compact_summary",
        "content": summary,
        "timestamp": timestamp,
    }


def _parse_codex_call_args(arguments: Any) -> dict:
    if isinstance(arguments, dict):
        return arguments
    if not isinstance(arguments, str) or not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return {"arguments": arguments}
    return parsed if isinstance(parsed, dict) else {"arguments": arguments}


def _codex_relpath(path: str) -> str:
    text = str(path or "")
    prefix = "/workspace/repo/"
    if text.startswith(prefix):
        return text[len(prefix):]
    return text


def _parse_codex_patch_input(text: str) -> dict:
    if not text:
        return {"description": "Patch"}
    matches = list(_CODEX_PATCH_FILE_RE.finditer(text))
    files = [_codex_relpath(match.group(2).strip()) for match in matches if match.group(2).strip()]
    unique_files: list[str] = []
    seen: set[str] = set()
    for file_path in files:
        if file_path in seen:
            continue
        seen.add(file_path)
        unique_files.append(file_path)
    result: dict[str, Any] = {}
    if len(unique_files) == 1:
        result["description"] = unique_files[0]
        result["file_path"] = unique_files[0]
    elif unique_files:
        result["description"] = f"Patched {len(unique_files)} files"
    else:
        result["description"] = "Patch"
    result["files"] = unique_files
    result["patch"] = text
    return result


def _parse_codex_tool_output_metadata(output: str) -> dict:
    text = str(output or "")
    data: dict[str, Any] = {}
    if not text:
        return data
    session_match = _CODEX_TOOL_OUTPUT_SESSION_RE.search(text)
    exit_match = _CODEX_TOOL_OUTPUT_EXIT_RE.search(text)
    if session_match:
        data["process_id"] = session_match.group(1)
        if not exit_match:
            data["status"] = "running"
    if exit_match:
        try:
            data["exit_code"] = int(exit_match.group(1))
        except ValueError:
            pass
        data["status"] = "completed"
    time_match = _CODEX_TOOL_OUTPUT_TIME_RE.search(text)
    if time_match:
        try:
            data["duration_seconds"] = float(time_match.group(1))
        except ValueError:
            pass
    body = text
    body_split = _CODEX_TOOL_OUTPUT_BODY_RE.split(text, maxsplit=1)
    if len(body_split) == 2:
        body = body_split[1]
    data["stdout"] = body
    return data


def _parse_codex_custom_tool_output(payload: dict, timestamp: str) -> dict:
    output_text = str(payload.get("output") or "")
    content = output_text
    is_error = False
    duration_seconds = None
    try:
        parsed = json.loads(output_text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        content = str(parsed.get("output") or "")
        metadata = parsed.get("metadata") or {}
        if isinstance(metadata, dict):
            exit_code = metadata.get("exit_code")
            is_error = bool(exit_code not in (None, 0))
            try:
                duration_seconds = float(metadata.get("duration_seconds"))
            except (TypeError, ValueError):
                duration_seconds = None
    return {
        "type": "tool_result",
        "role": "tool",
        "tool_id": payload.get("call_id") or "",
        "content": content,
        "is_error": is_error,
        "timestamp": timestamp,
        "result_kind": "custom_tool_call_output",
        "duration_seconds": duration_seconds,
    }


def _parse_codex_patch_apply_end(payload: dict, timestamp: str) -> dict:
    stdout = str(payload.get("stdout") or "")
    stderr = str(payload.get("stderr") or "")
    changes = payload.get("changes") or {}
    changed_files = []
    if isinstance(changes, dict):
        for path in changes.keys():
            changed_files.append(_codex_relpath(str(path)))
    return {
        "type": "tool_result",
        "role": "tool",
        "tool_id": payload.get("call_id") or "",
        "content": stdout or stderr,
        "is_error": not bool(payload.get("success")),
        "timestamp": timestamp,
        "result_kind": "patch_apply_end",
        "status": str(payload.get("status") or ""),
        "stdout": stdout,
        "stderr": stderr,
        "changed_files": changed_files,
    }


def _codex_session_scope(session_dir: Path | None) -> str:
    if session_dir is None:
        return "__default__"
    try:
        return str(session_dir.resolve())
    except OSError:
        return str(session_dir)


def _codex_session_progress_state(session_dir: Path | None) -> dict[str, dict[str, str] | set[str]]:
    if session_dir is None:
        return {
            "tool_names": {},
            "exec_sessions": {},
            "write_calls": {},
            "completed_tools": set(),
        }
    scope = _codex_session_scope(session_dir)
    state = _CODEX_SESSION_PROGRESS_STATE.get(scope)
    if state is None:
        state = {
            "tool_names": {},
            "exec_sessions": {},
            "write_calls": {},
            "completed_tools": set(),
        }
        _CODEX_SESSION_PROGRESS_STATE[scope] = state
    return state


def _build_codex_exec_progress_result(
    entry: dict,
    *,
    tool_id: str,
    process_id: str,
) -> dict | None:
    stdout = str(entry.get("stdout") or "")
    if not stdout and not process_id:
        return None
    status = str(entry.get("status") or "")
    exit_code = entry.get("exit_code")
    if status in ("completed", "failed") or exit_code is not None:
        result_status = status if status in ("completed", "failed") else "completed"
    elif process_id:
        result_status = "running"
    else:
        result_status = "completed"
    is_error = bool(entry.get("is_error"))
    if exit_code not in (None, ""):
        try:
            is_error = int(exit_code) != 0
        except (TypeError, ValueError):
            is_error = bool(exit_code)
    return {
        "type": "tool_result",
        "role": "tool",
        "tool_id": tool_id,
        "content": stdout,
        "is_error": is_error,
        "timestamp": entry.get("timestamp") or "",
        "result_kind": "exec_command",
        "status": result_status,
        "exit_code": exit_code,
        "cwd": entry.get("cwd") or "",
        "command": entry.get("command") or "",
        "parsed_cmd": entry.get("parsed_cmd") or [],
        "duration_seconds": entry.get("duration_seconds"),
        "stdout": stdout,
        "stderr": "",
        "process_id": process_id,
    }


def _codex_command_text(payload: dict, inp: dict | None = None) -> str:
    if inp and inp.get("cmd"):
        return str(inp["cmd"])
    command = payload.get("command")
    if isinstance(command, list):
        if len(command) >= 3 and str(command[0]).endswith("bash") and command[1] == "-lc":
            return str(command[2])
        return " ".join(str(part) for part in command)
    if isinstance(command, str):
        return command
    return ""


def _codex_duration_seconds(duration: Any) -> float | None:
    if not isinstance(duration, dict):
        return None
    try:
        secs = float(duration.get("secs") or 0)
        nanos = float(duration.get("nanos") or 0)
    except (TypeError, ValueError):
        return None
    return secs + nanos / 1_000_000_000


def _parse_codex_exec_end(payload: dict, timestamp: str) -> dict | list[dict] | None:
    tool_id = payload.get("call_id") or ""
    output = (
        payload.get("aggregated_output")
        or payload.get("formatted_output")
        or payload.get("stdout")
        or payload.get("stderr")
        or ""
    )
    command = _codex_command_text(payload)
    exit_code = payload.get("exit_code")
    result = {
        "type": "tool_result",
        "role": "tool",
        "tool_id": tool_id,
        "content": output,
        "is_error": bool(exit_code not in (None, 0)),
        "timestamp": timestamp,
        "result_kind": "exec_command",
        "exit_code": exit_code,
        "status": payload.get("status") or "",
        "cwd": payload.get("cwd") or "",
        "command": command,
        "parsed_cmd": payload.get("parsed_cmd") or [],
        "duration_seconds": _codex_duration_seconds(payload.get("duration")),
        "stdout": payload.get("stdout") or "",
        "stderr": payload.get("stderr") or "",
        "process_id": payload.get("process_id") or "",
    }
    tc = _upconvert_turn_correction(output, timestamp, tool_id=tool_id)
    if tc is None:
        tc = _upconvert_turn_correction_command(command, timestamp, tool_id=tool_id)
    sem = _upconvert_graph_result(output, timestamp, tool_id=tool_id)
    if tc and sem:
        _enrich_semantic_tile(sem)
        return [result, tc, sem]
    if tc:
        return [result, tc]
    if sem:
        _enrich_semantic_tile(sem)
        return [result, sem]
    return result


def parse_codex_log_line(line: str) -> dict | list[dict] | None:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None

    timestamp = raw.get("timestamp", "")
    payload = raw.get("payload") or {}
    if not isinstance(payload, dict):
        return None
    entry_type = raw.get("type")

    if entry_type == "compacted":
        return _build_codex_compact_summary(payload, timestamp)

    if entry_type == "event_msg":
        event_type = payload.get("type")
        if event_type == "user_message":
            text = str(payload.get("message") or "")
            if text:
                ct = _classify_crosstalk(text)
                if ct:
                    return {
                        "type": "crosstalk",
                        "role": "crosstalk",
                        "content": ct["message"],
                        "sender": ct["from"],
                        "sender_label": ct["label"],
                        "source_id": ct["source"],
                        "turn": ct["turn"],
                        "timestamp": timestamp,
                    }
                sys_info = _classify_system_message(text)
                if sys_info:
                    entry = {
                        "type": "system",
                        "role": "system",
                        "content": sys_info["summary"],
                        "tag": sys_info["tag"],
                        "timestamp": timestamp,
                    }
                    if sys_info.get("body"):
                        entry["body"] = sys_info["body"]
                    return entry
                return {
                    "type": "user",
                    "role": "user",
                    "content": text,
                    "timestamp": timestamp,
                }
        if event_type == "agent_message":
            text = str(payload.get("message") or "")
            if text:
                return {
                    "type": "assistant_text",
                    "role": "assistant",
                    "content": text,
                    "timestamp": timestamp,
                }
        if event_type == "exec_command_end":
            return _parse_codex_exec_end(payload, timestamp)
        if event_type == "patch_apply_end":
            return _parse_codex_patch_apply_end(payload, timestamp)
        if event_type == "task_started":
            return None
        if event_type == "task_complete":
            return None
        return None

    if entry_type != "response_item":
        return None

    item_type = payload.get("type")
    if item_type == "function_call":
        arguments = payload.get("arguments") or ""
        tool_name = payload.get("name") or "?"
        tool_input = _parse_codex_call_args(arguments)
        if tool_name == "exec_command":
            tool_input.setdefault("command", tool_input.get("cmd") or _codex_command_text(payload, tool_input))
            if tool_input.get("workdir") and "cwd" not in tool_input:
                tool_input["cwd"] = tool_input["workdir"]
        elif tool_name == "write_stdin":
            tool_input.setdefault("session_id", tool_input.get("session_id"))
        entry = {
            "type": "tool_use",
            "role": "assistant",
            "tool_name": tool_name,
            "tool_id": payload.get("call_id") or "",
            "input": tool_input,
            "timestamp": timestamp,
        }
        todos = None
        if tool_name == "update_plan":
            todos = parse_plan_snapshot(arguments)
        if todos:
            return [
                entry,
                {
                    "type": "todo_plan",
                    "role": "assistant",
                    "todos": todos,
                    "timestamp": timestamp,
                },
            ]
        return entry

    if item_type == "custom_tool_call":
        tool_name = payload.get("name") or "?"
        tool_input = {"input": str(payload.get("input") or "")}
        normalized_tool_name = tool_name
        if tool_name == "apply_patch":
            normalized_tool_name = "Patch"
            tool_input = _parse_codex_patch_input(str(payload.get("input") or ""))
        return {
            "type": "tool_use",
            "role": "assistant",
            "tool_name": normalized_tool_name,
            "tool_id": payload.get("call_id") or "",
            "input": tool_input,
            "timestamp": timestamp,
        }

    if item_type == "function_call_output":
        entry = {
            "type": "tool_result",
            "role": "tool",
            "tool_id": payload.get("call_id") or "",
            "content": str(payload.get("output") or ""),
            "is_error": False,
            "timestamp": timestamp,
            "result_kind": "function_call_output",
        }
        entry.update(_parse_codex_tool_output_metadata(entry["content"]))
        return entry

    if item_type == "custom_tool_call_output":
        return _parse_codex_custom_tool_output(payload, timestamp)

    if item_type == "reasoning":
        text = _extract_codex_text_blocks(payload.get("summary"))
        if text:
            entry = {
                "type": "thinking",
                "role": "assistant",
                "content": text,
                "timestamp": timestamp,
            }
            return entry

    # Skip response_item.message to avoid duplicating the operator-visible
    # stream already emitted by event_msg.user_message/agent_message.
    return None


def extract_codex_message_text(raw_entry: dict) -> str:
    if raw_entry.get("type") == "event_msg":
        payload = raw_entry.get("payload") or {}
        if payload.get("type") != "agent_message":
            return ""
        text = str(payload.get("message") or "")
        return text[:150] if len(text) > 5 else ""
    if raw_entry.get("type") != "response_item":
        return ""
    payload = raw_entry.get("payload") or {}
    if payload.get("type") != "message":
        return ""
    role = str(payload.get("role") or "")
    text = _extract_codex_text_blocks(payload.get("content"))
    if not _is_codex_visible_message(role, text):
        return ""
    return text[:150]


def extract_codex_model(raw_entry: dict, current_model: str | None) -> str | None:
    """Return the model id observed in a Codex JSONL entry, else ``current_model``.

    Codex serializes the active model in two places:
      1. ``session_meta`` envelope at session start, under ``payload.cli_version``
         siblings — the model field is ``payload.model`` or, for newer rollouts,
         nested in ``payload.config.model`` / ``payload.originator.model``.
      2. ``event_msg`` / ``response_item`` envelopes per turn — sometimes carry
         a ``model`` field on the payload root for newly-bound models. We pick
         up either to keep the value fresh on per-turn ingest.
    """
    payload = raw_entry.get("payload") or {}
    if not isinstance(payload, dict):
        return current_model
    entry_type = raw_entry.get("type")
    if entry_type == "session_meta":
        for candidate in (
            payload.get("model"),
            (payload.get("config") or {}).get("model") if isinstance(payload.get("config"), dict) else None,
            (payload.get("originator") or {}).get("model") if isinstance(payload.get("originator"), dict) else None,
        ):
            if isinstance(candidate, str) and candidate:
                return candidate
        return current_model
    # Per-turn payloads: response_item / event_msg may include model on the
    # outer payload (Codex normaliser surfaces it consistently per
    # graph://0ac8e52c-2de). Prefer payload.model, then nested response.model.
    candidate = payload.get("model")
    if isinstance(candidate, str) and candidate:
        return candidate
    response = payload.get("response")
    if isinstance(response, dict):
        candidate = response.get("model")
        if isinstance(candidate, str) and candidate:
            return candidate
    return current_model


def extract_codex_context_tokens(raw_entry: dict, current_tokens: int) -> int:
    """Use Codex's current-turn input usage, not cumulative session totals."""

    if raw_entry.get("type") != "event_msg":
        return current_tokens
    payload = raw_entry.get("payload") or {}
    if payload.get("type") != "token_count":
        return current_tokens
    info = payload.get("info") or {}
    for usage in (info.get("last_token_usage") or {}, info.get("total_token_usage") or {}):
        if not isinstance(usage, dict):
            continue
        for key in ("input_tokens", "total_tokens"):
            try:
                val = int(usage.get(key) or 0)
            except (TypeError, ValueError):
                val = 0
            if val > 0:
                return val
    return current_tokens


def _coerce_rate_limit_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_rate_limit_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_codex_rate_limit_window(window: Any) -> dict[str, float | int | None] | None:
    if not isinstance(window, dict):
        return None
    used_percent = _coerce_rate_limit_float(window.get("used_percent"))
    window_minutes = _coerce_rate_limit_int(window.get("window_minutes"))
    resets_at = _coerce_rate_limit_int(window.get("resets_at"))
    if used_percent is None and window_minutes is None and resets_at is None:
        return None
    return {
        "used_percent": used_percent,
        "window_minutes": window_minutes,
        "resets_at": resets_at,
    }


def _update_last_user_message_at(
    raw_entry: dict,
    current_state: dict[str, Any] | None,
) -> dict[str, Any] | None:
    original_state = current_state
    state = dict(current_state or {})
    timestamp = raw_entry.get("timestamp")
    if not isinstance(timestamp, str) or not timestamp:
        return original_state

    if raw_entry.get("type") == "user" and not raw_entry.get("isSidechain"):
        if state.get("last_user_message_at") == timestamp:
            return original_state
        state["last_user_message_at"] = timestamp
        return state

    payload = raw_entry.get("payload") or {}
    if (
        raw_entry.get("type") == "event_msg"
        and isinstance(payload, dict)
        and payload.get("type") == "user_message"
    ):
        if state.get("last_user_message_at") == timestamp:
            return original_state
        state["last_user_message_at"] = timestamp
        return state

    return original_state


def extract_codex_harness_state(
    raw_entry: dict,
    current_state: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Extract compact Codex rate-limit telemetry for persistent session state."""

    current_state = _update_last_user_message_at(raw_entry, current_state)
    if raw_entry.get("type") != "event_msg":
        return current_state
    payload = raw_entry.get("payload") or {}
    if payload.get("type") != "token_count":
        return current_state
    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, dict) or not rate_limits:
        return current_state

    windows_found: list[tuple[str, dict[str, float | int | None]]] = []
    for key in ("primary", "secondary"):
        normalized = _normalize_codex_rate_limit_window(rate_limits.get(key))
        if normalized is not None:
            windows_found.append((key, normalized))
    if not windows_found:
        return current_state

    windows_found.sort(
        key=lambda item: (
            item[1].get("window_minutes") is None,
            item[1].get("window_minutes") or 0,
            item[0],
        ),
    )
    windows: dict[str, dict[str, float | int | None]] = {
        "short": windows_found[0][1],
    }
    if len(windows_found) > 1:
        windows["long"] = windows_found[-1][1]

    updated_state: dict[str, Any] = dict(current_state or {})
    updated_state.update({
        "kind": "rate_limits",
        "harness": "codex",
        "source": "transcript",
        "updated_at": raw_entry.get("timestamp"),
        "limit_id": rate_limits.get("limit_id"),
        "limit_name": rate_limits.get("limit_name"),
        "plan_type": rate_limits.get("plan_type"),
        "credits": rate_limits.get("credits"),
        "rate_limit_reached_type": rate_limits.get("rate_limit_reached_type"),
        "windows": windows,
    })
    return current_state if updated_state == current_state else updated_state


HARNESSES: dict[str, SessionHarness] = {
    "claude": CLAUDE_HARNESS,
    "codex": CODEX_HARNESS,
}

_HOST_LAUNCH_LOCKS: dict[str, asyncio.Lock] = {}


def get_session_harness(name: str | None) -> SessionHarness:
    return HARNESSES.get((name or "").strip().lower(), CLAUDE_HARNESS)


def _read_session_meta(path: Path) -> dict[str, Any]:
    for parent in (path.parent, *path.parents):
        meta_path = parent / ".session_meta.json"
        if not meta_path.exists():
            continue
        try:
            payload = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        return payload if isinstance(payload, dict) else {}
    return {}


def resolve_harness_for_path(path: str | Path | None) -> SessionHarness:
    """Resolve the harness for a transcript path."""

    if not path:
        return CLAUDE_HARNESS
    p = Path(path)
    meta = _read_session_meta(p)
    if meta.get("harness"):
        return get_session_harness(str(meta["harness"]))
    if ".codex" in p.parts or p.name.startswith("rollout-"):
        return CODEX_HARNESS
    if ".claude" in p.parts:
        return CLAUDE_HARNESS
    try:
        if p.is_file():
            with open(p, encoding="utf-8", errors="replace") as fh:
                first_line = fh.readline().strip()
            if first_line:
                raw = json.loads(first_line)
                if raw.get("type") == "session_meta" and (raw.get("payload") or {}).get("originator") == "codex-tui":
                    return CODEX_HARNESS
    except (OSError, json.JSONDecodeError):
        pass
    return CLAUDE_HARNESS


def resolve_harness_for_session_row(row: dict | None) -> SessionHarness:
    """Resolve the harness for a dashboard session row."""

    if row:
        harness_name = row.get("harness") or row.get("provider")
        if harness_name:
            return get_session_harness(harness_name)
        session_uuid = str(row.get("session_uuid") or "")
        if session_uuid.startswith("rollout-"):
            return CODEX_HARNESS
    if row and row.get("jsonl_path"):
        return resolve_harness_for_path(row["jsonl_path"])
    return CLAUDE_HARNESS


def _get_host_launch_lock(project_folder: str) -> asyncio.Lock:
    return _HOST_LAUNCH_LOCKS.setdefault(project_folder, asyncio.Lock())


def _link_session_file(
    tmux_name: str,
    jsonl_path: Path,
    *,
    project: str | None = None,
) -> dict | None:
    if not jsonl_path.exists() or jsonl_path.suffix != ".jsonl":
        return None
    from tools.dashboard.dao import dashboard_db

    project = project or jsonl_path.parent.name
    session_uuid = jsonl_path.stem
    dashboard_db.link_and_enrich(
        tmux_name,
        session_uuid=session_uuid,
        jsonl_path=str(jsonl_path),
        project=project,
    )
    return {
        "tmux_name": tmux_name,
        "project": project,
        "session_uuid": session_uuid,
        "jsonl_path": jsonl_path,
        "resolution_dir": jsonl_path.parent,
    }


def _resolve_claude_host_jsonl(tmux_name: str) -> Path | None:
    claude_projects = Path.home() / ".claude" / "projects"
    if not claude_projects.exists():
        return None
    for meta_path in sorted(
        claude_projects.rglob("*.meta.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        try:
            data = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("tmux_session") != tmux_name:
            continue
        jsonl = meta_path.parent / (meta_path.stem.removesuffix(".meta") + ".jsonl")
        if jsonl.exists():
            return jsonl
    return None


def _resolve_claude_handshake_link(tmux_name: str, handshake_text: str) -> dict | None:
    claude_projects = Path.home() / ".claude" / "projects"
    if not claude_projects.exists():
        return None

    all_jsonls: list[Path] = []
    for project_dir in claude_projects.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl in project_dir.glob("*.jsonl"):
            all_jsonls.append(jsonl)
    all_jsonls.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    for jsonl in all_jsonls[:5]:
        try:
            lines = jsonl.read_text(encoding="utf-8", errors="replace").strip().split("\n")
        except OSError:
            continue
        tail = lines[-5:] if len(lines) > 5 else lines
        if any(handshake_text in line for line in tail):
            return _link_session_file(tmux_name, jsonl)
    return None


def _attach_live_monitoring(
    monitor: Any,
    *,
    tmux_name: str,
    jsonl_path: Path,
    resolution_dir: Path | None = None,
    reset_offset: bool = False,
    reset_state: bool = False,
) -> None:
    from tools.dashboard.dao.dashboard_db import update_tail_state
    from tools.dashboard.session_monitor import _TailState

    res_dir = resolution_dir or jsonl_path.parent
    current = monitor._tail_states.get(tmux_name)
    if current is None or reset_state:
        monitor._tail_states[tmux_name] = _TailState(
            resolution_dir=res_dir,
            needs_resolution=False,
        )
    else:
        current.resolution_dir = res_dir
        current.needs_resolution = False

    if getattr(monitor, "_use_inotify", False):
        monitor._add_file_watch(tmux_name, str(jsonl_path))
        monitor._add_dir_watch(tmux_name, str(res_dir))

    if reset_offset:
        update_tail_state(tmux_name, file_offset=0)


async def _watch_for_claude_host_jsonl(
    monitor: Any,
    projects_dir: Path,
    tmux_name: str,
    timeout: float = 10.0,
) -> None:
    lock = _get_host_launch_lock(projects_dir.name)
    async with lock:
        existing = set(projects_dir.glob("*.jsonl")) if projects_dir.exists() else set()
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.5)
            if not projects_dir.exists():
                continue
            current = set(projects_dir.glob("*.jsonl"))
            new_files = current - existing
            if not new_files:
                continue
            new_jsonl = min(new_files, key=lambda p: p.stat().st_mtime)
            linked = _link_session_file(tmux_name, new_jsonl)
            if linked is None:
                return
            CLAUDE_HARNESS.attach_live_monitoring(
                monitor=monitor,
                tmux_name=tmux_name,
                jsonl_path=new_jsonl,
                resolution_dir=projects_dir,
            )
            await monitor._broadcast_registry()
            return
        logger.warning("JSONL watcher timed out after %.0fs  tmux=%s", timeout, tmux_name)
