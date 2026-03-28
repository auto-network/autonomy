"""Tests for the parentUuid chain data structure linking JSONL files.

When Claude restarts (plan mode exit, context exhaustion, /clear), it creates a new
JSONL file. The first entry's parentUuid field links back to the last entry's uuid
of the predecessor file. This chain enables rollover detection without mtime.

These tests verify the parentUuid chain structure in JSONL files, independent
of the monitor's rollover detection logic.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────

def _make_jsonl(directory: Path, uuid: str, entries: list[dict],
                mtime_offset: float = 0) -> Path:
    """Create a JSONL file with specific entries and controlled mtime."""
    p = directory / f"{uuid}.jsonl"
    p.write_text("".join(json.dumps(e) + "\n" for e in entries))
    t = time.time() + mtime_offset
    os.utime(p, (t, t))
    return p


def _read_first_entry(jsonl: Path) -> dict:
    """Read the first JSONL entry from a file."""
    with open(jsonl) as f:
        return json.loads(f.readline())


def _read_last_entry(jsonl: Path) -> dict:
    """Read the last JSONL entry from a file."""
    with open(jsonl) as f:
        lines = [line.strip() for line in f if line.strip()]
        return json.loads(lines[-1])


def _walk_chain_backwards(start_file: Path, all_files: list[Path]) -> list[Path]:
    """Walk the parentUuid chain backwards from start_file, returning all linked files.

    Algorithm:
    1. Read first entry of start_file → get parentUuid
    2. Search all_files for a file whose last entry uuid == parentUuid
    3. Recurse until parentUuid is null

    Returns list of files in chain order (oldest first).
    """
    chain = [start_file]
    current = start_file
    visited = {start_file}

    while True:
        first = _read_first_entry(current)
        parent_uuid = first.get("parentUuid")
        if not parent_uuid:
            break

        # Find predecessor file
        found = False
        for candidate in all_files:
            if candidate in visited:
                continue
            last = _read_last_entry(candidate)
            if last.get("uuid") == parent_uuid:
                chain.insert(0, candidate)
                visited.add(candidate)
                current = candidate
                found = True
                break

        if not found:
            break  # orphan parentUuid — no predecessor found

    return chain


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def chain_dir(tmp_path):
    """Directory for parentUuid chain test files."""
    d = tmp_path / "chain"
    d.mkdir()
    return d


@pytest.fixture
def two_file_chain(chain_dir):
    """Two JSONL files linked by parentUuid."""
    file1 = _make_jsonl(chain_dir, "uuid-file1", [
        {"type": "user", "message": {"content": "start"}, "uuid": "msg-001", "parentUuid": None},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "response 1"}]}, "uuid": "msg-002"},
    ], mtime_offset=-10)

    file2 = _make_jsonl(chain_dir, "uuid-file2", [
        {"type": "user", "message": {"content": "continue"}, "uuid": "msg-003", "parentUuid": "msg-002"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "response 2"}]}, "uuid": "msg-004"},
    ], mtime_offset=0)

    return {"file1": file1, "file2": file2, "dir": chain_dir}


@pytest.fixture
def three_file_chain(chain_dir):
    """Three JSONL files linked by parentUuid chain."""
    file1 = _make_jsonl(chain_dir, "uuid-file1", [
        {"type": "user", "message": {"content": "start"}, "uuid": "msg-001", "parentUuid": None},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "r1"}]}, "uuid": "msg-002"},
    ], mtime_offset=-20)

    file2 = _make_jsonl(chain_dir, "uuid-file2", [
        {"type": "user", "message": {"content": "continue 1"}, "uuid": "msg-003", "parentUuid": "msg-002"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "r2"}]}, "uuid": "msg-004"},
    ], mtime_offset=-10)

    file3 = _make_jsonl(chain_dir, "uuid-file3", [
        {"type": "user", "message": {"content": "continue 2"}, "uuid": "msg-005", "parentUuid": "msg-004"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "r3"}]}, "uuid": "msg-006"},
    ], mtime_offset=0)

    return {"file1": file1, "file2": file2, "file3": file3, "dir": chain_dir}


# ── TestParentUuidChain ───────────────────────────────────────────────────

class TestParentUuidChain:
    """Tests for the parentUuid chain data structure."""

    def test_chain_links_two_files(self, two_file_chain):
        """file2.first_entry.parentUuid == file1.last_entry.uuid.

        Expected: GREEN — tests JSONL data structure, not monitor logic.
        """
        file1 = two_file_chain["file1"]
        file2 = two_file_chain["file2"]

        last_of_file1 = _read_last_entry(file1)
        first_of_file2 = _read_first_entry(file2)

        assert first_of_file2["parentUuid"] == last_of_file1["uuid"], (
            f"file2's parentUuid ({first_of_file2['parentUuid']}) "
            f"should match file1's last uuid ({last_of_file1['uuid']})"
        )

    def test_first_file_has_null_parent(self, two_file_chain):
        """Original session file has parentUuid=null.

        Expected: GREEN — tests JSONL data structure.
        """
        file1 = two_file_chain["file1"]
        first_entry = _read_first_entry(file1)
        assert first_entry["parentUuid"] is None, (
            "First file in chain should have parentUuid=null"
        )

    def test_chain_walk_discovers_all_files(self, three_file_chain):
        """Given 3 linked files, walking backwards from file3 discovers file2 and file1.

        Expected: GREEN — tests the chain walking algorithm.
        """
        file1 = three_file_chain["file1"]
        file2 = three_file_chain["file2"]
        file3 = three_file_chain["file3"]
        all_files = [file1, file2, file3]

        chain = _walk_chain_backwards(file3, all_files)

        assert len(chain) == 3, f"Chain should have 3 files, got {len(chain)}"
        assert chain[0] == file1, "First in chain should be file1 (oldest)"
        assert chain[1] == file2
        assert chain[2] == file3, "Last in chain should be file3 (newest)"

    def test_chain_walk_from_middle(self, three_file_chain):
        """Walking from file2 discovers file1 but not file3.

        Expected: GREEN — chain walk only goes backwards via parentUuid.
        """
        file1 = three_file_chain["file1"]
        file2 = three_file_chain["file2"]
        file3 = three_file_chain["file3"]
        all_files = [file1, file2, file3]

        chain = _walk_chain_backwards(file2, all_files)

        assert len(chain) == 2, f"Walking from file2 should find 2 files, got {len(chain)}"
        assert chain[0] == file1
        assert chain[1] == file2

    def test_orphan_parentuuid_stops_walk(self, chain_dir):
        """File with parentUuid pointing to nonexistent uuid → chain stops.

        Expected: GREEN — walk algorithm handles missing predecessors.
        """
        orphan = _make_jsonl(chain_dir, "orphan", [
            {"type": "user", "message": {"content": "orphan"}, "uuid": "o-001", "parentUuid": "nonexistent"},
        ])

        chain = _walk_chain_backwards(orphan, [orphan])
        assert len(chain) == 1, "Orphan file should be alone in its chain"
        assert chain[0] == orphan

    def test_single_file_chain(self, chain_dir):
        """Single file with null parentUuid → chain of length 1.

        Expected: GREEN — walk algorithm handles single-file chains.
        """
        single = _make_jsonl(chain_dir, "single", [
            {"type": "user", "message": {"content": "solo"}, "uuid": "s-001", "parentUuid": None},
        ])

        chain = _walk_chain_backwards(single, [single])
        assert len(chain) == 1
        assert chain[0] == single
