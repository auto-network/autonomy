"""Renderer extraction regression tests.

Verifies the data contract between the server-side parser and the client-side
renderer (session-viewer.js).  These tests run against the CURRENT code and
will continue to pass after auto-q2ub extracts the 32 shared rendering methods
to session-renderer.js.

Test strategy: parse JSONL sequences through _parse_jsonl_entry() and verify
the output entries have the exact types, fields, and structure that the
renderer's methods depend on:
  - _buildDisplayEntries() tool grouper
  - headline() per-tool headline extraction
  - metaDisplay() badge computation (duration, error, line count)
  - hasGap() role-transition gap detection
  - semantic tile rendering (enriched vs fallback)
"""

import json

import pytest

from tools.dashboard.server import _parse_jsonl_entry, _dedup_queued_entries


# ── Helpers ───────────────────────────────────────────────────────────

def _ts(n):
    """Generate ordered ISO timestamps: _ts(0)→'...00Z', _ts(5)→'...05Z'."""
    return f"2026-03-24T12:00:{n:02d}Z"


def _line(obj: dict) -> str:
    return json.dumps(obj)


def _parse_sequence(raw_entries: list[dict]) -> list[dict]:
    """Parse a sequence of JSONL dicts through the full server pipeline."""
    entries = []
    for raw in raw_entries:
        parsed = _parse_jsonl_entry(_line(raw))
        if parsed is None:
            continue
        if isinstance(parsed, list):
            entries.extend(parsed)
        else:
            entries.append(parsed)
    return _dedup_queued_entries(entries)


def _make_tool_use(tool_name, tool_id, input_dict, ts=None):
    """Create an assistant JSONL entry with a single tool_use block."""
    return {
        "parentUuid": f"uuid-{tool_id}", "isSidechain": False, "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tool_id, "name": tool_name, "input": input_dict},
        ]},
        "timestamp": ts or _ts(0),
    }


def _make_tool_result(tool_id, content, is_error=False, ts=None):
    """Create a tool_result JSONL entry."""
    return {
        "type": "tool_result", "toolUseId": tool_id,
        "message": {"role": "user", "content": content},
        "is_error": is_error,
        "timestamp": ts or _ts(1),
    }


def _make_assistant_text(text, ts=None):
    """Create an assistant JSONL entry with a single text block."""
    return {
        "parentUuid": "uuid-text", "isSidechain": False, "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": text},
        ]},
        "timestamp": ts or _ts(0),
    }


def _make_user_message(text, ts=None):
    """Create a user JSONL entry with string content."""
    return {
        "parentUuid": "uuid-user", "isSidechain": False, "type": "user",
        "message": {"role": "user", "content": text},
        "timestamp": ts or _ts(0),
    }


# ── TestToolGrouping ─────────────────────────────────────────────────

class TestToolGrouping:
    """Verify parsed entries have the structure the JS tool grouper needs.

    Client-side _buildDisplayEntries() groups consecutive tool_use entries
    with the same tool_name when tool_name is in the _GROUPABLE set
    ('Bash', 'Read', 'Edit', 'Grep', 'Glob').  These tests verify the
    server produces entries with correct type and tool_name for grouping.
    """

    def test_consecutive_same_tool_grouped(self):
        """3 consecutive Read entries → 3 entries with type=tool_use, tool_name=Read.

        JS grouper collapses these into 1 tool_group with 3 children.
        """
        entries = _parse_sequence([
            _make_tool_use("Read", "tu_r1", {"file_path": "/a.py"}, _ts(0)),
            _make_tool_use("Read", "tu_r2", {"file_path": "/b.py"}, _ts(1)),
            _make_tool_use("Read", "tu_r3", {"file_path": "/c.py"}, _ts(2)),
        ])
        assert len(entries) == 3
        for e in entries:
            assert e["type"] == "tool_use"
            assert e["tool_name"] == "Read"
        # Verify they arrive in order — grouper depends on adjacency
        assert entries[0]["tool_id"] == "tu_r1"
        assert entries[1]["tool_id"] == "tu_r2"
        assert entries[2]["tool_id"] == "tu_r3"

    def test_mixed_tools_not_grouped(self):
        """Read then Bash then Read → 3 separate entries with different tool_names.

        JS grouper requires consecutive SAME tool — these stay separate.
        """
        entries = _parse_sequence([
            _make_tool_use("Read", "tu_r1", {"file_path": "/a.py"}, _ts(0)),
            _make_tool_use("Bash", "tu_b1", {"command": "ls"}, _ts(1)),
            _make_tool_use("Read", "tu_r2", {"file_path": "/b.py"}, _ts(2)),
        ])
        assert len(entries) == 3
        assert entries[0]["tool_name"] == "Read"
        assert entries[1]["tool_name"] == "Bash"
        assert entries[2]["tool_name"] == "Read"

    def test_groupable_tools_only(self):
        """Bash, Read, Edit, Grep, Glob produce type=tool_use; Agent, Write too.

        All tools produce tool_use entries.  The JS grouper filters by name
        using _GROUPABLE = {Bash, Read, Edit, Grep, Glob}.
        """
        tools_and_inputs = [
            ("Bash",  {"command": "echo test"}),
            ("Read",  {"file_path": "/test.py"}),
            ("Edit",  {"file_path": "/test.py", "old_string": "a", "new_string": "b"}),
            ("Grep",  {"pattern": "def main"}),
            ("Glob",  {"pattern": "**/*.py"}),
            ("Agent", {"description": "Search code", "prompt": "Find the bug"}),
            ("Write", {"file_path": "/new.py", "content": "pass"}),
        ]
        for tool_name, input_dict in tools_and_inputs:
            entries = _parse_sequence([
                _make_tool_use(tool_name, f"tu_{tool_name.lower()}", input_dict),
            ])
            assert len(entries) == 1
            assert entries[0]["type"] == "tool_use"
            assert entries[0]["tool_name"] == tool_name

    def test_single_tool_not_grouped(self):
        """1 Read entry → regular tool_use, not a group.

        JS grouper requires 2+ consecutive same-tool entries to form a group.
        """
        entries = _parse_sequence([
            _make_tool_use("Read", "tu_r1", {"file_path": "/a.py"}),
        ])
        assert len(entries) == 1
        assert entries[0]["type"] == "tool_use"

    def test_assistant_text_breaks_group(self):
        """Read, Read, assistant_text, Read → text separates the tool runs.

        JS grouper sees: [Read, Read, text, Read] → group(2) + text + single.
        """
        entries = _parse_sequence([
            _make_tool_use("Read", "tu_r1", {"file_path": "/a.py"}, _ts(0)),
            _make_tool_use("Read", "tu_r2", {"file_path": "/b.py"}, _ts(1)),
            _make_assistant_text("Let me check the other file.", _ts(2)),
            _make_tool_use("Read", "tu_r3", {"file_path": "/c.py"}, _ts(3)),
        ])
        types = [e["type"] for e in entries]
        assert types == ["tool_use", "tool_use", "assistant_text", "tool_use"]
        assert entries[0]["tool_name"] == "Read"
        assert entries[1]["tool_name"] == "Read"
        assert entries[3]["tool_name"] == "Read"


# ── TestHeadlineExtraction ──────────────────────────────────────────

class TestHeadlineExtraction:
    """Verify tool_use entries preserve the input fields headline() reads.

    Client-side headline() extracts a display headline from entry.input
    based on tool_name:
      Bash  → input.description || input.command
      Read  → _smartPath(input.file_path)
      Edit  → _smartPath(input.file_path)
      Grep  → input.pattern [+ ' in ' + input.path]
      Glob  → input.pattern
      Agent → input.description || input.prompt[:60]
    """

    def test_bash_headline_is_command(self):
        """Bash input.command → headline shows command (truncated)."""
        entries = _parse_sequence([
            _make_tool_use("Bash", "tu_bash", {
                "command": "git status --short",
                "description": "Show working tree status",
            }),
        ])
        inp = entries[0]["input"]
        assert inp["command"] == "git status --short"
        # headline() prefers description over command
        assert inp["description"] == "Show working tree status"

    def test_read_headline_is_filepath(self):
        """Read input.file_path → headline shows path."""
        entries = _parse_sequence([
            _make_tool_use("Read", "tu_read", {
                "file_path": "/workspace/repo/tools/dashboard/server.py",
            }),
        ])
        assert entries[0]["input"]["file_path"] == "/workspace/repo/tools/dashboard/server.py"

    def test_edit_headline_is_filepath(self):
        """Edit input.file_path → headline shows path."""
        entries = _parse_sequence([
            _make_tool_use("Edit", "tu_edit", {
                "file_path": "/workspace/repo/tools/dashboard/server.py",
                "old_string": "def foo():",
                "new_string": "def bar():",
            }),
        ])
        assert entries[0]["input"]["file_path"] == "/workspace/repo/tools/dashboard/server.py"

    def test_grep_headline_is_pattern(self):
        """Grep input.pattern → headline shows pattern."""
        entries = _parse_sequence([
            _make_tool_use("Grep", "tu_grep", {
                "pattern": "def main",
                "path": "/workspace/repo",
            }),
        ])
        inp = entries[0]["input"]
        assert inp["pattern"] == "def main"
        # headline() appends ' in ' + path when present
        assert inp["path"] == "/workspace/repo"

    def test_glob_headline_is_pattern(self):
        """Glob input.pattern → headline shows pattern."""
        entries = _parse_sequence([
            _make_tool_use("Glob", "tu_glob", {"pattern": "**/*.py"}),
        ])
        assert entries[0]["input"]["pattern"] == "**/*.py"

    def test_agent_headline_is_description(self):
        """Agent input.description → headline shows description."""
        entries = _parse_sequence([
            _make_tool_use("Agent", "tu_agent", {
                "description": "Search for auth code",
                "prompt": "Find all authentication related code in the project",
            }),
        ])
        inp = entries[0]["input"]
        assert inp["description"] == "Search for auth code"
        # headline() falls back to prompt[:60] when description is missing
        assert inp["prompt"].startswith("Find all authentication")


# ── TestMetaBadges ──────────────────────────────────────────────────

class TestMetaBadges:
    """Verify entries have the fields needed for meta badge computation.

    Client-side metaDisplay() computes badges from:
      - Duration: tool_use.timestamp + paired tool_result.timestamp
      - Error:    tool_result.is_error
      - Lines:    tool_result.content (counted via _countLines)
    Pairing uses tool_id: tool_use.tool_id === tool_result.tool_id.
    """

    def test_duration_from_tool_pair(self):
        """tool_use + tool_result have matching tool_ids and timestamps.

        JS _duration() computes (result.timestamp - use.timestamp) / 1000.
        """
        entries = _parse_sequence([
            _make_tool_use("Bash", "tu_dur", {"command": "make build"}, "2026-03-24T12:00:00Z"),
            _make_tool_result("tu_dur", "Build complete", False, "2026-03-24T12:00:05Z"),
        ])
        tool_use = next(e for e in entries if e["type"] == "tool_use")
        tool_result = next(e for e in entries if e["type"] == "tool_result")
        # Matching tool_id enables JS _resultMap pairing
        assert tool_use["tool_id"] == "tu_dur"
        assert tool_result["tool_id"] == "tu_dur"
        # Both have timestamps for duration computation
        assert tool_use["timestamp"] == "2026-03-24T12:00:00Z"
        assert tool_result["timestamp"] == "2026-03-24T12:00:05Z"

    def test_error_flag_from_is_error(self):
        """tool_result with is_error=true → error indicator badge."""
        entries = _parse_sequence([
            _make_tool_use("Bash", "tu_err", {"command": "cat /nonexistent"}, _ts(0)),
            _make_tool_result("tu_err", "Error: file not found", True, _ts(1)),
        ])
        tool_result = next(e for e in entries if e["type"] == "tool_result")
        assert tool_result["is_error"] is True

    def test_no_error_flag_on_success(self):
        """Successful tool_result → is_error=false."""
        entries = _parse_sequence([
            _make_tool_use("Bash", "tu_ok", {"command": "echo hi"}, _ts(0)),
            _make_tool_result("tu_ok", "hi\n", False, _ts(1)),
        ])
        tool_result = next(e for e in entries if e["type"] == "tool_result")
        assert tool_result["is_error"] is False

    def test_line_count_from_content(self):
        """tool_result content with N lines → line count badge.

        JS _countLines() counts newlines + 1 in the content string.
        Server must preserve content so the client can count.
        """
        multiline = "line1\nline2\nline3\nline4\nline5\n"
        entries = _parse_sequence([
            _make_tool_use("Read", "tu_lc", {"file_path": "/test.py"}, _ts(0)),
            _make_tool_result("tu_lc", multiline, False, _ts(1)),
        ])
        tool_result = next(e for e in entries if e["type"] == "tool_result")
        assert tool_result["content"] == multiline
        # Client computes: 5 newlines + 1 = 6 lines

    def test_edit_badges_from_input_strings(self):
        """Edit tool_use has old_string and new_string for +/- line badges.

        JS metaDisplay() counts lines in input.new_string (added) and
        input.old_string (removed) for Edit entries.
        """
        entries = _parse_sequence([
            _make_tool_use("Edit", "tu_edit", {
                "file_path": "/test.py",
                "old_string": "def foo():\n    pass\n",
                "new_string": "def foo():\n    return 42\n    # done\n",
            }),
        ])
        inp = entries[0]["input"]
        assert "old_string" in inp
        assert "new_string" in inp
        assert "\n" in inp["old_string"]
        assert "\n" in inp["new_string"]

    def test_write_badge_from_input_content(self):
        """Write tool_use has input.content for line count badge."""
        entries = _parse_sequence([
            _make_tool_use("Write", "tu_write", {
                "file_path": "/new.py",
                "content": "import os\nimport sys\n\ndef main():\n    pass\n",
            }),
        ])
        assert "content" in entries[0]["input"]
        assert "\n" in entries[0]["input"]["content"]


# ── TestGapDetection ─────────────────────────────────────────────────

class TestGapDetection:
    """Verify entry types enable correct gap detection.

    Client-side hasGap(idx) adds a visual separator when transitioning
    between user and non-user entries:
        prevIsUser = prev.type === 'user'
        currIsUser = curr.type === 'user'
        return prevIsUser !== currIsUser
    """

    def test_gap_between_different_roles(self):
        """user then assistant → gap=true on assistant.

        prev.type='user', curr.type='assistant_text' → user !== non-user.
        """
        entries = _parse_sequence([
            _make_user_message("Hello, help me with this.", _ts(0)),
            _make_assistant_text("Sure, let me help.", _ts(1)),
        ])
        assert entries[0]["type"] == "user"
        assert entries[1]["type"] == "assistant_text"
        # user → assistant_text: types differ for the user check → gap fires

    def test_no_gap_within_same_role(self):
        """assistant_text then tool_use → gap=false.

        Both have type !== 'user', so hasGap returns false.
        """
        entries = _parse_sequence([
            _make_assistant_text("Let me check the file.", _ts(0)),
            _make_tool_use("Read", "tu_ng", {"file_path": "/a.py"}, _ts(1)),
        ])
        text_entry = next(e for e in entries if e["type"] == "assistant_text")
        tool_entry = next(e for e in entries if e["type"] == "tool_use")
        assert text_entry["type"] != "user"
        assert tool_entry["type"] != "user"

    def test_gap_after_user_message(self):
        """tool_result then user → gap=true on user.

        tool_result.type='tool_result' (not user), then user.type='user' → gap fires.
        """
        entries = _parse_sequence([
            _make_tool_use("Read", "tu_ga", {"file_path": "/a.py"}, _ts(0)),
            _make_tool_result("tu_ga", "file contents here", False, _ts(1)),
            _make_user_message("Now fix the bug.", _ts(2)),
        ])
        # Find the transition point: tool_result → user
        tr_idx = next(i for i, e in enumerate(entries) if e["type"] == "tool_result")
        user_idx = next(i for i, e in enumerate(entries) if e["type"] == "user")
        assert user_idx > tr_idx
        assert entries[tr_idx]["type"] != "user"
        assert entries[user_idx]["type"] == "user"

    def test_tool_result_between_does_not_trigger_gap(self):
        """tool_use then tool_result → both non-user, no gap."""
        entries = _parse_sequence([
            _make_tool_use("Read", "tu_nr", {"file_path": "/a.py"}, _ts(0)),
            _make_tool_result("tu_nr", "file contents", False, _ts(1)),
        ])
        assert entries[0]["type"] != "user"
        assert entries[1]["type"] != "user"

    def test_full_conversation_gap_sequence(self):
        """Realistic conversation: user → assistant → tools → user → assistant.

        Gaps should fire at each user↔non-user transition.
        """
        entries = _parse_sequence([
            _make_user_message("Fix the bug in server.py", _ts(0)),
            _make_assistant_text("I'll look at the file.", _ts(1)),
            _make_tool_use("Read", "tu_cv", {"file_path": "/server.py"}, _ts(2)),
            _make_tool_result("tu_cv", "def main(): pass", False, _ts(3)),
            _make_user_message("Thanks, now deploy it.", _ts(4)),
            _make_assistant_text("Deploying now.", _ts(5)),
        ])
        types = [e["type"] for e in entries]
        assert types == [
            "user", "assistant_text", "tool_use", "tool_result",
            "user", "assistant_text",
        ]
        # Gap at index 1: user→assistant_text (yes)
        # No gap at 2: assistant_text→tool_use (both non-user)
        # No gap at 3: tool_use→tool_result (both non-user)
        # Gap at 4: tool_result→user (yes)
        # Gap at 5: user→assistant_text (yes)


# ── TestSemanticTileRendering ────────────────────────────────────────

class TestSemanticTileRendering:
    """Verify semantic_bash entries have the fields for tile rendering.

    Client-side renders semantic_bash entries as enriched tiles when
    _enriched_title/_enriched_tags are populated via lazy API fetch
    (keyed by source_id).  Falls back to entry.content when enrichment
    hasn't loaded or fails.
    """

    def test_enriched_tile_shows_title(self):
        """semantic_bash with source_id → enables lazy title fetch via /api/graph/{id}."""
        entries = _parse_sequence([
            _make_tool_result("tu_ns", "\u2713 Note saved (src:f6c6c43e-24a1-4b9f-8c3d-1e2f3a4b5c6d)\nTags: pitfall, dashboard"),
        ])
        entry = next(e for e in entries if e.get("type") == "semantic_bash")
        assert entry["type"] == "semantic_bash"
        assert entry["source_id"] == "f6c6c43e-24a1-4b9f-8c3d-1e2f3a4b5c6d"
        # Client fetches /api/graph/{source_id} to populate _enriched_title

    def test_enriched_tile_shows_preview(self):
        """semantic_bash content field serves as preview fallback."""
        entries = _parse_sequence([
            _make_tool_result("tu_np", "\u2713 Note saved (src:abcdef01-2345-6789-0123-456789abcdef)\nTags: design"),
        ])
        entry = next(e for e in entries if e.get("type") == "semantic_bash")
        assert len(entry["content"]) > 0
        # Content is the preview until lazy enrichment populates _enriched_preview

    def test_enriched_tile_shows_tags(self):
        """Note creation includes tag info in content for immediate display."""
        entries = _parse_sequence([
            _make_tool_result("tu_nt", "\u2713 Note saved (src:aabb0011-2233-4455-6677-8899aabbccdd)\nTags: pitfall, security"),
        ])
        entry = next(e for e in entries if e.get("type") == "semantic_bash")
        assert entry["semantic_type"] == "note-created"
        # Tag names are in the content string; client also fetches from API
        assert "pitfall" in entry["content"] or entry.get("source_id")

    def test_unenriched_tile_shows_raw_content(self):
        """semantic_bash without enrichment → falls back to content field.

        JS renderer uses entry.content when _enriched_title is null.
        """
        entries = _parse_sequence([
            _make_tool_result("tu_ue", "\u2713 Captured: deadbeef-1234-5678-9abc-def012345678 (raw thought)"),
        ])
        entry = next(e for e in entries if e.get("type") == "semantic_bash")
        assert entry["type"] == "semantic_bash"
        assert entry["content"]  # non-empty string for fallback display

    def test_thought_captured_has_purple_border(self):
        """semantic_type='thought-captured' → drives purple border in renderer.

        JS borderClass() uses semantic_type to select visual styling.
        """
        entries = _parse_sequence([
            _make_tool_result("tu_tc", "\u2713 Captured: a1b2c3d4-5e6f-7890-abcd-ef1234567890 (thought about auth)"),
        ])
        entry = next(e for e in entries if e.get("type") == "semantic_bash")
        assert entry["type"] == "semantic_bash"
        assert entry["semantic_type"] == "thought-captured"
        assert entry["source_id"] == "a1b2c3d4-5e6f-7890-abcd-ef1234567890"

    def test_pitfall_has_red_border(self):
        """Tags including 'pitfall' → drives red border in renderer.

        The 'pitfall' tag is preserved in the content string.  Client-side
        lazy enrichment fetches tags from /api/graph/{source_id} and applies
        the pitfall styling when tags include 'pitfall'.
        """
        entries = _parse_sequence([
            _make_tool_result("tu_pf", "\u2713 Note saved (src:f6c6c43e-24a1-4b9f-8c3d-1e2f3a4b5c6d)\nTags: pitfall, dashboard"),
        ])
        entry = next(e for e in entries if e.get("type") == "semantic_bash")
        assert entry["type"] == "semantic_bash"
        assert entry["semantic_type"] == "note-created"
        assert entry["source_id"]  # enables tag fetch for pitfall styling
        assert "pitfall" in entry["content"]

    def test_dispatch_approved_tile(self):
        """dispatch-approved semantic_bash has bead_id for tile rendering."""
        entries = _parse_sequence([
            _make_tool_use("Bash", "tu_da", {"command": "graph dispatch approve auto-5kj"}, _ts(0)),
        ])
        entry = next(e for e in entries if e.get("type") == "semantic_bash")
        assert entry["type"] == "semantic_bash"
        assert entry["semantic_type"] == "dispatch-approved"
        assert entry["bead_id"] == "auto-5kj"
        assert "auto-5kj" in entry["content"]

    def test_state_changed_tile(self):
        """state-changed semantic_bash has bead_id and state fields."""
        entries = _parse_sequence([
            _make_tool_use("Bash", "tu_sc", {
                "command": "bd set-state auto-5kj status=done --reason 'tests pass'",
            }, _ts(0)),
        ])
        entry = next(e for e in entries if e.get("type") == "semantic_bash")
        assert entry["type"] == "semantic_bash"
        assert entry["semantic_type"] == "state-changed"
        assert entry["bead_id"] == "auto-5kj"
        assert "status=done" in entry["state"]

    def test_comment_added_from_bash(self):
        """graph comment Bash command → semantic_bash with source_id."""
        entries = _parse_sequence([
            _make_tool_use("Bash", "tu_gc", {
                "command": "graph comment dc4c73ee 'Fixed the wording'",
            }, _ts(0)),
        ])
        entry = next(e for e in entries if e.get("type") == "semantic_bash")
        assert entry["type"] == "semantic_bash"
        assert entry["semantic_type"] == "comment-added"
        assert entry["source_id"] == "dc4c73ee"

    def test_comment_added_from_tool_result(self):
        """Comment added tool_result → semantic_bash with comment_id."""
        entries = _parse_sequence([
            _make_tool_result("tu_ca", "\u2713 Comment added (id:df5f1546-9a8b-4c7d-e6f5-1234abcd5678) on f6c6c43e"),
        ])
        entry = next(e for e in entries if e.get("type") == "semantic_bash")
        assert entry["type"] == "semantic_bash"
        assert entry["semantic_type"] == "comment-added"
        assert entry["comment_id"] == "df5f1546-9a8b-4c7d-e6f5-1234abcd5678"
