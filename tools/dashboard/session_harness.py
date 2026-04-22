"""Session harness adapter seam for live transcript parsing.

The current production path is still Claude-only, but the rest of the
dashboard should stop depending on Claude helpers directly.  This module
provides a minimal adapter boundary that can keep delegating to the
existing Claude implementation while we carve out a parallel Codex path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Protocol, Any


class SessionHarness(Protocol):
    """Contract for a session transcript harness."""

    name: str

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


def resolve_harness_for_path(path: str | Path | None) -> SessionHarness:
    """Resolve the harness for a transcript path.

    Only Claude-backed sessions exist today, so this always returns the
    Claude adapter. The path argument is accepted now so callers can route
    through this helper without another API change when Codex lands.
    """

    _ = Path(path) if path else None
    return CLAUDE_HARNESS


def resolve_harness_for_session_row(row: dict | None) -> SessionHarness:
    """Resolve the harness for a dashboard session row."""

    if row and row.get("jsonl_path"):
        return resolve_harness_for_path(row["jsonl_path"])
    return CLAUDE_HARNESS


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
