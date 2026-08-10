"""Session harness adapter seam for live transcript parsing.

Harnesses own raw transcript parsing.  The dashboard above this module owns
shared normalized entries, activity state, SSE delivery, and rendering.
"""

from __future__ import annotations

import asyncio
import hashlib
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
# Fallback pairing store for parse_line callers that predate explicit
# parse contexts (auto-16g9t). Every in-repo parse loop passes its own
# ``ctx`` dict, so live streams and HTTP replays can never steal each
# other's pending functions.exec wrapper pairs; this module-level dict
# only serves stray ctx-less callers, best-effort.
_CODEX_EXEC_WRAPPER_CALLS: dict[str, list[dict[str, Any]]] = {}


# ── Canonical entry identity (auto-16g9t) ──────────────────────────────
#
# Every rendered entry carries entry_ref = {file, off, sub}:
#   file — the transcript file's filename stem (what session_uuids stores)
#   off  — byte offset of the raw line's start within that file
#   sub  — 0..k index among the entries produced from that one raw line
# Total order = (position of file in the session chain, off, sub). Any two
# batches from any two server paths merge and dedupe by this tuple; harness
# message ids (Claude uuid, Codex call_id) ride along as metadata only.


def iter_jsonl_lines_with_offsets(data: bytes, base_offset: int = 0):
    """Yield (line_text, line_start_byte_offset) for each non-empty line.

    ``base_offset`` is the file offset of ``data[0]``. Offsets are computed
    from the RAW bytes (newline included in the running total) so they are
    exact file positions regardless of decoding replacements.
    """
    off = base_offset
    for raw in data.splitlines(keepends=True):
        line_off = off
        off += len(raw)
        line = raw.decode("utf-8", errors="replace").strip()
        if line:
            yield line, line_off


def stamp_entry_refs(
    parsed: dict | list[dict], stem: str, line_off: int,
) -> list[dict]:
    """Stamp parse-time entry_refs onto one line's parsed entries."""
    entries = parsed if isinstance(parsed, list) else [parsed]
    for sub, entry in enumerate(entries):
        entry["entry_ref"] = {"file": stem, "off": line_off, "sub": sub}
    return entries


def parse_lines_with_refs(
    harness: "SessionHarness",
    data: bytes,
    *,
    stem: str,
    base_offset: int = 0,
    ctx: dict | None = None,
) -> list[dict]:
    """Parse a raw byte window into entries stamped with entry_refs."""
    out: list[dict] = []
    for line, line_off in iter_jsonl_lines_with_offsets(data, base_offset):
        try:
            parsed = harness.parse_line(line, ctx=ctx)
        except Exception:
            logger.exception("parse_lines_with_refs: parse_line failed")
            continue
        if parsed is None:
            continue
        out.extend(stamp_entry_refs(parsed, stem, line_off))
    return out


def finalize_entry_refs(entries: list[dict]) -> None:
    """Re-assign sub_index by output order within each (file, off) group.

    Runs AFTER postprocessing: split results (``dict(entry)`` copies) and
    synthesized sidecars would otherwise duplicate their source line's
    parse-time sub. Entries with no ref (fresh dicts a builder forgot to
    stamp) inherit the previous entry's line — postprocess appends derived
    entries directly after their source, so this is the correct line.
    Assumes postprocess never drops PART of a multi-entry line (it drops
    whole lines or whole-line-derived singles), which holds for both
    harnesses today; a partial drop would only shift sub numbering for
    that one line.
    """
    counters: dict[tuple[str, int], int] = {}
    last_key: tuple[str, int] | None = None
    for entry in entries:
        ref = entry.get("entry_ref")
        if isinstance(ref, dict) and "file" in ref and "off" in ref:
            key = (ref["file"], ref["off"])
        else:
            key = last_key
            if key is None:
                continue
        sub = counters.get(key, 0)
        entry["entry_ref"] = {"file": key[0], "off": key[1], "sub": sub}
        counters[key] = sub + 1
        last_key = key


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

    def parse_line(self, line: str, ctx: dict | None = None) -> dict | list[dict] | None:
        """Parse one raw transcript line into normalized viewer entries.

        ``ctx`` scopes cross-line parse state (e.g. Codex functions.exec
        wrapper pairing) to ONE parse stream. Every caller that loops over
        lines must hold one ctx dict per loop — sharing a stream's ctx with
        an unrelated replay is exactly the state-theft bug auto-16g9t fixed.
        """

    def postprocess_entries(
        self,
        entries: list[dict],
        *,
        session_dir: Path | None = None,
        state: dict | None = None,
    ) -> list[dict]:
        """Apply harness-specific entry post-processing.

        ``state`` is the stream's persistent postprocess state (see
        :meth:`new_postprocess_state`). ``None`` means a self-contained
        read: a fresh state is used and discarded — NEVER a process-global.
        """

    def new_postprocess_state(self) -> dict:
        """Fresh postprocess state for one stream (live tail or replay)."""

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

    def read_screen_state(
        self,
        pane_text: str,
        current_state: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], list[dict]]:
        """Inspect a tmux capture-pane snapshot — return state + keystrokes.

        auto-eerfx. The session monitor's screen-poll loop calls this
        every 2s for sessions in the harness_starting / first_turn_written
        phase window. The harness adapter decides WHAT to send (e.g. an
        Enter to confirm a trust dialog); the poller decides WHEN to
        send it via tmux_send_keys.

        Returns:
          - new_state: dict to merge into harness_state. Documented keys:
            * composer_ready (bool)         — harness will accept input
            * confirming_trust_prompt (bool) — trust dialog detected AND
                                              a confirm keystroke sent
            * in_planning_mode (bool)       — planning-mode banner visible
            * blocking_modal (str | None)   — name of any other blocking
                                              modal
            Adapters may add their own keys; consumers must not assume
            keys beyond the documented schema.
          - keystrokes: list of {"kind": "key"|"literal", "value": str}
            dicts to inject in order via tmux_send_keys.

        Adapters without screen-reading inference (Codex initially) MUST
        return ``composer_ready=True`` from this method as soon as
        ``current_state`` is non-empty (the first JSONL turn has been
        written) — otherwise sessions running that harness never reach
        the composer-ready signal and the derived ``ready`` flag
        never fires.
        """


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

    def parse_line(self, line: str, ctx: dict | None = None) -> dict | list[dict] | None:
        _ = ctx  # Claude parsing carries no cross-line state
        return parse_claude_log_line(line)

    def postprocess_entries(
        self,
        entries: list[dict],
        *,
        session_dir: Path | None = None,
        state: dict | None = None,
    ) -> list[dict]:
        _ = state  # Claude postprocessing is batch-local
        return postprocess_claude_entries(entries, session_dir=session_dir)

    def new_postprocess_state(self) -> dict:
        return {}

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
        # Claude Code writes error/local-command entries with the literal
        # placeholder id "<synthetic>". Persisting it poisons the row's model
        # column, and resume forwards it as --model <synthetic> — an
        # unresolvable model, so the relaunched session errors at boot
        # ("issue with the selected model ()", 2026-07-14). Real ids never
        # start with "<".
        if isinstance(model, str) and model and not model.startswith("<"):
            return model
        return current_model

    def extract_harness_state(
        self,
        raw_entry: dict,
        current_state: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        updated_state = _update_last_user_message_at(raw_entry, current_state)
        return current_state if updated_state == (current_state or {}) else updated_state

    def read_screen_state(
        self,
        pane_text: str,
        current_state: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], list[dict]]:
        return _claude_read_screen_state(pane_text, current_state)


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

    def parse_line(self, line: str, ctx: dict | None = None) -> dict | list[dict] | None:
        return parse_codex_log_line(line, ctx=ctx)

    def postprocess_entries(
        self,
        entries: list[dict],
        *,
        session_dir: Path | None = None,
        state: dict | None = None,
    ) -> list[dict]:
        return postprocess_codex_entries(
            entries, session_dir=session_dir, state=state,
        )

    def new_postprocess_state(self) -> dict:
        return new_codex_progress_state()

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

    def read_screen_state(
        self,
        pane_text: str,
        current_state: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], list[dict]]:
        # auto-eerfx: Codex screen-reading is deferred to a follow-up
        # bead. The stub returns composer_ready=True unconditionally so
        # Codex sessions can reach composer_ready (and
        # thus the derived 'ready' state) without waiting on a
        # screen-state signal this adapter doesn't yet produce.
        return (
            {
                "composer_ready": True,
                "confirming_trust_prompt": False,
                "in_planning_mode": False,
                "blocking_modal": None,
            },
            [],
        )


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


def _upconvert_viewer_attachment(
    content: str, timestamp: str, tool_id: str = ""
) -> dict | None:
    """Upconvert ``graph share`` JSON output into a typed ``viewer_attachment``
    parser entry.

    The CLI prints a single JSON object whose ``type`` is ``viewer_attachment``;
    we recognize that discriminator and emit a typed entry the viewer renders
    as a thumbnail tile (tap to fullscreen). The file itself lives under
    ``/workspace/output/.attachments/...`` (host's ``data/agent-runs/<run>/``)
    and is served via ``/api/session/<tmux>/output/<rel_path>``.

    Security: ``session`` is **deliberately not** copied from the payload —
    ``tool_result`` content is whatever the agent printed, so a compromised
    or malicious agent could otherwise emit ``"session": "<other-tmux>"`` and
    induce the viewer to fetch another session's files (confused-deputy
    cross-session read). The trusted session is stamped later by the
    SessionMonitor, which knows authoritatively which JSONL stream this
    entry came from. Same reason for skipping any other identity-shaped
    fields the payload might claim.
    """
    if not isinstance(content, str):
        return None
    stripped = content.strip()
    if not stripped or stripped[0] != "{":
        return None
    if "viewer_attachment" not in stripped:
        return None
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("type") != "viewer_attachment":
        return None
    rel_path = payload.get("rel_path")
    if not isinstance(rel_path, str) or not rel_path:
        return None
    entry: dict[str, Any] = {
        "type": "viewer_attachment",
        "role": "tool",
        "timestamp": timestamp,
        "rel_path": rel_path,
    }
    if tool_id:
        entry["tool_id"] = tool_id
    # ``session`` intentionally omitted — see docstring. Stamped by monitor.
    for key in ("filename", "mime", "alt", "caption", "sha8"):
        v = payload.get(key)
        if isinstance(v, str) and v:
            entry[key] = v
    size = payload.get("size")
    if isinstance(size, int) and size >= 0:
        entry["size"] = size
    return entry


def _extract_read_images(
    result_content_raw: object,
    timestamp: str,
    *,
    tool_id: str = "",
) -> list[dict]:
    """Surface image blocks inside a tool_result as ``read_image`` tiles.

    When the assistant Reads an image file, the tool_result content is a list
    whose blocks include ``{type:image, source:{type:base64, media_type, data}}``.
    The text-only flattening elsewhere would discard these, so the picture never
    reaches the viewer (it shows an empty Read chip). The base64 is already in the
    log — we just pass it through as a typed entry the client renders inline
    (#34). One entry per image block.
    """
    if not isinstance(result_content_raw, list):
        return []
    out: list[dict] = []
    for block in result_content_raw:
        if not isinstance(block, dict) or block.get("type") != "image":
            continue
        source = block.get("source") or {}
        if not isinstance(source, dict) or source.get("type") != "base64":
            continue
        data = source.get("data") or ""
        if not data:
            continue
        out.append({
            "type": "read_image",
            "role": "tool",
            "tool_id": tool_id,
            "mime": source.get("media_type") or "image/png",
            "data": data,
            "timestamp": timestamp,
        })
    return out


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


# Pure image placeholders the harness injects alongside real payloads — the
# image tile carries the meaning, so these strings must not render as text.
_IMAGE_PLACEHOLDER_RE = re.compile(
    r"^\[Image #\d+\]$|^\[Image: source: .+\]$", re.IGNORECASE
)


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
            message_id = claude_queue_message_id(raw, content, timestamp)
            return {
                "type": "user",
                "content": content,
                "timestamp": timestamp,
                "queued": True,
                **({"message_id": message_id} if message_id else {}),
            }
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
                    result_content_raw = block.get("content", "")
                    if isinstance(result_content_raw, list):
                        result_content = "".join(
                            b.get("text", "") for b in result_content_raw
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    else:
                        result_content = result_content_raw
                    tool_use_id = block.get("tool_use_id", "")
                    tool_results.append({
                        "type": "tool_result",
                        "role": "tool",
                        "tool_id": tool_use_id,
                        "content": result_content,
                        "is_error": block.get("is_error", False),
                        "timestamp": timestamp,
                    })
                    # Surface any images the assistant Read (#34) — the text-only
                    # flatten above would otherwise drop them silently.
                    tool_results.extend(
                        _extract_read_images(result_content_raw, timestamp, tool_id=tool_use_id)
                    )
                    va = _upconvert_viewer_attachment(
                        result_content, timestamp, tool_id=tool_use_id,
                    )
                    if va:
                        tool_results.append(va)
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
        read_imgs = _extract_read_images(content_raw, timestamp, tool_id=tool_id)
        if not result_content and not read_imgs:
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
        va = _upconvert_viewer_attachment(result_content, timestamp, tool_id=tool_id)
        sem = _upconvert_graph_result(result_content, timestamp, tool_id=tool_id)
        out = [base_result]
        if va:
            out.append(va)
        if sem:
            _enrich_semantic_tile(sem)
            out.append(sem)
        out.extend(read_imgs)   # surface Read-of-image tiles (#34)
        return out if len(out) > 1 else base_result

    if entry_type == "attachment":
        # A message/screenshot the operator queued WHILE the assistant was working
        # (#34). The real payload — including base64 images — lives in
        # attachment.prompt[], and this record type had no handler, so the whole
        # thing (and the only copy of the image) was dropped. Materialize it.
        attachment = raw.get("attachment")
        if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
            return None  # other subtypes (task_reminder, …) are scaffolding
        prompt = attachment.get("prompt")
        if not isinstance(prompt, list):
            return None
        attach_entries: list[dict] = []
        text_parts: list[str] = []
        for block in prompt:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text", "")
                # Drop pure "[Image #N]" / "[Image: source: …]" placeholders.
                if t and not _IMAGE_PLACEHOLDER_RE.match(t.strip()):
                    text_parts.append(t)
        text = "".join(text_parts).strip()
        if text:
            attach_entries.append({
                "type": "user",
                "role": "user",
                "content": text,
                "timestamp": timestamp,
                **identity,
            })
        # prompt image parts share the tool_result image shape, so reuse the
        # extractor — they surface as the same read_image tiles.
        attach_entries.extend(_extract_read_images(prompt, timestamp))
        if not attach_entries:
            return None
        return attach_entries if len(attach_entries) > 1 else attach_entries[0]

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
    last_enqueue_index: int | None = None
    for entry in entries:
        if entry.get("queued"):
            last_enqueue_content = entry.get("content", "").strip()
            last_enqueue_index = len(result)
            result.append(entry)
        elif (
            entry.get("type") in ("user", "crosstalk")
            and last_enqueue_content
            and entry.get("content", "").strip() == last_enqueue_content
        ):
            if (
                last_enqueue_index is not None
                and 0 <= last_enqueue_index < len(result)
            ):
                kept = result[last_enqueue_index]
                for src_key, dst_key in (
                    ("message_id", "message_id"),
                    ("parent_uuid", "parent_uuid"),
                ):
                    if entry.get(src_key) and not kept.get(dst_key):
                        kept[dst_key] = entry[src_key]
            last_enqueue_content = None
            last_enqueue_index = None
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


def parse_plan_snapshot(arguments: str | dict) -> list[dict] | None:
    """Best-effort parser for Codex-style ``update_plan`` arguments.

    Accept both the direct function-call JSON string and the decoded object
    extracted from a functions.exec orchestration wrapper.
    """

    if isinstance(arguments, dict):
        payload = arguments
    else:
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
    state: dict | None = None,
) -> list[dict]:
    _ = session_dir  # retained for signature parity with the Claude harness
    if state is None:
        state = new_codex_progress_state()
    tool_names = state["tool_names"]
    exec_sessions = state["exec_sessions"]
    write_calls = state["write_calls"]
    completed_tools = state["completed_tools"]
    use_refs = state.setdefault("use_refs", {})

    normalized: list[dict] = []
    patch_results: dict[str, dict] = {}

    for entry in entries:
        if entry.get("type") == "tool_use" and entry.get("tool_id"):
            tool_id = entry.get("tool_id") or ""
            tool_name = str(entry.get("tool_name") or "")
            tool_names[tool_id] = tool_name
            if entry.get("entry_ref") is not None:
                use_refs[tool_id] = entry["entry_ref"]
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
                        _append_codex_exec_sidecars(normalized, progress)
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
                            _append_codex_exec_sidecars(normalized, progress)
                        continue
                    continue
                if entry.get("status") == "running":
                    # In a split/cold batch with no remembered tool name,
                    # "Process running with session ID ..." is not enough
                    # evidence that this function_call_output is itself an
                    # active exec_command. Treat the call as complete so
                    # write_stdin polling artifacts cannot pin activity.
                    entry["status"] = "completed"

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
                # Split live batch: the tool_use published earlier, in a
                # previous window. Stamp these synthetic upgrades with the
                # remembered CALL-line ref so they merge over the existing
                # raw tile instead of inserting a duplicate.
                call_ref = use_refs.get(tool_id)
                ops = transform["ops"]
                upgrades = [
                    _build_codex_semantic_tool_use(
                        entry,
                        ops[0],
                        tool_id,
                        preserve_timestamp=False,
                    ),
                ]
                for idx, op in enumerate(ops[1:], start=2):
                    upgrades.append(
                        _build_codex_semantic_tool_use(
                            entry,
                            op,
                            f"{tool_id}#{idx}",
                            preserve_timestamp=False,
                        ),
                    )
                for upgrade in upgrades:
                    if call_ref is not None:
                        upgrade["entry_ref"] = call_ref
                out.extend(upgrades)
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


def codex_message_id(payload: dict, role: str, text: str) -> str | None:
    """Return the deterministic ``message_id`` for a Codex event_msg chat turn.

    Live Codex ``event_msg`` user/agent rows do not always carry a raw UUID,
    but every downstream consumer (live viewer overlay, graph ingest, accepted
    turn-correction lookup) needs the same stable identity. Prefer the
    explicit payload UUID when present; otherwise derive a synthetic
    ``codex-<role>:<sha1[:16]>`` so live overlay state, ingest, and graph
    persistence all agree on the same key.

    Returns ``None`` when neither a raw UUID nor any text is available — a
    Codex turn with no body cannot have a stable identity.
    """
    raw_uuid = payload.get("uuid") or payload.get("message_id")
    if isinstance(raw_uuid, str) and raw_uuid:
        return raw_uuid
    if text:
        digest = hashlib.sha1(f"{role}\n{text}".encode("utf-8")).hexdigest()[:16]
        return f"codex-{role}:{digest}"
    return None


def claude_queue_message_id(payload: dict, text: str, timestamp: str) -> str | None:
    """Return the stable identity for a Claude ``queue-operation`` user turn.

    Queue rows normally have no UUID and do not always receive a later
    UUID-bearing user echo. The live correction overlay still needs an
    identity, as does graph ingest when an accepted correction is persisted.
    Prefer an explicit UUID when a provider supplies one; otherwise include
    the event timestamp in a content hash so two identical queued messages in
    one session remain distinct.
    """
    raw_uuid = payload.get("uuid") or payload.get("message_id")
    if isinstance(raw_uuid, str) and raw_uuid:
        return raw_uuid
    if not text:
        return None
    digest = hashlib.sha1(
        f"{timestamp}\n{text}".encode("utf-8")
    ).hexdigest()[:16]
    return f"claude-queued-user:{digest}"


def _codex_event_message_identity(payload: dict, role: str, text: str) -> dict[str, str]:
    """Return stable tile identity for Codex event_msg chat turns.

    Wraps :func:`codex_message_id` and adds ``parent_uuid`` when the payload
    carries one; live tile rendering uses the parent edge but graph ingest
    does not need it.
    """
    identity: dict[str, str] = {}
    msg_id = codex_message_id(payload, role, text)
    if msg_id:
        identity["message_id"] = msg_id
    parent = payload.get("parentUuid") or payload.get("parent_uuid")
    if isinstance(parent, str) and parent:
        identity["parent_uuid"] = parent
    return identity


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
    body = text
    body_split = _CODEX_TOOL_OUTPUT_BODY_RE.split(text, maxsplit=1)
    if len(body_split) == 2:
        header, body = body_split
    else:
        header = text

    session_match = _CODEX_TOOL_OUTPUT_SESSION_RE.search(header)
    exit_match = _CODEX_TOOL_OUTPUT_EXIT_RE.search(header)
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
    time_match = _CODEX_TOOL_OUTPUT_TIME_RE.search(header)
    if time_match:
        try:
            data["duration_seconds"] = float(time_match.group(1))
        except ValueError:
            pass
    data["stdout"] = body
    return data


def _skip_javascript_literal(source: str, start: int) -> int:
    """Return the first offset after a quoted JavaScript literal.

    The functions.exec input is JavaScript, so nested ``tools.*`` text can
    also occur inside shell-command strings.  A small lexical scanner is
    enough to distinguish executable calls without taking a JavaScript parser
    dependency in the dashboard process.
    """
    quote = source[start]
    idx = start + 1
    while idx < len(source):
        char = source[idx]
        if char == "\\":
            idx += 2
            continue
        if char == quote:
            return idx + 1
        idx += 1
    return len(source)


def _javascript_call_end(source: str, open_paren: int) -> int | None:
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = [")"]
    idx = open_paren + 1
    while idx < len(source):
        char = source[idx]
        if char in "'\"`":
            idx = _skip_javascript_literal(source, idx)
            continue
        if source.startswith("//", idx):
            newline = source.find("\n", idx + 2)
            idx = len(source) if newline == -1 else newline + 1
            continue
        if source.startswith("/*", idx):
            end = source.find("*/", idx + 2)
            idx = len(source) if end == -1 else end + 2
            continue
        if char in pairs:
            stack.append(pairs[char])
        elif char in ")]}":
            if not stack or char != stack[-1]:
                return None
            stack.pop()
            if not stack:
                return idx
        idx += 1
    return None


def _iter_codex_exec_wrapper_calls(source: str) -> list[tuple[str, str, int]]:
    """Extract executable ``tools.<name>(...)`` calls from functions.exec JS."""
    calls: list[tuple[str, str, int]] = []
    idx = 0
    while idx < len(source):
        char = source[idx]
        if char in "'\"`":
            idx = _skip_javascript_literal(source, idx)
            continue
        if source.startswith("//", idx):
            newline = source.find("\n", idx + 2)
            idx = len(source) if newline == -1 else newline + 1
            continue
        if source.startswith("/*", idx):
            end = source.find("*/", idx + 2)
            idx = len(source) if end == -1 else end + 2
            continue
        if source.startswith("tools.", idx):
            match = re.match(r"tools\.([A-Za-z_][A-Za-z0-9_]*)\s*\(", source[idx:])
            if match:
                open_paren = idx + match.end() - 1
                close_paren = _javascript_call_end(source, open_paren)
                if close_paren is not None:
                    calls.append(
                        (match.group(1), source[open_paren + 1:close_paren].strip(), idx)
                    )
                    idx = close_paren + 1
                    continue
        idx += 1
    return calls


def _quote_javascript_object_keys(source: str) -> str:
    """Convert unquoted JavaScript object keys to JSON-compatible keys.

    Some Codex functions.exec transcripts serialize nested tool arguments as
    ``{cmd:"...",workdir:"..."}`` instead of strict JSON. Only identifiers
    immediately followed by a colon after ``{`` or ``,`` are rewritten;
    quoted command content is copied byte-for-byte.
    """
    out: list[str] = []
    idx = 0
    while idx < len(source):
        char = source[idx]
        if char in "'\"`":
            end = _skip_javascript_literal(source, idx)
            out.append(source[idx:end])
            idx = end
            continue
        out.append(char)
        idx += 1
        if char not in "{,":
            continue
        while idx < len(source) and source[idx].isspace():
            out.append(source[idx])
            idx += 1
        key = re.match(r"[A-Za-z_$][A-Za-z0-9_$]*", source[idx:])
        if not key:
            continue
        key_end = idx + key.end()
        colon = key_end
        while colon < len(source) and source[colon].isspace():
            colon += 1
        if colon >= len(source) or source[colon] != ":":
            continue
        out.append(json.dumps(key.group(0)))
        out.append(source[key_end:colon + 1])
        idx = colon + 1
    return "".join(out)


def _javascript_object_property(source: str, wanted: str) -> str | None:
    """Return one top-level object property's raw JavaScript expression."""
    text = source.strip()
    if not text.startswith("{"):
        return None
    idx = 1
    while idx < len(text):
        while idx < len(text) and (text[idx].isspace() or text[idx] == ","):
            idx += 1
        key = re.match(r"[A-Za-z_$][A-Za-z0-9_$]*", text[idx:])
        if not key:
            return None
        name = key.group(0)
        idx += key.end()
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text) or text[idx] != ":":
            return None
        idx += 1
        while idx < len(text) and text[idx].isspace():
            idx += 1
        value_start = idx
        stack: list[str] = []
        while idx < len(text):
            char = text[idx]
            if char in "'\"`":
                idx = _skip_javascript_literal(text, idx)
                continue
            if char in "([{":
                stack.append({"(": ")", "[": "]", "{": "}"}[char])
            elif char in ")]}":
                if stack:
                    if char != stack[-1]:
                        return None
                    stack.pop()
                elif char == "}":
                    break
            elif char == "," and not stack:
                break
            idx += 1
        value = text[value_start:idx].strip()
        if name == wanted:
            return value or None
        if idx < len(text) and text[idx] == ",":
            idx += 1
            continue
        return None
    return None


def _display_javascript_expression(expression: str) -> str:
    """Return a readable, explicitly unresolved JavaScript value."""
    value = expression.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, str) else value
        except json.JSONDecodeError:
            return value
    if len(value) >= 2 and value[0] == value[-1] == "`":
        return value[1:-1]
    return value


def _resolve_codex_exec_wrapper_argument(
    expression: str,
    source: str,
    call_offset: int,
) -> Any:
    try:
        return json.loads(expression)
    except (json.JSONDecodeError, TypeError):
        pass
    if expression.startswith("{"):
        try:
            return json.loads(_quote_javascript_object_keys(expression))
        except json.JSONDecodeError:
            pass
    if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", expression):
        return expression
    assignment = re.compile(
        rf"(?:const|let|var)\s+{re.escape(expression)}\s*=\s*"
        r'("(?:\\.|[^"\\])*")\s*;'
    )
    matches = list(assignment.finditer(source[:call_offset]))
    if not matches:
        return expression
    try:
        return json.loads(matches[-1].group(1))
    except json.JSONDecodeError:
        return expression


def _wrapper_calls_store(ctx: dict | None) -> dict[str, list[dict[str, Any]]]:
    """The pending functions.exec pairing map for one parse stream."""
    if ctx is None:
        return _CODEX_EXEC_WRAPPER_CALLS
    return ctx.setdefault("exec_wrapper_calls", {})


def _build_codex_exec_wrapper_entries(
    payload: dict,
    timestamp: str,
    ctx: dict | None = None,
) -> list[dict] | None:
    """Expand a functions.exec orchestration call into its nested tool calls."""
    source = str(payload.get("input") or "")
    outer_tool_id = str(payload.get("call_id") or "")
    parsed_calls = _iter_codex_exec_wrapper_calls(source)
    if not parsed_calls:
        return None

    entries: list[dict] = []
    nested_tools: list[dict] = []
    for index, (tool_name, expression, call_offset) in enumerate(parsed_calls, start=1):
        argument = _resolve_codex_exec_wrapper_argument(expression, source, call_offset)
        if isinstance(argument, dict):
            tool_input = dict(argument)
        else:
            tool_input = {"input": argument}
        normalized_name = tool_name
        if tool_name == "exec_command":
            if not isinstance(argument, dict):
                raw_argument = str(argument or "")
                command_expression = _javascript_object_property(raw_argument, "cmd")
                workdir_expression = _javascript_object_property(raw_argument, "workdir")
                if command_expression:
                    tool_input["cmd"] = _display_javascript_expression(command_expression)
                    tool_input["command_expression"] = command_expression
                if workdir_expression:
                    tool_input["workdir"] = _display_javascript_expression(workdir_expression)
            tool_input.setdefault("command", tool_input.get("cmd") or "")
            if tool_input.get("workdir") and "cwd" not in tool_input:
                tool_input["cwd"] = tool_input["workdir"]
        elif tool_name == "apply_patch":
            normalized_name = "Patch"
            tool_input = _parse_codex_patch_input(str(argument or ""))

        tool_id = f"{outer_tool_id}#{index}"
        tool_entry = {
            "type": "tool_use",
            "role": "assistant",
            "tool_name": normalized_name,
            "tool_id": tool_id,
            "input": tool_input,
            "timestamp": timestamp,
            "orchestrated_by": outer_tool_id,
        }
        entries.append(tool_entry)
        nested_tools.append(tool_entry)
        if tool_name == "update_plan":
            todos = parse_plan_snapshot(tool_input)
            if todos:
                entries.append(
                    {
                        "type": "todo_plan",
                        "role": "assistant",
                        "todos": todos,
                        "timestamp": timestamp,
                    }
                )

    if outer_tool_id:
        _wrapper_calls_store(ctx)[outer_tool_id] = nested_tools
    return entries


def _build_codex_exec_wrapper_results(
    payload: dict,
    timestamp: str,
    ctx: dict | None = None,
) -> list[dict] | None:
    """Complete synthetic nested calls when the outer wrapper returns."""
    outer_tool_id = str(payload.get("call_id") or "")
    nested_tools = _wrapper_calls_store(ctx).pop(outer_tool_id, None)
    if not nested_tools:
        return None
    output = _extract_codex_text_blocks(payload.get("output"))
    metadata = _parse_codex_tool_output_metadata(output)
    body = str(metadata.get("stdout") or output)
    results: list[dict] = []
    for index, tool in enumerate(nested_tools):
        tool_name = str(tool.get("tool_name") or "")
        content = body if index == 0 else ""
        if tool_name == "exec_command":
            result = {
                "type": "tool_result",
                "role": "tool",
                "tool_id": tool["tool_id"],
                "content": content,
                "is_error": False,
                "timestamp": timestamp,
                "result_kind": "exec_command",
                "status": "completed",
                "exit_code": metadata.get("exit_code"),
                "cwd": (tool.get("input") or {}).get("cwd") or "",
                "command": (tool.get("input") or {}).get("command") or "",
                "parsed_cmd": [],
                "duration_seconds": metadata.get("duration_seconds"),
                "stdout": content,
                "stderr": "",
                "process_id": "",
            }
            results.append(result)
            _append_codex_exec_sidecars(results, result)
        else:
            results.append(
                {
                    "type": "tool_result",
                    "role": "tool",
                    "tool_id": tool["tool_id"],
                    "content": content,
                    "is_error": False,
                    "timestamp": timestamp,
                    "result_kind": "custom_tool_call_output",
                }
            )
    return results


def _parse_codex_custom_tool_output(payload: dict, timestamp: str) -> dict:
    output_text = _extract_codex_text_blocks(payload.get("output"))
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


def new_codex_progress_state() -> dict[str, dict[str, str] | set[str]]:
    """Fresh Codex postprocess state for ONE stream.

    auto-16g9t: this replaced a process-global registry keyed by session
    dir — the live tailer and HTTP backfill reads used to mutate the SAME
    dict, so a scroll-up replay could mark tools completed (or consume
    pairing state) under the live stream's feet. State is now owned by the
    caller: the monitor holds one per session, read paths use a snapshot
    copy or a fresh one, and nothing is shared implicitly.
    """
    return {
        "tool_names": {},
        "exec_sessions": {},
        "write_calls": {},
        "completed_tools": set(),
        # tool_id → the tool_use's entry_ref. Split live batches synthesize
        # the semantic tool_use from the RESULT line; stamping it with the
        # remembered CALL-line ref makes it merge in place over the raw
        # Bash tile client-side — identical identity on live and cold paths.
        "use_refs": {},
    }


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
    result = {
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
    # Derived entries keep their source line's identity (auto-16g9t);
    # finalize_entry_refs re-numbers sub within the line afterwards.
    if entry.get("entry_ref") is not None:
        result["entry_ref"] = entry["entry_ref"]
    return result


def _append_codex_exec_sidecars(out: list[dict], progress: dict) -> None:
    """Emit typed entries hidden inside Codex exec progress output.

    Codex often reports short exec results as ``function_call_output`` rather
    than a later ``exec_command_end`` envelope. Keep the raw tool_result, but
    also surface graph mutations and graph-share attachments so viewer tiles
    do not depend on the provider choosing the final-envelope path (or
    exposing a nested tools.exec_command directly).
    """
    content = str(progress.get("stdout") or progress.get("content") or "")
    timestamp = str(progress.get("timestamp") or "")
    tool_id = str(progress.get("tool_id") or "")
    ref = progress.get("entry_ref")
    va = _upconvert_viewer_attachment(content, timestamp, tool_id=tool_id)
    if va:
        if ref is not None:
            va["entry_ref"] = ref
        out.append(va)
    sem = _upconvert_graph_result(content, timestamp, tool_id=tool_id)
    if sem:
        _enrich_semantic_tile(sem)
        if ref is not None:
            sem["entry_ref"] = ref
        out.append(sem)


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
    va = _upconvert_viewer_attachment(output, timestamp, tool_id=tool_id)
    sem = _upconvert_graph_result(output, timestamp, tool_id=tool_id)
    out = [result]
    if va:
        out.append(va)
    if sem:
        _enrich_semantic_tile(sem)
        out.append(sem)
    return out if len(out) > 1 else result


def parse_codex_log_line(line: str, ctx: dict | None = None) -> dict | list[dict] | None:
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
                identity = _codex_event_message_identity(payload, "user", text)
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
                    **identity,
                }
        if event_type == "agent_message":
            text = str(payload.get("message") or "")
            if text:
                identity = _codex_event_message_identity(payload, "assistant", text)
                return {
                    "type": "assistant_text",
                    "role": "assistant",
                    "content": text,
                    "timestamp": timestamp,
                    **identity,
                }
        if event_type == "exec_command_end":
            return _parse_codex_exec_end(payload, timestamp)
        if event_type == "patch_apply_end":
            return _parse_codex_patch_apply_end(payload, timestamp)
        if event_type == "task_started":
            return None
        if event_type == "task_complete":
            return {
                "type": "codex_task_complete",
                "role": "system",
                "timestamp": timestamp,
                "internal": True,
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
        if tool_name in {"exec", "functions.exec"}:
            nested = _build_codex_exec_wrapper_entries(payload, timestamp, ctx=ctx)
            if nested:
                return nested if len(nested) > 1 else nested[0]
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
        nested = _build_codex_exec_wrapper_results(payload, timestamp, ctx=ctx)
        if nested:
            return nested if len(nested) > 1 else nested[0]
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


# auto-eerfx: Claude TUI screen-state inference.
#
# These patterns are matched against the rectangular pane snapshot
# returned by ``tmux capture-pane -p``. The width is configurable
# (server.py defaults the terminal to -x 120 -y 40) so the patterns
# must regex on glyph shape, never absolute column position.
_CLAUDE_TRUST_DIALOG_RE = re.compile(
    # Wording drifts across Claude Code versions: older builds said
    # "trust the files in this folder"; v2.1.x says "trust the contents of
    # this directory". Match the stable core — "trust [the files/contents
    # in/of] this directory|folder" — rather than an exact phrase. Keying on
    # the old exact wording silently broke trust auto-confirm (operator had
    # to confirm by hand, 2026-06-04) — the same drift class as the composer
    # glyph below. A miss here now also files a self-repair bead (see the
    # screen-poll timeout in session_monitor) so the next wording change
    # surfaces itself instead of silently stalling startup.
    r"trust\s+(?:the\s+(?:files|contents)\s+(?:in|of)\s+)?this\s+(?:directory|folder)",
    re.IGNORECASE,
)
_CLAUDE_TRUST_CORNER_RE = re.compile(r"╭[─━]+╮")
# Confirm affordance. The newer numbered-list trust prompt ("1. Yes,
# continue   2. No, quit  /  Press enter to continue") may not draw the
# rounded ╭──╮ box, so the box alone is no longer a reliable corroborator.
# Either the box OR a visible confirm affordance corroborates the wording.
_CLAUDE_TRUST_CONFIRM_RE = re.compile(
    r"yes,?\s+(?:(?:i\s+)?trust(?:\s+this\s+(?:folder|directory))?|continue|proceed)"
    r"|press\s+enter\s+to\s+(?:confirm|continue|trust|proceed)"
    r"|(?:^|\n)\s*1\.\s*yes\b|no,?\s+quit",
    re.IGNORECASE,
)
_CLAUDE_PLANNING_RE = re.compile(
    r"(?:^|\n)\s*(?:plan mode|planning|Planning)\b",
    re.IGNORECASE,
)
_CLAUDE_AUTH_RE = re.compile(
    r"please run /login|/login to authenticate|invalid api key|"
    r"authentication required",
    re.IGNORECASE,
)
_CLAUDE_COMPOSER_PROMPT_RE = re.compile(
    # The composer renders as a prompt glyph on a near-bottom line, often
    # bracketed by ──── rules or a rounded box (╭ … ╰). The glyph has
    # changed across Claude Code versions:
    #   - older builds:        "> "
    #   - Claude Code v2.1.x:  "❯ " (U+276F), occasionally "› " (U+203A)
    # Match any of them at line start. Keying on the literal "> " alone
    # silently broke composer_ready when the TUI switched to "❯ " — every
    # session got stuck at harness_starting. The "⏵⏵ … (shift+tab to
    # cycle)" permission-mode footer is an additional corroborating idle
    # signal and is matched as a fallback so a blank prompt-line redraw
    # frame still resolves.
    r"(?:^|\n)[ \t]*[>❯›][ \t]"
    r"|⏵⏵[^\n]*\(shift\+tab to cycle\)",
)


def _claude_read_screen_state(
    pane_text: str,
    current_state: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[dict]]:
    """auto-eerfx: detect trust dialog / planning mode / composer-ready.

    See the docstring on the Protocol method for the contract.
    """
    prev = dict(current_state or {})
    new_state = dict(prev)
    keystrokes: list[dict] = []

    text = pane_text or ""

    trust_visible = bool(
        _CLAUDE_TRUST_DIALOG_RE.search(text)
        and (
            _CLAUDE_TRUST_CORNER_RE.search(text)
            or _CLAUDE_TRUST_CONFIRM_RE.search(text)
        )
    )
    new_state["confirming_trust_prompt"] = bool(prev.get("confirming_trust_prompt"))
    if trust_visible and not prev.get("confirming_trust_prompt"):
        # Newly-detected dialog. Send the confirm keystroke and flip
        # the flag so a re-poll while the keystroke is in flight does
        # not re-send. The fallback default-selected option in Claude's
        # TUI is "Yes, trust this directory" — Enter on that option
        # accepts. Empirical verification required: a sandbox test
        # spawning a real harness and asserting that this single
        # Enter clears the dialog within one poll interval.
        keystrokes.append({"kind": "key", "value": "C-m"})
        new_state["confirming_trust_prompt"] = True
    elif not trust_visible and prev.get("confirming_trust_prompt"):
        # Dialog cleared since last poll — confirm worked.
        new_state["confirming_trust_prompt"] = False

    new_state["in_planning_mode"] = bool(_CLAUDE_PLANNING_RE.search(text))

    if _CLAUDE_AUTH_RE.search(text):
        new_state["blocking_modal"] = "auth_required"
    else:
        new_state["blocking_modal"] = None

    # composer_ready is the cleanest single signal that the harness
    # will accept user input. The `> ` prompt is visible only when
    # Claude is at idle waiting for input — not during model thinking,
    # not during tool use, not when a dialog is up.
    composer_visible = bool(_CLAUDE_COMPOSER_PROMPT_RE.search(text))
    new_state["composer_ready"] = (
        composer_visible
        and not trust_visible
        and not new_state["blocking_modal"]
    )

    return new_state, keystrokes


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
    # auto-suvcp B6: the generation identity is written IN THE SAME UPDATE
    # as jsonl_path — every path writer establishes matching
    # generation+cursor atomically, so no reader can observe a link whose
    # generation belongs to a different inode.
    generation = None
    try:
        st = jsonl_path.stat()
        seq = dashboard_db.next_link_seq(tmux_name)
        generation = f"{st.st_dev}:{st.st_ino}:{seq}"
    except OSError:
        pass
    # W4 (auto-gah4g): link only — no ENRICH subprocess. This path is
    # always followed by SessionMonitor._eager_create_source (called from
    # _handle_jsonl_appeared right after resolve_session() returns), which
    # already sets graph_source_id in-process. The old link_and_enrich's
    # `subprocess.run(["graph", "ingest-session", ...], timeout=30)` was
    # therefore redundant here — same blocking-subprocess pattern as the
    # ENRICH loop retired in W2, just triggered per-session instead of at
    # startup.
    dashboard_db.update_jsonl_link(
        tmux_name,
        session_uuid=session_uuid,
        jsonl_path=str(jsonl_path),
        project=project,
        generation=generation,
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

    # auto-suvcp: attaching arms IN_MODIFY, which never fires for bytes
    # already on disk — request a catch-up drain so a quiet file's existing
    # content becomes operator-visible with no further write (the
    # attach-without-catch-up gap of incident auto-0807-225218).
    request_drain = getattr(monitor, "request_drain", None)
    if callable(request_drain):
        request_drain(tmux_name)


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
            # auto-suvcp B6: activation goes through the unified machine —
            # the persisted re-attach requests the catch-up drain (a burst
            # already on disk becomes visible with no further write) and
            # the registry publishes AFTER that drain (invariant 9), never
            # here at link time (a durable linked-but-zero card is the
            # CalStartupStall broadcast leg, host edition).
            observe = getattr(monitor, "observe_rollout", None)
            if callable(observe):
                observe(tmux_name, new_jsonl, source="host_watch")
            else:
                await monitor._broadcast_registry()
            return
        logger.warning("JSONL watcher timed out after %.0fs  tmux=%s", timeout, tmux_name)
