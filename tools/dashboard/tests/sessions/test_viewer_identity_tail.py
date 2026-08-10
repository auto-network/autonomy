"""
auto-16g9t Commit A — canonical entry identity + chain-aware tail windows.

Regression-first tests for the server half of "one identity, one merge,
truthful catch-up". Written against the NEW contract; on pre-fix master they
fail (missing params/fields, empty windows, cross-context state theft).

Contract under test:
  entry_ref = (file_uuid=filename stem, line_start_byte_offset, sub_index)
  stamped on every entry served by /api/session/{proj}/{id}/tail, on both
  the reverse-window and forward-delta modes, with the session's rollover
  chain (session_uuids order) exposed and cursors as (file, offset) pairs.

Symptom classes covered here (numbering from the bead):
  S2b — raw-line reverse windows rendering empty (renderable-entries rule)
  S3  — shared mutable codex parse/postprocess state across read paths
  S5b — forward cursor in a superseded file lies "caught up"; pre-rollover
        history unreachable from the reverse window
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest


# ── DB scaffolding (mirrors tests/sessions/test_consolidation_tail.py) ──


def _init_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE IF NOT EXISTS tmux_sessions (
        tmux_name TEXT PRIMARY KEY, session_uuid TEXT, graph_source_id TEXT,
        type TEXT NOT NULL, project TEXT NOT NULL, jsonl_path TEXT,
        bead_id TEXT, created_at REAL NOT NULL,
        state TEXT CHECK (state IN
            ('LAUNCHING','ACTIVE','STOPPING','ENDED','FAILED')),
        attention TEXT CHECK (attention IN
            ('tool_running','thinking','idle')),
        ended_at REAL,
        file_offset INTEGER DEFAULT 0, last_activity REAL,
        last_message TEXT DEFAULT '', entry_count INTEGER DEFAULT 0,
        context_tokens INTEGER DEFAULT 0, label TEXT DEFAULT '',
        topics TEXT DEFAULT '[]', role TEXT DEFAULT '',
        nag_enabled INTEGER DEFAULT 0, nag_interval INTEGER DEFAULT 15,
        nag_message TEXT DEFAULT '', nag_last_sent REAL DEFAULT 0,
        dispatch_nag INTEGER DEFAULT 0,
        resolution_dir TEXT, session_uuids TEXT DEFAULT '[]',
        curr_jsonl_file TEXT
    )""")
    conn.commit()
    conn.close()


def _insert_session(
    db_path: Path,
    *,
    tmux_name: str,
    jsonl_path: str,
    session_uuids: list[str] | None = None,
    state: str = "ACTIVE",
    session_type: str = "container",
    harness: str = "claude",
) -> None:
    stems = session_uuids if session_uuids is not None else [Path(jsonl_path).stem]
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tmux_sessions"
        " (tmux_name, type, project, jsonl_path, session_uuid,"
        "  resolution_dir, session_uuids, curr_jsonl_file, created_at,"
        "  state, ended_at, harness)"
        " VALUES (?, ?, 'autonomy', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tmux_name, session_type, jsonl_path, Path(jsonl_path).stem,
         str(Path(jsonl_path).parent), json.dumps(stems),
         jsonl_path, time.time(), state,
         time.time() if state in ("ENDED", "FAILED") else None, harness),
    )
    conn.commit()
    conn.close()


# ── JSONL builders with byte-offset accounting ──────────────────────────


def _claude_text_line(text: str, ts: str = "2026-08-10T00:00:00Z") -> str:
    return json.dumps({
        "type": "assistant",
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": text}]},
        "timestamp": ts,
    })


def _claude_noise_line() -> str:
    # parse_claude_log_line returns None for type=progress lines.
    return json.dumps({"type": "progress", "data": "x" * 40})


def _write_lines(path: Path, lines: list[str]) -> list[int]:
    """Write lines; return each line's start byte offset."""
    offsets = []
    off = 0
    with open(path, "wb") as fh:
        for line in lines:
            offsets.append(off)
            raw = (line + "\n").encode()
            fh.write(raw)
            off += len(raw)
    return offsets


@pytest.fixture
def tail_client(tmp_path):
    """Sync TestClient over the REAL tail endpoint with an isolated DB."""
    db_path = tmp_path / "dashboard.db"
    _init_db(db_path)

    os.environ.pop("DASHBOARD_MOCK", None)
    os.environ["DASHBOARD_DB"] = str(db_path)

    import importlib
    from tools.dashboard.dao import dashboard_db as db_mod
    importlib.reload(db_mod)
    from tools.dashboard import session_monitor as sm_mod
    importlib.reload(sm_mod)
    from tools.dashboard import server as server_mod
    importlib.reload(server_mod)

    from starlette.testclient import TestClient
    with TestClient(server_mod.app) as client:
        yield client, tmp_path, db_path

    os.environ.pop("DASHBOARD_DB", None)


def _refs(entries: list[dict]) -> list[tuple[str, int, int]]:
    out = []
    for e in entries:
        ref = e.get("entry_ref")
        assert ref is not None, f"entry missing entry_ref: {e.get('type')}"
        out.append((ref["file"], ref["off"], ref["sub"]))
    return out


# ── Entry-ref stamping + chain metadata ────────────────────────────────


class TestEntryRefStamping:

    def test_reverse_window_new_mode_stamps_refs_and_chain(self, tail_client):
        client, tmp_path, db_path = tail_client
        d = tmp_path / "s1"
        d.mkdir()
        jsonl = d / "aaaa-1111.jsonl"
        lines = [_claude_text_line(f"msg {i}") for i in range(5)]
        offsets = _write_lines(jsonl, lines)
        _insert_session(db_path, tmux_name="auto-ref1", jsonl_path=str(jsonl))

        resp = client.get("/api/session/autonomy/auto-ref1/tail?tail_entries=3")
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert data.get("chain") == ["aaaa-1111"], data.get("chain")
        assert len(data["entries"]) == 3
        refs = _refs(data["entries"])
        # Exact byte truth: the last 3 lines' start offsets, sub 0.
        assert refs == [("aaaa-1111", offsets[2], 0),
                        ("aaaa-1111", offsets[3], 0),
                        ("aaaa-1111", offsets[4], 0)]
        # The fake transport sequence is gone from real responses.
        assert "seq" not in data
        # Chain-aware scroll-up cursor.
        assert data["older_cursor"] == {"file": "aaaa-1111", "off": offsets[2]}
        assert data["has_more"] is True
        spans = data["window_spans"]
        assert spans == [{"file": "aaaa-1111", "from": offsets[2],
                          "to": os.path.getsize(jsonl)}]

    def test_legacy_tail_lines_mode_unchanged_plus_stamps(self, tail_client):
        client, tmp_path, db_path = tail_client
        d = tmp_path / "s2"
        d.mkdir()
        jsonl = d / "bbbb-2222.jsonl"
        offsets = _write_lines(jsonl, [_claude_text_line(f"m{i}") for i in range(4)])
        _insert_session(db_path, tmux_name="auto-ref2", jsonl_path=str(jsonl))

        resp = client.get("/api/session/autonomy/auto-ref2/tail?tail_lines=2")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # Legacy surface preserved for already-open tabs.
        assert data["older_before"] == offsets[2]
        assert data["has_more"] is True
        assert data["offset"] == os.path.getsize(jsonl)
        # Additive stamps ride along even on the legacy mode.
        assert _refs(data["entries"]) == [("bbbb-2222", offsets[2], 0),
                                          ("bbbb-2222", offsets[3], 0)]

    def test_legacy_forward_after_unchanged_plus_stamps(self, tail_client):
        client, tmp_path, db_path = tail_client
        d = tmp_path / "s3"
        d.mkdir()
        jsonl = d / "cccc-3333.jsonl"
        offsets = _write_lines(jsonl, [_claude_text_line(f"f{i}") for i in range(3)])
        _insert_session(db_path, tmux_name="auto-ref3", jsonl_path=str(jsonl))

        resp = client.get(
            f"/api/session/autonomy/auto-ref3/tail?after={offsets[1]}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["offset"] == os.path.getsize(jsonl)
        assert _refs(data["entries"]) == [("cccc-3333", offsets[1], 0),
                                          ("cccc-3333", offsets[2], 0)]


# ── S2b: renderable-entries rule ───────────────────────────────────────


class TestRenderableWindow:

    def test_noise_tail_still_returns_renderable_entries(self, tail_client):
        """A file whose trailing lines all parse to None must NOT produce an
        empty page — the window extends backward until entries render."""
        client, tmp_path, db_path = tail_client
        d = tmp_path / "s4"
        d.mkdir()
        jsonl = d / "dddd-4444.jsonl"
        lines = [_claude_text_line(f"real {i}") for i in range(3)]
        lines += [_claude_noise_line() for _ in range(50)]
        offsets = _write_lines(jsonl, lines)
        _insert_session(db_path, tmux_name="auto-noise", jsonl_path=str(jsonl))

        resp = client.get("/api/session/autonomy/auto-noise/tail?tail_entries=2")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        contents = [e.get("content") for e in data["entries"]]
        assert contents == ["real 1", "real 2"], (
            f"expected the window to reach past 50 noise lines, got {contents}"
        )
        assert _refs(data["entries"])[0] == ("dddd-4444", offsets[1], 0)
        assert data["has_more"] is True

    def test_page_of_pure_noise_does_not_dead_end(self, tail_client):
        """Scrolling back over a pure-noise region must keep paging into the
        real content behind it rather than returning an empty page."""
        client, tmp_path, db_path = tail_client
        d = tmp_path / "s5"
        d.mkdir()
        jsonl = d / "eeee-5555.jsonl"
        lines = [_claude_text_line("ancient")]
        lines += [_claude_noise_line() for _ in range(80)]
        lines += [_claude_text_line("recent")]
        _write_lines(jsonl, lines)
        _insert_session(db_path, tmux_name="auto-noise2", jsonl_path=str(jsonl))

        page1 = client.get(
            "/api/session/autonomy/auto-noise2/tail?tail_entries=1").json()
        assert [e.get("content") for e in page1["entries"]] == ["recent"]
        cur = page1["older_cursor"]
        page2 = client.get(
            "/api/session/autonomy/auto-noise2/tail?tail_entries=1"
            f"&before_file={cur['file']}&before={cur['off']}").json()
        assert [e.get("content") for e in page2["entries"]] == ["ancient"], (
            f"empty-page dead-end: {page2['entries']!r}"
        )
        assert page2["has_more"] is False


# ── S5b: chain-aware cursors across a rollover boundary ────────────────


def _two_file_chain(tmp_path, db_path, tmux_name="auto-chain"):
    d = tmp_path / tmux_name
    d.mkdir()
    file_a = d / "aaaa-old.jsonl"
    file_b = d / "bbbb-new.jsonl"
    offs_a = _write_lines(file_a, [_claude_text_line(f"A{i}") for i in range(3)])
    offs_b = _write_lines(file_b, [_claude_text_line(f"B{i}") for i in range(2)])
    _insert_session(
        db_path, tmux_name=tmux_name, jsonl_path=str(file_b),
        session_uuids=["aaaa-old", "bbbb-new"],
    )
    return file_a, file_b, offs_a, offs_b


class TestChainScrollback:

    def test_scroll_back_crosses_rollover_boundary(self, tail_client):
        client, tmp_path, db_path = tail_client
        file_a, file_b, offs_a, offs_b = _two_file_chain(tmp_path, db_path)

        page1 = client.get(
            "/api/session/autonomy/auto-chain/tail?tail_entries=4").json()
        assert page1["chain"] == ["aaaa-old", "bbbb-new"]
        contents = [e.get("content") for e in page1["entries"]]
        assert contents == ["A1", "A2", "B0", "B1"], contents
        refs = _refs(page1["entries"])
        assert refs == [("aaaa-old", offs_a[1], 0), ("aaaa-old", offs_a[2], 0),
                        ("bbbb-new", offs_b[0], 0), ("bbbb-new", offs_b[1], 0)]
        assert page1["has_more"] is True
        cur = page1["older_cursor"]
        assert cur == {"file": "aaaa-old", "off": offs_a[1]}

        page2 = client.get(
            "/api/session/autonomy/auto-chain/tail?tail_entries=4"
            f"&before_file={cur['file']}&before={cur['off']}").json()
        assert [e.get("content") for e in page2["entries"]] == ["A0"]
        assert page2["has_more"] is False, (
            "has_more must be False only at the true chain start — and here"
        )

    def test_forward_delta_from_superseded_file(self, tail_client):
        """A wake-up cursor left in a rolled-over file returns the remainder
        of that file and then the successors — never a caught-up lie."""
        client, tmp_path, db_path = tail_client
        file_a, file_b, offs_a, offs_b = _two_file_chain(
            tmp_path, db_path, tmux_name="auto-chain2")

        resp = client.get(
            "/api/session/autonomy/auto-chain2/tail"
            f"?after_file=aaaa-old&after={offs_a[2]}").json()
        contents = [e.get("content") for e in resp["entries"]]
        assert contents == ["A2", "B0", "B1"], (
            f"superseded-file cursor must chain forward, got {contents}"
        )
        assert resp["cursor"] == {"file": "bbbb-new",
                                  "off": os.path.getsize(file_b)}

    def test_forward_caught_up_is_small_and_truthful(self, tail_client):
        client, tmp_path, db_path = tail_client
        file_a, file_b, offs_a, offs_b = _two_file_chain(
            tmp_path, db_path, tmux_name="auto-chain3")
        size_b = os.path.getsize(file_b)

        resp = client.get(
            "/api/session/autonomy/auto-chain3/tail"
            f"?after_file=bbbb-new&after={size_b}").json()
        assert resp["entries"] == []
        assert resp["cursor"] == {"file": "bbbb-new", "off": size_b}
        assert "seq" not in resp

    def test_legacy_row_without_chain_degrades_gracefully(self, tail_client):
        """Pre-generation rows may have empty/sparse session_uuids — the new
        modes must degrade to single-file behavior, never 500."""
        client, tmp_path, db_path = tail_client
        d = tmp_path / "legacy"
        d.mkdir()
        jsonl = d / "ffff-6666.jsonl"
        offsets = _write_lines(jsonl, [_claude_text_line(f"L{i}") for i in range(2)])
        _insert_session(db_path, tmux_name="auto-legacy", jsonl_path=str(jsonl),
                        session_uuids=[])

        resp = client.get(
            "/api/session/autonomy/auto-legacy/tail?tail_entries=5")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["chain"] == ["ffff-6666"]
        assert [e.get("content") for e in data["entries"]] == ["L0", "L1"]
        assert data["has_more"] is False

    def test_forward_cursor_in_unknown_file_degrades(self, tail_client):
        """An after_file stem absent from the resolvable chain must fall back
        to serving the current file from 0 (self-healing), not error."""
        client, tmp_path, db_path = tail_client
        file_a, file_b, offs_a, offs_b = _two_file_chain(
            tmp_path, db_path, tmux_name="auto-chain4")

        resp = client.get(
            "/api/session/autonomy/auto-chain4/tail"
            "?after_file=gone-stem&after=17")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        contents = [e.get("content") for e in data["entries"]]
        assert contents == ["B0", "B1"], contents
        assert data["cursor"]["file"] == "bbbb-new"


# ── S3: read paths never share live mutable parse/postprocess state ────


CODEX_WRAPPER_CALL = json.dumps({
    "timestamp": "2026-08-10T01:00:00Z",
    "type": "response_item",
    "payload": {
        "type": "custom_tool_call",
        "name": "exec",
        "call_id": "call-77",
        "input": 'const r = await tools.exec_command({"cmd":"ls -la"});\ntext(r);',
    },
})

CODEX_WRAPPER_OUTPUT = json.dumps({
    "timestamp": "2026-08-10T01:00:02Z",
    "type": "response_item",
    "payload": {
        "type": "custom_tool_call_output",
        "call_id": "call-77",
        "output": "Exit code: 0\nWall time: 0.1 seconds\nOutput:\nfiles",
    },
})


class TestReadPathIsolation:

    def test_codex_wrapper_pairing_isolated_between_contexts(self):
        """An HTTP replay parsing the same lines must not steal the live
        stream's pending wrapper pair (the force-completed split-tool-call
        mechanism)."""
        from tools.dashboard.session_harness import CODEX_HARNESS

        live_ctx: dict = {}
        http_ctx: dict = {}

        call_entries = CODEX_HARNESS.parse_line(CODEX_WRAPPER_CALL, ctx=live_ctx)
        assert call_entries, "wrapper call must parse"

        # Interleaved HTTP replay of the output line with its OWN context —
        # on pre-fix master this popped the process-global pending pair.
        CODEX_HARNESS.parse_line(CODEX_WRAPPER_OUTPUT, ctx=http_ctx)

        live_out = CODEX_HARNESS.parse_line(CODEX_WRAPPER_OUTPUT, ctx=live_ctx)
        outs = live_out if isinstance(live_out, list) else [live_out]
        kinds = {e.get("result_kind") for e in outs if e}
        assert "exec_command" in kinds, (
            f"live stream lost its wrapper pairing to the HTTP replay: {outs!r}"
        )

    def test_codex_postprocess_state_isolated(self, tmp_path):
        """postprocess with an explicit live state continues that stream;
        postprocess without one starts fresh — and neither touches the
        other (the process-global progress dict is gone)."""
        from tools.dashboard import session_harness as sh

        session_dir = tmp_path / "codex-sess"
        session_dir.mkdir()

        final_result = {
            "type": "tool_result", "role": "tool", "tool_id": "T1",
            "content": "done", "is_error": False,
            "timestamp": "2026-08-10T01:00:01Z",
            "result_kind": "exec_command", "status": "completed",
            "process_id": "",
        }
        late_progress = {
            "type": "tool_result", "role": "tool", "tool_id": "T1",
            "content": "partial", "is_error": False, "stdout": "partial",
            "timestamp": "2026-08-10T01:00:02Z",
            "result_kind": "function_call_output", "status": "running",
            "process_id": "9",
        }
        tool_use = {
            "type": "tool_use", "role": "assistant", "tool_name": "exec_command",
            "tool_id": "T1", "input": {"command": "sleep 1"},
            "timestamp": "2026-08-10T01:00:00Z",
        }

        live_state = sh.new_codex_progress_state()
        sh.postprocess_codex_entries(
            [dict(tool_use), dict(final_result)],
            session_dir=session_dir, state=live_state,
        )
        assert "T1" in live_state["completed_tools"]

        # An independent read (no explicit state) must not see T1 completed…
        fresh = sh.postprocess_codex_entries(
            [dict(tool_use), dict(late_progress)], session_dir=session_dir,
        )
        assert any(e.get("tool_id") == "T1" and e.get("type") == "tool_result"
                   for e in fresh), "fresh read wrongly inherited live state"

        # …and must not have mutated the live stream's state either.
        assert "T1" in live_state["completed_tools"]
        follow = sh.postprocess_codex_entries(
            [dict(late_progress)], session_dir=session_dir, state=live_state,
        )
        assert not any(e.get("tool_id") == "T1" and e.get("status") == "running"
                       for e in follow), (
            "live continuation lost its completed_tools suppression"
        )


# ── Round-1 review regressions (codex report, all reproduced pre-fix) ──


def _codex_line(payload: dict, ts: str = "2026-08-10T01:00:00Z") -> str:
    return json.dumps({"timestamp": ts, "type": "response_item", "payload": payload})


class TestRound1Blockers:

    def test_b1_partial_line_cold_open_never_commits_into_it(self, tail_client):
        """B1: a cold-open taken mid-write must anchor at the last COMPLETE
        newline; after the line completes, the forward fetch from that
        anchor must deliver it (pre-fix: committed at physical EOF → the
        completed line was skipped forever)."""
        client, tmp_path, db_path = tail_client
        d = tmp_path / "b1"
        d.mkdir()
        jsonl = d / "part-1111.jsonl"
        line_a = _claude_text_line("complete A")
        line_b = _claude_text_line("late B")
        with open(jsonl, "w") as fh:
            fh.write(line_a + "\n")
            fh.write(line_b[: len(line_b) // 2])   # writer mid-line
        _insert_session(db_path, tmux_name="auto-b1", jsonl_path=str(jsonl))

        cold = client.get(
            "/api/session/autonomy/auto-b1/tail?tail_entries=10").json()
        complete_off = len(line_a) + 1
        assert [e["content"] for e in cold["entries"]] == ["complete A"]
        spans = cold["window_spans"]
        assert spans[-1]["to"] == complete_off, (
            f"window span must end at the last complete newline, got {spans}"
        )

        # The writer finishes line B (plus newline) while SSE is missed.
        with open(jsonl, "a") as fh:
            fh.write(line_b[len(line_b) // 2:] + "\n")

        wake = client.get(
            "/api/session/autonomy/auto-b1/tail"
            f"?after_file=part-1111&after={spans[-1]['to']}").json()
        assert [e["content"] for e in wake["entries"]] == ["late B"], (
            "the completed line must be recoverable from the cold anchor"
        )

    def test_b2_forward_cap_never_strands_bytes(self, tail_client, monkeypatch):
        """B2a: a newline-clipped capped response must keep has_more_forward
        true until the cursor really reaches the complete end."""
        client, tmp_path, db_path = tail_client
        from tools.dashboard import server as server_mod
        import sys
        srv = sys.modules[server_mod.__name__]
        monkeypatch.setattr(srv, "_FORWARD_CAP_BYTES", 4096)

        d = tmp_path / "b2"
        d.mkdir()
        jsonl = d / "cap-2222.jsonl"
        _write_lines(jsonl, [_claude_text_line(f"line {i} " + "x" * 100)
                             for i in range(60)])
        _insert_session(db_path, tmux_name="auto-b2", jsonl_path=str(jsonl))

        got: list = []
        cursor = {"file": "cap-2222", "off": 0}
        rounds = 0
        while rounds < 30:
            resp = client.get(
                "/api/session/autonomy/auto-b2/tail"
                f"?after_file={cursor['file']}&after={cursor['off']}").json()
            got.extend(resp["entries"])
            assert resp["cursor"]["off"] > cursor["off"] or not resp["entries"], (
                "every non-empty response must advance the cursor"
            )
            cursor = resp["cursor"]
            rounds += 1
            if not resp["has_more_forward"]:
                break
        assert rounds > 1, "cap must have forced multiple rounds"
        assert len(got) == 60, f"stranded entries: got {len(got)}/60"
        assert cursor["off"] == os.path.getsize(jsonl)

    def test_b2_single_over_cap_line_still_progresses(self, tail_client, monkeypatch):
        """B2b: one complete line larger than the cap is served whole —
        the cursor always moves (pre-fix: has_more with an unmoved cursor
        → client hot-loop)."""
        client, tmp_path, db_path = tail_client
        from tools.dashboard import server as server_mod
        import sys
        srv = sys.modules[server_mod.__name__]
        monkeypatch.setattr(srv, "_FORWARD_CAP_BYTES", 2048)

        d = tmp_path / "b2b"
        d.mkdir()
        jsonl = d / "mono-3333.jsonl"
        _write_lines(jsonl, [_claude_text_line("y" * 5000)])
        _insert_session(db_path, tmux_name="auto-b2b", jsonl_path=str(jsonl))

        resp = client.get(
            "/api/session/autonomy/auto-b2b/tail?after_file=mono-3333&after=0").json()
        assert len(resp["entries"]) == 1
        assert resp["cursor"]["off"] == os.path.getsize(jsonl)
        assert resp["has_more_forward"] is False

    def test_b3_forward_replay_preserves_progress_entries(self, tail_client):
        """B3: a catch-up over a range containing a running progress line
        must serve it exactly as the live stream did — cursor-state
        reconstruction, not the EOF snapshot that suppressed it."""
        client, tmp_path, db_path = tail_client
        d = tmp_path / "b3"
        d.mkdir()
        jsonl = d / "rollout-2026-08-10T01-00-00-b3b3.jsonl"
        lines = [
            _codex_line({"type": "function_call", "name": "exec_command",
                         "call_id": "T1",
                         "arguments": json.dumps({"cmd": "sleep 5"})}),
            _codex_line({"type": "function_call_output", "call_id": "T1",
                         "output": "Process running with session ID 7\nOutput:\npartial"}),
            json.dumps({"timestamp": "2026-08-10T01:00:02Z", "type": "event_msg",
                        "payload": {"type": "exec_command_end", "call_id": "T1",
                                    "stdout": "done", "stderr": "",
                                    "exit_code": 0, "duration": {"secs": 2, "nanos": 0}}}),
        ]
        offsets = _write_lines(jsonl, lines)
        _insert_session(db_path, tmux_name="auto-b3", jsonl_path=str(jsonl), harness="codex")

        # Cursor sits after the call line; running + final were missed.
        resp = client.get(
            "/api/session/autonomy/auto-b3/tail"
            f"?after_file={jsonl.stem}&after={offsets[1]}").json()
        results = [e for e in resp["entries"] if e.get("type") == "tool_result"]
        statuses = [(r["entry_ref"]["off"], r.get("status")) for r in results]
        assert (offsets[1], "running") in statuses, (
            f"running progress entry must survive the replay, got {statuses}"
        )

    def test_b4_forward_replay_reconstructs_wrapper_pairing(self, tail_client):
        """B4: an output line fetched after its wrapper call line (cursor
        between them) must parse against the reconstructed pending pair —
        never mint the same ref with degraded content."""
        client, tmp_path, db_path = tail_client
        d = tmp_path / "b4"
        d.mkdir()
        jsonl = d / "rollout-2026-08-10T02-00-00-b4b4.jsonl"
        lines = [
            _codex_line({"type": "custom_tool_call", "name": "exec",
                         "call_id": "W1",
                         "input": 'const r = await tools.exec_command({"cmd":"ls"});\ntext(r);'}),
            _codex_line({"type": "custom_tool_call_output", "call_id": "W1",
                         "output": "Exit code: 0\nWall time: 0.1 seconds\nOutput:\nok"},
                        ts="2026-08-10T02:00:01Z"),
        ]
        offsets = _write_lines(jsonl, lines)
        _insert_session(db_path, tmux_name="auto-b4", jsonl_path=str(jsonl), harness="codex")

        resp = client.get(
            "/api/session/autonomy/auto-b4/tail"
            f"?after_file={jsonl.stem}&after={offsets[1]}").json()
        kinds = {e.get("result_kind") for e in resp["entries"]
                 if e.get("type") == "tool_result"}
        assert "exec_command" in kinds, (
            f"wrapper pairing must be reconstructed through the cursor, got {kinds}"
        )

    def test_b5_split_vs_combined_semantic_refs_equal(self, tmp_path):
        """B5: the semantic tool_use upgrade carries the CALL line's ref on
        BOTH the combined-cold and split-live paths (pre-fix: cold left it
        refless → client-synthetic ref → duplicate tile)."""
        from tools.dashboard import session_harness as sh

        def mk_call():
            return {"type": "tool_use", "role": "assistant",
                    "tool_name": "exec_command", "tool_id": "C1",
                    "input": {"command": "sed -n '1,2p' a.py", "cmd": "sed -n '1,2p' a.py"},
                    "timestamp": "t0", "entry_ref": {"file": "f", "off": 500, "sub": 0}}

        def mk_result():
            return {"type": "tool_result", "role": "tool", "tool_id": "C1",
                    "content": "l1\nl2\n", "is_error": False, "timestamp": "t1",
                    "result_kind": "exec_command", "status": "completed",
                    "parsed_cmd": [{"type": "read", "name": "a.py"}], "cwd": "",
                    "command": "sed -n '1,2p' a.py", "stdout": "l1\nl2\n",
                    "stderr": "", "process_id": "", "exit_code": 0,
                    "entry_ref": {"file": "f", "off": 600, "sub": 0}}

        cold = sh.postprocess_codex_entries(
            [mk_call(), mk_result()], state=sh.new_codex_progress_state())
        sh.finalize_entry_refs(cold)
        cold_use = [e for e in cold if e.get("type") == "tool_use"][0]

        st = sh.new_codex_progress_state()
        b1 = sh.postprocess_codex_entries([mk_call()], state=st)
        sh.finalize_entry_refs(b1)
        b2 = sh.postprocess_codex_entries([mk_result()], state=st)
        sh.finalize_entry_refs(b2)
        live_uses = [e for e in b1 + b2
                     if e.get("type") == "tool_use" and e.get("semantic_from_exec")]

        want = {"file": "f", "off": 500, "sub": 0}
        assert cold_use.get("entry_ref") == want, cold_use.get("entry_ref")
        assert any(u.get("entry_ref") == want for u in live_uses), live_uses

    def test_b6_bead_dispatch_tail_stamps_refs(self, tail_client, monkeypatch):
        """B6: the bead-style dispatch branch serves canonical refs and a
        complete-line-clamped cursor like every other path."""
        client, tmp_path, db_path = tail_client
        from tools.dashboard import server as server_mod
        import sys
        srv = sys.modules[server_mod.__name__]
        runs = tmp_path / "agent-runs"
        monkeypatch.setattr(srv, "AGENT_RUNS_DIR", runs)

        run_name = "auto-bead-0810-121212"
        sess_dir = runs / run_name / "sessions" / "autonomy"
        sess_dir.mkdir(parents=True)
        jsonl = sess_dir / "beadsess-7777.jsonl"
        offsets = _write_lines(jsonl, [_claude_text_line(f"d{i}") for i in range(2)])
        with open(jsonl, "a") as fh:
            fh.write('{"partial":')   # trailing partial line

        resp = client.get(f"/api/dispatch/tail/{run_name}?after=0")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert _refs(data["entries"]) == [("beadsess-7777", offsets[0], 0),
                                          ("beadsess-7777", offsets[1], 0)]
        assert data["cursor"] == {"file": "beadsess-7777",
                                  "off": offsets[1] + len(_claude_text_line("d1")) + 1}

    def test_s1_blank_lines_never_corrupt_offsets(self, tail_client):
        """S1: a blank line inside the window must not shift the stamped
        offsets (pre-fix: compaction moved every earlier ref by the blank
        bytes)."""
        client, tmp_path, db_path = tail_client
        d = tmp_path / "s1"
        d.mkdir()
        jsonl = d / "blank-8888.jsonl"
        l1 = _claude_text_line("first")
        l2 = _claude_text_line("second")
        with open(jsonl, "w") as fh:
            fh.write(l1 + "\n\n" + l2 + "\n")
        _insert_session(db_path, tmux_name="auto-s1", jsonl_path=str(jsonl))

        resp = client.get(
            "/api/session/autonomy/auto-s1/tail?tail_entries=10").json()
        assert _refs(resp["entries"]) == [
            ("blank-8888", 0, 0),
            ("blank-8888", len(l1) + 2, 0),
        ]


# ── Round-2 review regressions (both reproduced pre-fix) ───────────────


class TestRound2Blockers:

    def test_rb1_over_64k_partial_after_complete_line(self, tail_client):
        """RB1: an unterminated trailing line >64KiB must not be mapped to
        'complete at physical EOF' — that re-opened the B1 permanent-loss
        timeline at production line sizes (executed pre-fix: helper
        returned 140,014 for a file whose true complete offset is 8)."""
        client, tmp_path, db_path = tail_client
        from tools.dashboard import server as server_mod
        import sys
        srv = sys.modules[server_mod.__name__]

        d = tmp_path / "rb1"
        d.mkdir()
        jsonl = d / "rb1-aaaa.jsonl"
        line_a = _claude_text_line("complete A")
        with open(jsonl, "w") as fh:
            fh.write(line_a + "\n")
            fh.write('{"b":"' + "x" * 140_000)   # >64KiB, no newline
        _insert_session(db_path, tmux_name="auto-rb1", jsonl_path=str(jsonl))

        true_complete = len(line_a) + 1
        assert srv._last_complete_offset_in(jsonl) == true_complete

        cold = client.get(
            "/api/session/autonomy/auto-rb1/tail?tail_entries=10").json()
        assert [e["content"] for e in cold["entries"]] == ["complete A"]
        assert cold["window_spans"][-1]["to"] == true_complete, (
            "cold-open must anchor before the giant partial line"
        )

        # The giant line completes off-screen; the anchor must recover it.
        with open(jsonl, "a") as fh:
            fh.write('"}\n')
        wake = client.get(
            "/api/session/autonomy/auto-rb1/tail"
            f"?after_file=rb1-aaaa&after={true_complete}").json()
        assert wake["cursor"]["off"] == os.path.getsize(jsonl)

    def test_rb1_sole_over_64k_partial_file(self, tail_client):
        """RB1 second shape: a file that is ONLY a >64KiB partial line has
        complete offset 0 — never its physical size."""
        client, tmp_path, db_path = tail_client
        from tools.dashboard import server as server_mod
        import sys
        srv = sys.modules[server_mod.__name__]

        d = tmp_path / "rb1b"
        d.mkdir()
        jsonl = d / "rb1b-bbbb.jsonl"
        jsonl.write_text('{"c":"' + "y" * 140_000)
        _insert_session(db_path, tmux_name="auto-rb1b", jsonl_path=str(jsonl))

        assert srv._last_complete_offset_in(jsonl) == 0
        cold = client.get(
            "/api/session/autonomy/auto-rb1b/tail?tail_entries=10").json()
        assert cold["entries"] == []
        assert cold["has_more"] is False

    def test_rb2_replay_claims_match_live_for_repeated_descriptions(self, tail_client):
        """RB2 (the execution reviewer's fixture shape): two Agent calls
        with the SAME description; the cursor sits between them. The
        replay must attach the SECOND subagent's tool_calls to the second
        Agent — reconstruction replays the claim allocation through the
        prefix (pre-fix: claimed_subagents arrived empty, the replay
        re-claimed subagent #1 and reported 1 instead of 3)."""
        client, tmp_path, db_path = tail_client
        d = tmp_path / "rb2"
        d.mkdir()
        jsonl = d / "rb2-cccc.jsonl"

        def agent_use_line(tid):
            return json.dumps({"type": "assistant", "timestamp": "t",
                               "message": {"role": "assistant", "content": [
                                   {"type": "tool_use", "name": "Agent", "id": tid,
                                    "input": {"description": "explore repo",
                                              "prompt": "go"}}]}})

        def agent_result_line(tid):
            return json.dumps({"type": "user", "timestamp": "t",
                               "message": {"role": "user", "content": [
                                   {"type": "tool_result", "tool_use_id": tid,
                                    "content": "done"}]}})

        lines = [agent_use_line("A1"), agent_result_line("A1"),
                 agent_use_line("A2"), agent_result_line("A2")]
        offsets = _write_lines(jsonl, lines)
        _insert_session(db_path, tmux_name="auto-rb2", jsonl_path=str(jsonl))

        sub = d / "rb2-cccc" / "subagents"
        sub.mkdir(parents=True)
        (sub / "01.meta.json").write_text(json.dumps({"description": "explore repo"}))
        (sub / "01.jsonl").write_text(json.dumps(
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Bash", "id": "x1", "input": {}}]}}) + "\n")
        (sub / "02.meta.json").write_text(json.dumps({"description": "explore repo"}))
        with open(sub / "02.jsonl", "w") as fh:
            for i in range(3):
                fh.write(json.dumps(
                    {"type": "assistant", "message": {"role": "assistant", "content": [
                        {"type": "tool_use", "name": "Bash", "id": f"y{i}",
                         "input": {}}]}}) + "\n")

        # Cursor between the two Agent calls: the replay covers A2 only.
        resp = client.get(
            "/api/session/autonomy/auto-rb2/tail"
            f"?after_file=rb2-cccc&after={offsets[2]}").json()
        a2_results = [e for e in resp["entries"]
                      if e.get("type") == "tool_result" and e.get("tool_id") == "A2"]
        assert a2_results, resp["entries"]
        assert a2_results[0].get("tool_calls") == 3, (
            f"A2 must claim the SECOND subagent (3 tool calls), got "
            f"{a2_results[0].get('tool_calls')} — the prefix claim was not replayed"
        )


# ── Acceptance: cold-open of a very long session is one cheap request ──


class TestColdOpenPerformance:

    def test_cold_open_long_session_under_250ms(self, tail_client):
        """Acceptance gate: latest window of a 20k-line session in one
        request, <250ms server-time (measured 3-11ms; generous margin
        against CI noise via best-of-3)."""
        import time as _time
        client, tmp_path, db_path = tail_client
        d = tmp_path / "big"
        d.mkdir()
        jsonl = d / "big-9999.jsonl"
        with open(jsonl, "w") as fh:
            for i in range(20000):
                fh.write(_claude_text_line(f"message {i} padded out to a realistic transcript line length") + "\n")
        _insert_session(db_path, tmux_name="auto-big", jsonl_path=str(jsonl))

        best = float("inf")
        for _ in range(3):
            t0 = _time.perf_counter()
            resp = client.get("/api/session/autonomy/auto-big/tail?tail_entries=200")
            best = min(best, (_time.perf_counter() - t0) * 1000)
            assert resp.status_code == 200
            assert len(resp.json()["entries"]) == 200
        assert best < 250, f"cold-open took {best:.1f}ms (gate: 250ms)"


# ── SSE broadcast spans + entry refs (monitor publish path) ────────────


class TestBroadcastSpans:

    def test_parse_window_stamps_refs_with_base_offset(self, tmp_path):
        """SessionMonitor._parse_window threads (stem, base_offset) into
        per-entry refs — the same identity the HTTP paths stamp."""
        from tools.dashboard import session_monitor as sm

        lines = [_claude_text_line("w0"), _claude_text_line("w1")]
        blob = ("\n".join(lines) + "\n").encode()
        row = {"harness": "claude", "harness_state": "{}",
               "context_tokens": 0}
        mon = object.__new__(sm.SessionMonitor)
        window = mon._parse_window(
            row, blob, stem="wwww-7777", base_offset=1000, parse_ctx={},
        )
        refs = _refs(window["entries"])
        assert refs[0] == ("wwww-7777", 1000, 0)
        assert refs[1] == ("wwww-7777", 1000 + len(lines[0]) + 1, 0)
