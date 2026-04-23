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
import sqlite3
from typing import Protocol, Any


logger = logging.getLogger(__name__)


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
            return _link_session_file(tmux_name, jsonl_path)

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
        _ = session_dir
        return postprocess_codex_entries(entries)

    def extract_message_text(self, raw_entry: dict) -> str:
        return extract_codex_message_text(raw_entry)

    def extract_context_tokens(self, raw_entry: dict, current_tokens: int) -> int:
        return extract_codex_context_tokens(raw_entry, current_tokens)


CODEX_HARNESS = CodexSessionHarness()


_CROSSTALK_RE = re.compile(
    r'<crosstalk\s+from="([^"]+)"\s+label="([^"]*)"\s+source="([^"]*)"\s+turn="([^"]*)"\s+timestamp="([^"]+)">\n(.*)\n</crosstalk>',
    re.DOTALL,
)


def _graph_db_path() -> str | None:
    return os.environ.get("GRAPH_DB") or None


def _classify_crosstalk(text: str) -> dict | None:
    stripped = text.strip()
    m = _CROSSTALK_RE.fullmatch(stripped)
    if not m:
        return None
    body = m.group(6)
    if "<" in body or ">" in body:
        return None
    return {
        "from": m.group(1),
        "label": m.group(2),
        "source": m.group(3),
        "turn": m.group(4),
        "timestamp": m.group(5),
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
        sem = _upconvert_graph_result(result_content, timestamp, tool_id=tool_id)
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


def postprocess_codex_entries(entries: list[dict]) -> list[dict]:
    use_ids = {
        entry.get("tool_id")
        for entry in entries
        if entry.get("type") == "tool_use" and entry.get("tool_name") == "exec_command" and entry.get("tool_id")
    }
    transforms: dict[str, dict] = {}
    for entry in entries:
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

    if not transforms:
        return entries

    out: list[dict] = []
    for entry in entries:
        tool_id = entry.get("tool_id") or ""
        transform = transforms.get(tool_id)
        if (
            entry.get("type") == "tool_use"
            and entry.get("tool_name") == "exec_command"
            and transform is not None
        ):
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
    sem = _upconvert_graph_result(output, timestamp, tool_id=tool_id)
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

    if entry_type == "event_msg":
        event_type = payload.get("type")
        if event_type == "user_message":
            text = str(payload.get("message") or "")
            if text:
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
        if event_type == "task_started":
            return {
                "type": "system",
                "role": "system",
                "content": "Task started",
                "tag": "task-started",
                "timestamp": timestamp,
            }
        if event_type == "task_complete":
            return {
                "type": "system",
                "role": "system",
                "content": "Task complete",
                "tag": "task-complete",
                "timestamp": timestamp,
            }
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

    if item_type == "function_call_output":
        return {
            "type": "tool_result",
            "role": "tool",
            "tool_id": payload.get("call_id") or "",
            "content": str(payload.get("output") or ""),
            "is_error": False,
            "timestamp": timestamp,
            "result_kind": "function_call_output",
        }

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
