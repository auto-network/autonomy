"""Tests for GraphAppender — tail-primary graph ingest (W3, auto-ea9g3).

Exercises the appender's core contract in isolation from the live tailer
(no inotify, no asyncio): feed_lines() writes turns, advances the offset
only on commit, derives an eager row's title once, and produces content
identical to a full reparse regardless of how the bytes were batched —
the property the soak's "sweeps still on, zero duplicates" requirement
depends on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.graph.appender import GraphAppender
from tools.graph.db import GraphDB
from tools.graph.ingest import ingest_claude_code_session
from tools.graph.models import Source


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _evict_pooled_orgs():
    GraphDB.close_all_pooled()
    yield
    GraphDB.close_all_pooled()


@pytest.fixture
def org_env(tmp_path, monkeypatch):
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    GraphDB(orgs_dir / "autonomy.db").close()  # pre-create, matches prod (eager creation always runs first)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    yield orgs_dir


def _entry_line(text: str, ts: str, role: str = "user", uuid: str = "u1") -> bytes:
    entry = {
        "type": role, "uuid": uuid,
        "message": {"role": role, "content": text},
        "timestamp": ts,
    }
    if role == "assistant":
        entry["message"] = {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": "claude-test",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
    return (json.dumps(entry) + "\n").encode("utf-8")


def _insert_eager_source(orgs_dir: Path, org: str, file_path: Path, *, eager: bool = True) -> str:
    g = GraphDB(orgs_dir / f"{org}.db")
    source = Source(
        type="session",
        platform="claude-code",
        title=None,
        file_path=str(file_path),
        metadata={
            "session_id": file_path.stem,
            "session_uuid": file_path.stem,
            "eager": eager,
            "file_size": 0,
            "graph_ingest_offset": 0,
        },
    )
    g.insert_source(source)
    g.close()
    return source.id


def _read_source(orgs_dir: Path, org: str, source_id: str) -> dict:
    g = GraphDB(orgs_dir / f"{org}.db")
    row = dict(g.conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone())
    g.close()
    return row


def _thoughts_for(orgs_dir: Path, org: str, source_id: str) -> list[dict]:
    g = GraphDB(orgs_dir / f"{org}.db")
    rows = [dict(r) for r in g.conn.execute(
        "SELECT * FROM thoughts WHERE source_id = ? ORDER BY turn_number", (source_id,)
    ).fetchall()]
    g.close()
    return rows


# ══════════════════════════════════════════════════════════════════════
# Core feed_lines behavior
# ══════════════════════════════════════════════════════════════════════


class TestFeedLinesCore:
    def test_writes_turns_and_advances_offset(self, org_env, tmp_path):
        orgs_dir = org_env
        jsonl = tmp_path / "sess.jsonl"
        source_id = _insert_eager_source(orgs_dir, "autonomy", jsonl)

        appender = GraphAppender(
            org="autonomy", source_id=source_id, file_path=jsonl, session_meta={},
        )
        line1 = _entry_line("Do the thing", "2026-05-01T10:00:00Z")
        line2 = _entry_line("Doing it", "2026-05-01T10:00:01Z", role="assistant", uuid="a1")
        batch = line1 + line2
        result = appender.feed_lines([line1, line2], new_byte_offset=len(batch))

        assert result == {"new_turns": 2, "skipped_lines": 0, "source_missing": False}
        assert appender.graph_ingest_offset == len(batch)

        thoughts = _thoughts_for(orgs_dir, "autonomy", source_id)
        assert len(thoughts) == 1
        assert thoughts[0]["content"] == "Do the thing"

        row = _read_source(orgs_dir, "autonomy", source_id)
        meta = json.loads(row["metadata"])
        assert meta["graph_ingest_offset"] == len(batch)
        assert meta["extractor_state"]["turn_number"] == 2

    def test_offset_only_advances_after_commit_not_on_exception(self, org_env, tmp_path, monkeypatch):
        """If the DB write blows up mid-batch, graph_ingest_offset (the
        in-memory instance attribute) must not have moved — the caller
        re-feeds the same bytes next tick."""
        orgs_dir = org_env
        jsonl = tmp_path / "sess.jsonl"
        source_id = _insert_eager_source(orgs_dir, "autonomy", jsonl)
        appender = GraphAppender(org="autonomy", source_id=source_id, file_path=jsonl, session_meta={})

        def _boom(*a, **kw):
            raise RuntimeError("simulated write failure")

        monkeypatch.setattr("tools.graph.appender._write_new_turns", _boom)
        line1 = _entry_line("Do the thing", "2026-05-01T10:00:00Z")
        with pytest.raises(RuntimeError):
            appender.feed_lines([line1], new_byte_offset=len(line1))

        assert appender.graph_ingest_offset == 0

    def test_source_missing_returns_flag_no_crash(self, org_env, tmp_path):
        orgs_dir = org_env
        jsonl = tmp_path / "sess.jsonl"
        appender = GraphAppender(
            org="autonomy", source_id="does-not-exist", file_path=jsonl, session_meta={},
        )
        line1 = _entry_line("Do the thing", "2026-05-01T10:00:00Z")
        result = appender.feed_lines([line1], new_byte_offset=len(line1))
        assert result["source_missing"] is True
        assert result["new_turns"] == 0

    def test_skipped_lines_counted_and_do_not_crash(self, org_env, tmp_path):
        orgs_dir = org_env
        jsonl = tmp_path / "sess.jsonl"
        source_id = _insert_eager_source(orgs_dir, "autonomy", jsonl)
        appender = GraphAppender(org="autonomy", source_id=source_id, file_path=jsonl, session_meta={})

        bad_line = b"not json at all\n"
        good_line = _entry_line("Real content here", "2026-05-01T10:00:00Z")
        result = appender.feed_lines([bad_line, good_line], new_byte_offset=len(bad_line) + len(good_line))
        assert result["skipped_lines"] == 1
        assert result["new_turns"] == 1


# ══════════════════════════════════════════════════════════════════════
# Title derivation on first content (W2/W5 interplay)
# ══════════════════════════════════════════════════════════════════════


class TestEagerTitleDerivation:
    def test_title_derived_once_then_stable(self, org_env, tmp_path, monkeypatch):
        orgs_dir = org_env
        jsonl = tmp_path / "sess.jsonl"
        source_id = _insert_eager_source(orgs_dir, "autonomy", jsonl)
        appender = GraphAppender(org="autonomy", source_id=source_id, file_path=jsonl, session_meta={})

        monkeypatch.setattr("tools.graph.appender._derive_session_title",
                             lambda meta, fp, sm, turns: (turns[0]["content"] if turns else None))

        line1 = _entry_line("First real question", "2026-05-01T10:00:00Z")
        appender.feed_lines([line1], new_byte_offset=len(line1))
        row = _read_source(orgs_dir, "autonomy", source_id)
        assert row["title"] == "First real question"

        line2 = _entry_line("Second question", "2026-05-01T10:01:00Z", uuid="u2")
        appender.feed_lines([line2], new_byte_offset=len(line1) + len(line2))
        row2 = _read_source(orgs_dir, "autonomy", source_id)
        assert row2["title"] == "First real question", "title must not be re-derived after the first content batch"

    def test_non_eager_row_title_never_touched(self, org_env, tmp_path):
        """A source NOT created via eager-creation (metadata.eager absent)
        must never have its title touched by the appender — matches W5's
        creation-only rule for normally-created sources."""
        orgs_dir = org_env
        jsonl = tmp_path / "sess.jsonl"
        source_id = _insert_eager_source(orgs_dir, "autonomy", jsonl, eager=False)
        appender = GraphAppender(org="autonomy", source_id=source_id, file_path=jsonl, session_meta={})

        line1 = _entry_line("Some content", "2026-05-01T10:00:00Z")
        appender.feed_lines([line1], new_byte_offset=len(line1))
        row = _read_source(orgs_dir, "autonomy", source_id)
        assert row["title"] is None


# ══════════════════════════════════════════════════════════════════════
# Resume / split-batch equivalence (mirrors W1's property test, at the
# appender+DB level rather than pure-extractor level)
# ══════════════════════════════════════════════════════════════════════


class TestResumeEquivalence:
    def test_two_batches_equal_one_batch(self, org_env, tmp_path):
        orgs_dir = org_env

        # Full-batch reference.
        jsonl_full = tmp_path / "full.jsonl"
        source_full = _insert_eager_source(orgs_dir, "autonomy", jsonl_full)
        appender_full = GraphAppender(org="autonomy", source_id=source_full, file_path=jsonl_full, session_meta={})
        lines = [
            _entry_line("Question one", "2026-05-01T10:00:00Z", uuid="u1"),
            _entry_line("Answer one", "2026-05-01T10:00:01Z", role="assistant", uuid="a1"),
            _entry_line("Question two", "2026-05-01T10:00:02Z", uuid="u2"),
        ]
        appender_full.feed_lines(lines, new_byte_offset=sum(len(l) for l in lines))

        # Split-batch: same three lines fed in two ticks, second tick
        # resumed via from_source() (the gap-catch-up path).
        jsonl_split = tmp_path / "split.jsonl"
        source_split = _insert_eager_source(orgs_dir, "autonomy", jsonl_split)
        appender_a = GraphAppender(org="autonomy", source_id=source_split, file_path=jsonl_split, session_meta={})
        offset_a = len(lines[0]) + len(lines[1])
        appender_a.feed_lines(lines[:2], new_byte_offset=offset_a)

        source_row = _read_source(orgs_dir, "autonomy", source_split)
        appender_b = GraphAppender.from_source(
            source_row, org="autonomy", file_path=jsonl_split, session_meta={},
        )
        assert appender_b.graph_ingest_offset == offset_a
        appender_b.feed_lines(lines[2:], new_byte_offset=offset_a + len(lines[2]))

        thoughts_full = _thoughts_for(orgs_dir, "autonomy", source_full)
        thoughts_split = _thoughts_for(orgs_dir, "autonomy", source_split)
        assert [t["content"] for t in thoughts_full] == [t["content"] for t in thoughts_split]
        assert [t["turn_number"] for t in thoughts_full] == [t["turn_number"] for t in thoughts_split]


# ══════════════════════════════════════════════════════════════════════
# Crash/idempotence + soak dedup proof
# ══════════════════════════════════════════════════════════════════════


class TestIdempotenceAndDedup:
    def test_refeeding_same_batch_produces_no_duplicates(self, org_env, tmp_path):
        """Simulates a crash where the caller's own offset bookkeeping
        didn't persist and the same bytes get handed to feed_lines twice.
        A fresh GraphAppender (as if reconstructed post-crash from the
        still-zero persisted offset) re-extracts the same turns; the
        max_turn dedup inside _write_new_turns must produce zero
        duplicate rows."""
        orgs_dir = org_env
        jsonl = tmp_path / "sess.jsonl"
        source_id = _insert_eager_source(orgs_dir, "autonomy", jsonl)

        line1 = _entry_line("Do the thing", "2026-05-01T10:00:00Z")
        line2 = _entry_line("Doing it", "2026-05-01T10:00:01Z", role="assistant", uuid="a1")
        batch = [line1, line2]
        offset = len(line1) + len(line2)

        appender1 = GraphAppender(org="autonomy", source_id=source_id, file_path=jsonl, session_meta={})
        appender1.feed_lines(batch, new_byte_offset=offset)

        # "Crash recovery": a fresh appender with NO persisted offset
        # re-processes the exact same bytes (worst case — offset never
        # made it to disk).
        appender2 = GraphAppender(org="autonomy", source_id=source_id, file_path=jsonl, session_meta={})
        appender2.feed_lines(batch, new_byte_offset=offset)

        thoughts = _thoughts_for(orgs_dir, "autonomy", source_id)
        assert len(thoughts) == 1, f"expected exactly 1 deduped thought, got {len(thoughts)}"

    def test_soak_dedup_against_concurrent_full_reparse(self, org_env, tmp_path):
        """W3 AC: legacy sweeps still enabled alongside the appender must
        not double-ingest. Feed via the appender, then run a full-reparse
        sweep (ingest_claude_code_session) over the same file on disk —
        assert the content converges with zero duplicates."""
        orgs_dir = org_env
        jsonl = tmp_path / "sess.jsonl"
        jsonl.parent.mkdir(parents=True, exist_ok=True)
        source_id = _insert_eager_source(orgs_dir, "autonomy", jsonl)

        line1 = _entry_line("Do the thing", "2026-05-01T10:00:00Z")
        line2 = _entry_line("Doing it", "2026-05-01T10:00:01Z", role="assistant", uuid="a1")
        jsonl.write_bytes(line1 + line2)

        appender = GraphAppender(org="autonomy", source_id=source_id, file_path=jsonl, session_meta={})
        appender.feed_lines([line1, line2], new_byte_offset=len(line1) + len(line2))

        db = GraphDB(orgs_dir / "autonomy.db")
        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "tools.graph.ingest._lookup_dashboard_label", return_value=None,
        ):
            result = ingest_claude_code_session(db, jsonl)
        db.close()

        assert result["status"] in ("updated", "refreshed")
        thoughts = _thoughts_for(orgs_dir, "autonomy", source_id)
        assert len(thoughts) == 1, f"sweep must not duplicate appender-written content, got {len(thoughts)}"


# ══════════════════════════════════════════════════════════════════════
# Concurrency — two sessions, same org, concurrent feeds
# ══════════════════════════════════════════════════════════════════════
#
# asyncio.to_thread's default executor hands each concurrent submission a
# real, potentially-distinct OS thread (verified: 8 concurrent calls land
# on 8 distinct threads). feed_lines() must be safe to call from two such
# threads at once for two DIFFERENT sessions in the SAME org — that's
# exactly what happens when two sessions in one org both grow their JSONL
# around the same tail tick.


class TestConcurrentAppenders:
    def test_two_sessions_same_org_concurrent_feeds_both_land_cleanly(self, org_env, tmp_path):
        import threading

        orgs_dir = org_env
        jsonl_a = tmp_path / "session-a.jsonl"
        jsonl_b = tmp_path / "session-b.jsonl"
        source_a = _insert_eager_source(orgs_dir, "autonomy", jsonl_a)
        source_b = _insert_eager_source(orgs_dir, "autonomy", jsonl_b)

        appender_a = GraphAppender(org="autonomy", source_id=source_a, file_path=jsonl_a, session_meta={})
        appender_b = GraphAppender(org="autonomy", source_id=source_b, file_path=jsonl_b, session_meta={})

        lines_a = [_entry_line(f"A turn {i}", f"2026-05-01T10:00:{i:02d}Z", uuid=f"a-u{i}") for i in range(20)]
        lines_b = [_entry_line(f"B turn {i}", f"2026-05-01T11:00:{i:02d}Z", uuid=f"b-u{i}") for i in range(20)]

        errors: list[Exception] = []

        def _feed(appender, lines):
            try:
                offset = sum(len(l) for l in lines)
                appender.feed_lines(lines, new_byte_offset=offset)
            except Exception as exc:  # noqa: BLE001 - captured for the assertion below
                errors.append(exc)

        threads = [
            threading.Thread(target=_feed, args=(appender_a, lines_a)),
            threading.Thread(target=_feed, args=(appender_b, lines_b)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"concurrent feed_lines raised: {errors}"

        thoughts_a = _thoughts_for(orgs_dir, "autonomy", source_a)
        thoughts_b = _thoughts_for(orgs_dir, "autonomy", source_b)
        assert [t["content"] for t in thoughts_a] == [f"A turn {i}" for i in range(20)]
        assert [t["content"] for t in thoughts_b] == [f"B turn {i}" for i in range(20)]
        assert appender_a.graph_ingest_offset == sum(len(l) for l in lines_a)
        assert appender_b.graph_ingest_offset == sum(len(l) for l in lines_b)
