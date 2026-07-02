"""Tail-primary graph appender (W3).

``GraphAppender`` owns per-session incremental graph-ingest state so the
dashboard's live tailer can feed graph writes as bytes arrive, instead of
graph content only landing via periodic full-reparse sweeps
(``ingest_claude_code_session`` / ``ingest_codex_session``). One instance
per live session, created alongside eager source creation (W2) and fed a
batch of complete raw JSONL lines on every tail tick.

Two cursors matter here and must never be conflated:

* ``graph_ingest_offset`` — bytes of the JSONL this appender has durably
  committed into the graph. Lives on this instance and is persisted into
  the source's ``metadata`` JSON (``sources.metadata``) alongside the
  extractor's resume state, distinct from...
* the *viewer's* ``tmux_sessions.file_offset`` — a completely separate
  cursor owned by the live-viewer tailer (``SessionMonitor._tail_one``).
  The viewer may be ahead of the graph cursor (it parses tool-use tiles
  etc. that never become graph turns); neither cursor is ever read from
  or written to the other's storage.

Idempotence: turn writes are keyed by ``turn_number`` (deduped against
``MAX(turn_number)`` already in the DB, same as the full-reparse path —
see ``_write_new_turns``), so at-least-once delivery from a crash-and-
resume, or a legacy sweep racing the same file, both converge to the
same content with no duplicates.

Concurrency: each ``feed_lines`` call opens its own connection (never the
pooled ``GraphDB.for_org``) and closes it before returning — deliberately
NOT reused across calls. Every live session's appender runs its DB work
via ``asyncio.to_thread``, and the default executor spins up a real OS
thread per concurrent submission (verified: 8 concurrent ticks land on 8
distinct threads), not just per session. ``sqlite3.connect()`` defaults
to ``check_same_thread=True``, so a *pooled* connection first opened on
one worker thread raises ``ProgrammingError`` the moment a second
concurrent tick — same org, different session, different thread — tries
to use it; that isn't a hypothetical, it reproduces on the second call.
A fresh connection per call sidesteps the thread-affinity problem
entirely. ``_ORG_LOCKS`` then serializes the DB-touching section itself
per org, so two sessions in the same org can't race each other's
transaction against the underlying SQLite file (WAL still allows only
one writer at a time; without app-level serialization a losing writer
would hit ``SQLITE_BUSY`` instead of simply waiting its turn). Decode and
extraction (pure per-instance in-memory state, never shared) happen
outside the lock; only open-connection/get-source/write-turns/commit/
close holds it, for the shortest critical section that's still correct.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .db import GraphDB, resolve_caller_db_path
from .ingest import (
    ClaudeTurnExtractor,
    CodexTurnExtractor,
    _dedup_new_turns,
    _derive_session_title,
    _write_new_turns,
)

_EXTRACTORS = {
    "claude": ClaudeTurnExtractor,
    "codex": CodexTurnExtractor,
}

# Per-org locks guarding the DB-writing section of feed_lines(). Every live
# session in an org shares one pooled connection (GraphDB.for_org); without
# this, two appenders in the same org feeding on different threads at the
# same time could interleave writes against that shared connection.
_ORG_LOCKS: dict[str, threading.Lock] = {}
_ORG_LOCKS_GUARD = threading.Lock()


def _org_lock(org: str) -> threading.Lock:
    with _ORG_LOCKS_GUARD:
        lock = _ORG_LOCKS.get(org)
        if lock is None:
            lock = threading.Lock()
            _ORG_LOCKS[org] = lock
        return lock


def extractor_class_for_harness(harness: str):
    return _EXTRACTORS.get(harness, ClaudeTurnExtractor)


class GraphAppender:
    """Incremental graph writer for one live session's JSONL tail."""

    def __init__(
        self,
        *,
        org: str,
        source_id: str,
        file_path: Path,
        session_meta: dict,
        harness: str = "claude",
        default_model: str = "claude",
        graph_ingest_offset: int = 0,
        extractor_state: dict | None = None,
    ) -> None:
        self.org = org
        self.source_id = source_id
        self.file_path = Path(file_path)
        self.session_meta = session_meta or {}
        self.harness = harness
        self.default_model = default_model
        self.graph_ingest_offset = graph_ingest_offset
        self.extractor = extractor_class_for_harness(harness)(state=extractor_state)

    @classmethod
    def from_source(
        cls, source: dict, *, org: str, file_path: Path, session_meta: dict,
        harness: str = "claude", default_model: str = "claude",
    ) -> "GraphAppender":
        """Restore an appender from a source row's persisted metadata — the
        gap-catch-up resume path used when a tail is (re)established.

        ``org`` must be supplied by the caller (usually the same
        ``session_target_org(...)`` resolution used to find/create
        ``source`` in the first place) — a ``sources`` row has no ``org``
        column of its own; org is implicit in which per-org DB it lives in.
        """
        meta = json.loads(source["metadata"]) if source.get("metadata") else {}
        return cls(
            org=org,
            source_id=source["id"],
            file_path=file_path,
            session_meta=session_meta,
            harness=harness,
            default_model=default_model,
            graph_ingest_offset=meta.get("graph_ingest_offset", 0),
            extractor_state=meta.get("extractor_state"),
        )

    def reset(self) -> None:
        """Discard extractor state and offset — used on truncation / inode
        change, where the file at ``file_path`` is no longer the file this
        appender's state was built against. The next ``feed_lines`` call
        should be given the file's content from byte 0."""
        self.graph_ingest_offset = 0
        self.extractor = extractor_class_for_harness(self.harness)()

    def feed_lines(self, lines: list[bytes], *, new_byte_offset: int) -> dict:
        """Decode + extract + write one batch, in a single transaction.

        ``lines`` are complete (newline-terminated in the source, already
        stripped) raw JSONL line bytes, in file order. ``new_byte_offset``
        is the absolute file offset this batch's bytes end at — caller-
        computed with the same last-newline discipline ``_tail_one`` uses,
        so a batch is never split mid-line.

        ``graph_ingest_offset`` only advances after ``db.commit()``
        succeeds — a crash between decode and commit leaves the offset at
        its previous value, so the same bytes are naturally re-fed and
        re-extracted next time. Re-extraction is safe: the resulting turns
        carry the same ``turn_number``s (the extractor state driving them
        wasn't persisted either), and ``_write_new_turns`` dedupes against
        ``MAX(turn_number)`` already committed.

        Returns ``{"new_turns": int, "skipped_lines": int,
        "source_missing": bool}``. ``source_missing=True`` means the
        source row this appender targets no longer exists (deleted, or a
        force re-ingest elsewhere) — the caller should reinitialize via
        :meth:`from_source` on the next tick rather than keep feeding a
        stale ``source_id``.
        """
        new_turns: list[dict] = []
        skipped = 0
        for raw in lines:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            turn = self.extractor.feed(entry)
            if turn is not None:
                new_turns.append(turn)

        # Everything from here on touches the org's DB — a fresh
        # connection (not the pooled GraphDB.for_org, which is not safe to
        # share across the worker threads asyncio.to_thread actually uses)
        # opened and closed within this call, serialized per org so two
        # sessions' appenders can't race each other's transaction against
        # the underlying SQLite file. See the module docstring for why.
        with _org_lock(self.org):
            db = GraphDB(resolve_caller_db_path(self.org))
            try:
                source = db.get_source(self.source_id)
                if source is None:
                    return {"new_turns": 0, "skipped_lines": skipped, "source_missing": True}

                max_turn = db.get_max_turn(self.source_id)
                dedup_turns = _dedup_new_turns(db, self.source_id, new_turns, max_turn)

                state = self.extractor.state
                thoughts, derivations, entities = _write_new_turns(
                    db, self.source_id, dedup_turns,
                    model=state.get("model") or self.default_model,
                )

                existing_meta = json.loads(source["metadata"]) if source.get("metadata") else {}
                existing_meta["extractor_state"] = state
                existing_meta["graph_ingest_offset"] = new_byte_offset
                existing_meta["total_turns"] = max(max_turn, dedup_turns[-1]["turn_number"] if dedup_turns else max_turn)
                if state.get("total_input_tokens") is not None:
                    existing_meta["total_input_tokens"] = state["total_input_tokens"]
                if state.get("total_output_tokens") is not None:
                    existing_meta["total_output_tokens"] = state["total_output_tokens"]
                if state.get("first_ts") and not existing_meta.get("started_at"):
                    existing_meta["started_at"] = state["first_ts"]
                if state.get("last_ts"):
                    existing_meta["ended_at"] = state["last_ts"]

                # W2/W5 interplay: an eager row's title is derived once, on
                # the first batch that lands real content — mirrors the
                # full-reparse incremental branch's rule in
                # _ingest_text_session. Scoped to this batch's turns only
                # (not the session's full history) so the appender doesn't
                # need to hold every turn in memory; if this batch's turns
                # are all low-signal, the title stays unset and the next
                # content-bearing batch tries again — self-correcting, same
                # as the pre-eager-row fallback behavior.
                new_title = None
                if not source.get("title") and existing_meta.get("eager") and dedup_turns:
                    new_title = _derive_session_title({}, self.file_path, self.session_meta, dedup_turns)

                db.update_source_summary(
                    self.source_id,
                    title=new_title,
                    metadata=existing_meta,
                    last_activity_at=state.get("last_ts") or source.get("last_activity_at"),
                )
                db.commit()
            finally:
                db.close()

        self.graph_ingest_offset = new_byte_offset
        return {
            "new_turns": len(dedup_turns),
            "skipped_lines": skipped,
            "source_missing": False,
        }
